"""Private local-only CPU worker. Executed by filename, with isolated Python."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import sys
import time
from pathlib import Path

MAX_REQUEST_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_PAIRS = 20
MAX_WINDOWS = 40
WINDOW_OVERLAP = 64
DOCUMENT_WINDOW_MODE = "document_windows_v1"


class ModelMismatch(Exception):
    pass


def _deny_network(*args, **kwargs):
    raise OSError("Local reranker network disabled")


def _offline():
    # Install before any model-library imports, including optional Hub clients.
    class OfflineSocket(socket.socket):
        connect = connect_ex = bind = listen = sendto = sendmsg = _deny_network

    socket.socket = OfflineSocket
    socket.create_connection = socket.getaddrinfo = _deny_network
    def audit(event, args):
        if event in {"socket.connect", "socket.bind", "socket.getaddrinfo",
                     "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto"}:
            _deny_network()

    sys.addaudithook(audit)
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                       "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                       "TOKENIZERS_PARALLELISM": "false", "OMP_NUM_THREADS": "2",
                       "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"})


def _verify_model(model_path, files):
    if not isinstance(model_path, str) or not Path(model_path).is_absolute():
        raise ModelMismatch
    directory = Path(model_path)
    if not directory.is_dir() or not isinstance(files, list) or not 1 <= len(files) <= 64:
        raise ModelMismatch
    names = set()
    for item in files:
        if not isinstance(item, dict):
            raise ModelMismatch
        name, expected, size = (item.get(k) for k in ("file", "sha256", "bytes"))
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name)
                or name in {".", ".."} or name in names or name.endswith(".py")
                or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or type(size) is not int or not 0 <= size <= 2 * 1024**3):
            raise ModelMismatch
        names.add(name)
        source = directory / name
        # HF snapshots use symlinks to cached blobs; verify the actual bytes.
        with source.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != size:
                raise ModelMismatch
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ModelMismatch
    required = {"config.json", "model.safetensors", "tokenizer_config.json"}
    if not required <= names or not ({"tokenizer.json", "vocab.txt"} & names):
        raise ModelMismatch
    # Avoid any additional unpinned loader configuration, adapters or weights.
    for source in directory.iterdir():
        if source.name == "download-provenance.json" and source.is_file():
            continue  # Operator download audit; never read by the model loader.
        if source.name not in names and (source.is_dir() or source.suffix in
                {".json", ".txt", ".safetensors", ".bin", ".pt", ".pth", ".py", ".model"}):
            raise ModelMismatch
    return str(directory)


def _read():
    data = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if not data:
        return None
    if len(data) > MAX_REQUEST_BYTES or not data.endswith(b"\n"):
        raise ValueError
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError
    if type(value.get("request_id")) is not int or value["request_id"] < 1:
        raise ValueError
    return value


def _score(model, query, texts, identity):
    cpu_started = time.process_time()
    if (not isinstance(query, str) or len(query.encode("utf-8")) > MAX_TEXT_BYTES
            or not isinstance(texts, list) or not 1 <= len(texts) <= MAX_PAIRS
            or any(not isinstance(t, str) or len(t.encode("utf-8")) > MAX_TEXT_BYTES for t in texts)):
        raise ValueError
    # Count complete tokenized pairs before the model's fixed 512-token cap.
    # Bounded bytes/pairs also bound this diagnostic tokenization allocation.
    encoded = model.tokenizer([query] * len(texts), texts, padding=False,
                              truncation=False, add_special_tokens=True, verbose=False)
    truncated = sum(len(ids) > 512 for ids in encoded["input_ids"])
    if truncated:
        return {"scores": [], "truncated_count": truncated,
                "skipped_reason": "input_truncated",
                "cpu_ms": (time.process_time() - cpu_started) * 1000}
    pairs = [(query, text) for text in texts]
    values = model.predict(pairs, batch_size=16, show_progress_bar=False,
                           activation_fn=identity, convert_to_numpy=True)
    scores = [float(v) for v in values]
    if len(scores) != len(texts) or any(not math.isfinite(v) for v in scores):
        raise ValueError
    return {"scores": scores, "truncated_count": truncated,
            "cpu_ms": (time.process_time() - cpu_started) * 1000}


class _WindowSkip(Exception):
    def __init__(self, reason):
        self.reason = reason


def _pair_lengths(tokenizer, query, texts):
    encoded = tokenizer([query] * len(texts), texts, padding=False, truncation=False,
                        add_special_tokens=True, verbose=False)
    ids = encoded["input_ids"]
    if not isinstance(ids, list) or len(ids) != len(texts) or any(
            not isinstance(row, list) for row in ids):
        raise _WindowSkip("tokenization_unavailable")
    return [len(row) for row in ids]


def _passage_spans(tokenizer, query, text, pair_tokens, capacity):
    """Exact Unicode source slices, including whitespace, with fixed token overlap.

    A fitting whole pair needs no offset mapping. An overlong source must allow
    positive progress while retaining 64 source-content tokens; a query leaving
    too little capacity is skipped rather than silently reducing the overlap.
    """
    if pair_tokens <= 512:
        return [{"start": 0, "end": len(text), "pair_tokens": pair_tokens}]
    if capacity <= WINDOW_OVERLAP:
        raise _WindowSkip("query_too_long")
    if not getattr(tokenizer, "is_fast", False):
        raise _WindowSkip("tokenization_unavailable")
    encoded = tokenizer([text], padding=False, truncation=False, add_special_tokens=False,
                        return_offsets_mapping=True, verbose=False)
    rows, offsets = encoded["input_ids"], encoded["offset_mapping"]
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], list)
            or not isinstance(offsets, list) or len(offsets) != 1
            or not isinstance(offsets[0], list) or len(offsets[0]) != len(rows[0])):
        raise _WindowSkip("tokenization_unavailable")
    offsets = offsets[0]
    if not offsets:
        raise _WindowSkip("tokenization_unavailable")
    previous = (0, 0)
    for span in offsets:
        if (not isinstance(span, (tuple, list)) or len(span) != 2
                or any(type(value) is not int for value in span)
                or not 0 <= span[0] < span[1] <= len(text)
                or span[0] < previous[0] or span[1] < previous[1]):
            raise _WindowSkip("tokenization_unavailable")
        previous = span
    windows, first, covered = [], 0, 0
    while first < len(offsets):
        last = min(len(offsets), first + capacity)
        start = 0 if not windows else offsets[first][0]
        if start > covered:
            raise _WindowSkip("tokenization_unavailable")
        while last > first:
            end = len(text) if last == len(offsets) else offsets[last - 1][1]
            tokens = _pair_lengths(tokenizer, query, [text[start:end]])[0]
            if tokens <= 512:
                break
            # Boundary retokenization may grow the pair. Shrink deterministically
            # by at least the overflow, then count the exact new substring again.
            last -= max(1, tokens - 512)
        if last <= first or end <= start or end <= covered:
            raise _WindowSkip("tokenization_unavailable")
        windows.append({"start": start, "end": end, "pair_tokens": tokens})
        if len(windows) > MAX_WINDOWS:
            raise _WindowSkip("window_budget_exceeded")
        covered = end
        if last == len(offsets):
            break
        next_first = last - WINDOW_OVERLAP
        if next_first <= first:
            raise _WindowSkip("query_too_long")
        first = next_first
    if covered != len(text):
        raise _WindowSkip("tokenization_unavailable")
    return windows


def _score_passages(model, query, texts, identity):
    """Score all exact windows only after complete bounded request preflight.

    Candidate max logits express model relevance, never support or confidence.
    No text or candidate is silently clipped, and a skipped request is unscored.
    """
    cpu_started = time.process_time()
    if (not isinstance(query, str) or len(query.encode("utf-8")) > MAX_TEXT_BYTES
            or not isinstance(texts, list) or not 1 <= len(texts) <= MAX_PAIRS
            or any(not isinstance(t, str) or len(t.encode("utf-8")) > MAX_TEXT_BYTES for t in texts)):
        raise ValueError
    result = {"scores": [], "windows": [], "window_count": 0, "truncated_count": 0,
              "mode": DOCUMENT_WINDOW_MODE}
    try:
        lengths = _pair_lengths(model.tokenizer, query, texts)
        capacity = None
        if any(length > 512 for length in lengths):
            query_tokens = _pair_lengths(model.tokenizer, query, [""])[0]
            capacity = 512 - query_tokens
            if capacity <= WINDOW_OVERLAP:
                raise _WindowSkip("query_too_long")
        planned, count = [], 0
        for text, length in zip(texts, lengths, strict=True):
            spans = _passage_spans(model.tokenizer, query, text, length, capacity)
            count += len(spans)
            if count > MAX_WINDOWS:
                raise _WindowSkip("window_budget_exceeded")
            planned.append(spans)
    except _WindowSkip as exc:
        result["skipped_reason"] = exc.reason
    except Exception:
        result["skipped_reason"] = "tokenization_unavailable"
    else:
        pairs = [(query, text[span["start"]:span["end"]])
                 for text, spans in zip(texts, planned, strict=True) for span in spans]
        values = model.predict(pairs, batch_size=16, show_progress_bar=False,
                               activation_fn=identity, convert_to_numpy=True)
        scores = [float(value) for value in values]
        if len(scores) != count or any(not math.isfinite(value) for value in scores):
            raise ValueError
        offset = 0
        for spans in planned:
            for span in spans:
                span["score"] = scores[offset]
                offset += 1
        result.update(scores=[max(span["score"] for span in spans) for spans in planned],
                      windows=planned, window_count=count)
    result["cpu_ms"] = (time.process_time() - cpu_started) * 1000
    return result


def _dispatch_score(model, request, identity):
    mode = request.get("mode", "whole")
    if mode == "whole":
        return _score(model, request.get("query"), request.get("texts"), identity)
    if mode == DOCUMENT_WINDOW_MODE:
        return _score_passages(model, request.get("query"), request.get("texts"), identity)
    raise ValueError("Unknown reranker request mode")


def main():
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    # Keep Python and native library stdout logs outside the IPC channel.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    _offline()

    request = None

    def send(result):
        result = {**result, "request_id": request.get("request_id") if request else None}
        protocol.write(json.dumps(result, allow_nan=False, separators=(",", ":")).encode() + b"\n")
        protocol.flush()

    try:
        init = request = _read()
        if init is None:
            return
        path = _verify_model(init.get("model_path"), init.get("model_files"))
        import torch
        from sentence_transformers import CrossEncoder

        torch.set_num_threads(2)
        torch.set_num_interop_threads(2)
        identity = torch.nn.Identity()
        model = CrossEncoder(path, local_files_only=True, trust_remote_code=False,
                             token=False, device="cpu", max_length=512,
                             model_kwargs={"use_safetensors": True},
                             activation_fn=identity)
        model.model.eval()
        send({"ready": True})
        while True:
            request = _read()
            if request is None:
                return
            send(_dispatch_score(model, request, identity))
    except ModelMismatch:
        send({"error": "model_mismatch"})
    except Exception:
        send({"error": "unavailable"})


if __name__ == "__main__":
    main()
