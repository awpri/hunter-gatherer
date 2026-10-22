"""Quote-first terminal views over the same Find/Open contract as MCP.

Only the human presentation selects excerpts. JSON retains full returned quotes,
provenance, omissions and access decisions; neither path generates an answer.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import sqlite3
import sys
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from rich.console import Console
from rich.padding import Padding
from rich.text import Text

from proharness.config import Settings

_STOP = set("the and for that this with from what when where which who whom whose why how was were are does did has have had can could would should about into than then there their they them these those your you our its any all more find show tell give please looking need know".split())  # noqa: SIM905
_STATUS = r"(?:real|genuine|authentic|fiction(?:al)?|fictitious|invented|fabricated|fake|made[\s-]+up|simulated|synthetic|demo(?:nstration)?|mock|sample)"
_SUBJECT = r"(?:data|datasets?|records?|sources?|documents?|projects?|companies|company|people|persons?|examples?|scenarios?|reports?|events?|case\s+stud(?:y|ies))"


def safe_text(value) -> str:
    """Make terminal controls and directional overrides visible, not executable.

    Rich markup is disabled separately. JSON uses its own lossless escaping.
    """
    return "".join(
        f"\\u{ord(char):04x}" if (unicodedata.category(char) in {"Cc", "Cf"}
                                  and char not in "\n\t") else char
        for char in str(value or "")
    )


def _authenticity_question(query: str) -> bool:
    wording = re.sub(r"\breal[\s-]+time\b", "realtime", query.lower())
    return any(re.search(pattern, wording, re.I) for pattern in (
        r"\b(?:authenticity|fictionality)\b|\b(?:actually|really)\s+exist\b",
        rf"^(?:is|are|was|were|isn['’]t|aren['’]t|wasn['’]t|weren['’]t)\b.+\b{_STATUS}\b(?:[\s?!.,]*(?:or\b.*)?)?$",
        rf"\b(?:whether|if)\b.+\b{_STATUS}\b(?:[\s?!.,]*(?:or\b.*)?)?$",
        rf"^(?:is|are|was|were|does|do)\b.+\b{_STATUS}\s+{_SUBJECT}\b",
    ))


def _authenticity_wording(text: str) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in (
        r"\b(?:fictional|fictitious|invented|fabricated|fake|genuine|authentic|made[\s-]+up)\b",
        r"\b(?:synthetic|simulated|mock|sample|demo(?:nstration)?|training|test)\s+(?:data(?:sets?)?|records?|sources?|documents?|projects?|examples?|scenarios?|reports?|events?|case\s+stud(?:y|ies))\b",
        r"\b(?:data(?:sets?)?|records?|examples?|scenarios?)\s+(?:(?:is|are|was|were)\s+)?(?:(?:not|entirely|all|artificially)\s+)*(?:synthetic|simulated|generated|mock)\b",
        r"\b(?:for|as)\s+(?:(?:a|an)\s+)?(?:demo(?:nstration)?|training|testing)\b",
        rf"\breal(?:[\s-]+world)?\s+{_SUBJECT}\b",
        r"\b(?:is|are|was|were)\s+(?:(?:not|entirely|all)\s+)*(?:real|genuine|authentic)\b",
    ))


@dataclass(frozen=True)
class Excerpt:
    text: str
    leading: bool = False
    trailing: bool = False
    focus_start: int = 0
    focus_end: int = 0


def passage_excerpt(quote: str, query: str) -> Excerpt:
    """Select a contiguous source window and emphasize its matching sentence.

    Mirrors the browser's display heuristic: repeated subject names identify the
    document, rare question terms identify prose. Neighboring qualifications and
    negations remain visible. No match means no inferred answer or verdict.
    """
    full = Excerpt(quote, focus_end=len(quote))
    if len(quote) <= 140:
        return full
    terms = set(re.findall(r"[^\W_]{3,}", query.lower())) - _STOP
    if not terms:
        return full
    authenticity = _authenticity_question(query)
    boundaries = sorted({match.end() for pattern in (
        r"[.!?][\"”’')\]]*(?=\s|$)|\n{2,}", r"^#{1,6}[^\n]*(?:\n|$)",
    ) for match in re.finditer(pattern, quote, re.M)} | {len(quote)})
    units, start = [], 0
    for end in boundaries:
        text = quote[start:end]
        if text.strip():
            units.append({
                "start": start, "end": end,
                "matches": terms & set(re.findall(r"[^\W_]{3,}", text.lower())),
                "structural": bool(re.search(r"^\s*#{1,6}\s|^\s*(?:date|author|title|version)\s*:[^\n]*\s*$", text, re.I)),
                "status": authenticity and _authenticity_wording(text),
            })
        start = end
    frequencies = {term: sum(term in unit["matches"] for unit in units) for term in terms}
    body_match = any(not unit["structural"] and (unit["matches"] or unit["status"]) for unit in units)
    best, best_score = -1, 0
    for index, unit in enumerate(units):
        score = sum(1 / frequencies[term] for term in unit["matches"])
        score += len(terms) + 1 if unit["status"] else 0
        if unit["structural"] and body_match:
            score = 0
        if score > best_score:
            best, best_score = index, score
    if best < 0:
        return full
    first, last = max(0, best - 1), min(len(units) - 1, best + 1)
    while first < best and units[first]["structural"]:
        first += 1
    while last > best and units[last]["structural"]:
        last -= 1
    start, end = units[first]["start"], units[last]["end"]
    while start < end and quote[start].isspace():
        start += 1
    while end > start and quote[end - 1].isspace():
        end -= 1
    focus_start, focus_end = units[best]["start"], units[best]["end"]
    while focus_start < focus_end and quote[focus_start].isspace():
        focus_start += 1
    while focus_end > focus_start and quote[focus_end - 1].isspace():
        focus_end -= 1
    return Excerpt(quote[start:end], bool(quote[:start].strip()), bool(quote[end:].strip()),
                   focus_start - start, focus_end - start)


def _location(item: dict) -> str:
    location = item.get("location") or {}
    section, title, version = location.get("section", ""), item.get("title", ""), item.get("version")
    parts = [f"Page {location['page']}"] if location.get("page") else []
    if section and section != title:
        parts.append(section)
    if version and not re.search(rf"\b(?:version|v)\s*{re.escape(str(version))}\b", f"{section} {title}", re.I):
        parts.append(f"Version {version}")
    if item.get("is_latest") is False:
        parts.append("Historical")
    return " · ".join(parts)


def _quote(console: Console, quote: str, question: str = "") -> bool:
    excerpt = passage_excerpt(quote, question)
    text = Text()
    if excerpt.leading:
        text.append("…\n", style="dim")
    text.append(safe_text(excerpt.text[:excerpt.focus_start]), style="dim")
    text.append(safe_text(excerpt.text[excerpt.focus_start:excerpt.focus_end]), style="bold")
    text.append(safe_text(excerpt.text[excerpt.focus_end:]), style="dim")
    if excerpt.trailing:
        text.append("\n…", style="dim")
    console.print(Padding(text, (0, 0, 0, 2), expand=False))
    return safe_text(quote) != quote


def _source(console: Console, item: dict, number: int, question: str = "", *, memory=False) -> bool:
    title = item.get("title") or ("Saved memory" if memory else "Source")
    console.print(Text(f"{number}. {safe_text(title)}", style="bold cyan"))
    location = _location(item)
    if memory:
        location = f"Saved memory · {item.get('verification', 'unverified')}"
    if location:
        console.print(Text(safe_text(location), style="dim"))
    quote = item.get("quote", item.get("text", ""))
    changed = _quote(console, quote, question)
    if not quote:
        console.print(Text("No quotation in this response. Use --view outline to browse this document.", style="dim"))
    console.print(Text(safe_text(item.get("citation", "")), style="dim"), soft_wrap=True)
    if item.get("warnings") or item.get("source_metadata_warnings"):
        console.print(Text("Source has extraction or metadata warnings; inspect --json before relying on it.", style="yellow"))
    console.print()
    return changed


def render_ask(result: dict, console: Console) -> None:
    passages, memories, leads = [], [], []
    seen = set()
    # Prefer full returned bodies over a catalog/linked reference to the same item.
    for page in result["pages"]:
        for key, target in (("supporting_passages", passages), ("relevant_memories", memories)):
            for item in page.get(key, []):
                if item.get("citation") not in seen:
                    target.append(item)
                    seen.add(item.get("citation"))
    documents = {item.get("document_id") for item in passages}
    for page in result["pages"]:
        candidates = [*page.get("source_leads", []), *page.get("omitted_references", []),
                      *page.get("source_discovery", {}).get("candidates", []),
                      *page.get("memory_discovery", {}).get("candidates", []),
                      *page.get("visual_regions", [])]
        for item in candidates:
            ref = item.get("citation") or item.get("original_citation")
            if not ref or ref in seen or item.get("kind") == "document_catalog" and item.get("document_id") in documents:
                continue
            leads.append(item)
            seen.add(ref)
    console.print(Text("hunter/gatherer · source quotes", style="dim"))
    console.print(Text(safe_text(result["question"]), style="bold"))
    count = f"{len(passages)} passage{'s' if len(passages) != 1 else ''} · {len(memories)} saved memor{'y' if len(memories) == 1 else 'ies'}"
    console.print(Text(count + (f" · {len(leads)} source lead{'s' if len(leads) != 1 else ''}" if leads else ""), style="dim"))
    console.print()
    changed = False
    for index, item in enumerate(passages + memories, 1):
        changed |= _source(console, item, index, result["question"], memory=index > len(passages))
    if not passages and not memories:
        console.print(Text("No matching quotes returned. Try another wording or inspect the source leads.", style="yellow"))
    if leads:
        console.print(Text("More sources to inspect", style="bold"))
        for item in leads:
            console.print(Text(safe_text(item.get("title") or "Source reference")))
            console.print(Text(safe_text(item.get("citation") or item.get("original_citation")), style="dim"), soft_wrap=True)
        console.print()
    if changed:
        console.print(Text("Control characters are shown as escapes; --json retains the original text.", style="dim"))
    if any(page.get("retrieval", {}).get("warnings") or
           page.get("retrieval", {}).get("document_search", {}).get("warnings")
           for page in result["pages"]):
        console.print(Text("Search reported limitations; see --json for details.", style="yellow"))
    if result["has_more"]:
        hint = (f"Rerun with --pages {len(result['pages']) + 1} for more matches."
                if len(result["pages"]) < 20 else "Page limit reached; narrow the question for more focused results.")
        console.print(Text(hint, style="yellow"))
    console.print(Text('Open full source: hunt open "<citation>" with the same library option.', style="dim"))
    console.print(Text("Source quotes, not a generated answer. --json for provenance and search details.", style="dim"))


def render_open(result: dict, console: Console) -> None:
    body = result["result"]
    console.print(Text("hunter/gatherer · source", style="dim"))
    info = body.get("source_info", {})
    for index, source in enumerate(body.get("sources", body.get("chunks", [])), 1):
        _source(console, {**info, **source}, index, memory=source.get("kind") == "memory")
    if "sections" in body:
        console.print(Text(safe_text(info.get("title") or "Document outline"), style="bold"))
        location = _location({**info, **{key: body[key] for key in ("version", "is_latest") if key in body}})
        if location:
            console.print(Text(safe_text(location), style="dim"))
        for section in body["sections"]:
            label = section.get("section") or section.get("title") or "Section"
            if section.get("page"):
                label = f"Page {section['page']} · {label}"
            console.print(Text(safe_text(label)))
            followup = section.get("followup", {}).get("arguments", {})
            for ref in followup.get("refs", []):
                console.print(Text(safe_text(ref), style="dim"), soft_wrap=True)
        if body.get("next_offset") is not None:
            console.print(Text(f"More sections: rerun with --offset {body['next_offset']}.", style="yellow"))
    for index, status in enumerate(body.get("statuses", [])):
        if status != "available":
            console.print(Text(f"Reference {index + 1}: {safe_text(status)}" +
                               ("; increase --max-tokens." if status == "omitted" else "."), style="yellow"))
    if body.get("error"):
        console.print(Text(safe_text(body["error"]), style="yellow"))
    if (body.get("omitted") or body.get("omitted_count") or body.get("truncated")
            or body.get("omitted_counts", {}).get("output_budget")):
        console.print(Text("Some context was omitted; increase --max-tokens or inspect --json for navigation.", style="yellow"))
    if any(body.get(key) for key in ("warnings", "extraction_warnings_unavailable", "extraction_warnings_omitted")):
        console.print(Text("Source extraction has warnings or incomplete warning details; inspect --json.", style="yellow"))


def _existing_settings(args, settings: Settings | None) -> Settings:
    if args.demo_library:
        from proharness.demo import demo_settings
        return demo_settings(args.demo_library)
    assert settings is not None
    path = Path(args.database or settings.sqlite_path).expanduser()
    if args.database is None and not path.is_absolute():
        path = Path.home() / ".proharness" / path
    if not path.is_file():
        raise ValueError("Library database does not exist. Select --database FILE, or create an isolated demo with hunt demo create DIR and use --demo-library DIR.")
    return settings.model_copy(update={"sqlite_path": str(path.resolve(strict=True))})


@contextlib.contextmanager
def _source_reader(settings: Settings):
    """Exact source reading does not initialize embeddings or migrate a database."""
    from proharness.memory.schemas import Memory

    connection = sqlite3.connect(Path(settings.sqlite_path).as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(memories)")]
        required = ["id", "text", "type", "tags", "importance", "created_at", "last_accessed",
                    "access_count", "source", "metadata"]
        metadata = [row["name"] for row in connection.execute("PRAGMA table_info(store_meta)")]
        if (columns[:10] != required or (len(columns) > 10 and columns[10] != "deleted_at")
                or metadata and metadata != ["key", "value"]):
            raise ValueError("Selected database is not an initialized evidence library")
        def get(memory_id):
            row = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            return Memory.from_sql_row(tuple(row)) if row is not None else None
        yield SimpleNamespace(_sqlite=connection, _lock=threading.RLock(), settings=settings, get=get)
    finally:
        connection.close()


def _server_endpoint(value: str) -> str:
    """Select one explicit loopback origin; never resolve arbitrary hostnames."""
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
        if (url.scheme not in {"http", "https"} or not host
                or url.username is not None or url.password is not None
                or url.path not in {"", "/"} or url.query or url.fragment
                or any(char.isspace() or ord(char) < 32 for char in value)):
            raise ValueError
        if host == "localhost":
            host = "127.0.0.1"
        address = ipaddress.ip_address(host)
        if not address.is_loopback or "%" in host:
            raise ValueError
    except ValueError as exc:
        raise ValueError("--server requires a loopback HTTP(S) origin, such as http://127.0.0.1:8767, without credentials, a path, query or fragment.") from exc
    authority = f"[{host}]" if address.version == 6 else host
    if port is not None:
        authority += f":{port}"
    return f"{url.scheme}://{authority}/mcp/library"


async def _reject_server_redirect(response):
    # The MCP SDK follows same-origin redirects itself, regardless of the HTTP
    # client's follow_redirects setting. Reject before that transport can follow.
    if 300 <= response.status_code < 400:
        raise ValueError("The selected library redirected the request; use its direct loopback origin.")


def _server_http_client():
    import httpx2

    return httpx2.AsyncClient(trust_env=False, follow_redirects=False,
                             timeout=httpx2.Timeout(60, connect=5),
                             event_hooks={"response": [_reject_server_redirect]})


def _server_payload(response, operation: str) -> dict:
    """Require the structured Find/Open contract, never interpret source text."""
    body = response.structured_content
    if not isinstance(body, dict):
        raise ValueError("The selected server did not return a structured library response. Run a compatible h/g library server.")
    if response.is_error or body.get("error"):
        raise ValueError("The selected library could not complete the request: " + str(body.get("error") or "tool error"))
    if operation == "ask":
        valid = (all(isinstance(body.get(key), list) for key in ("supporting_passages", "relevant_memories"))
                 and isinstance(body.get("continuation"), dict)
                 and (body["continuation"].get("next_cursor") is None
                      or isinstance(body["continuation"].get("next_cursor"), str)))
    else:
        valid = any(isinstance(body.get(key), list) for key in ("sources", "chunks", "sections"))
    if not valid:
        raise ValueError("The selected server returned an incompatible library response.")
    return body


async def _server_evidence(args) -> tuple[dict, int]:
    """Use the running library's store/model through its existing MCP interface."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from proharness.memory.output_budget import budget_report

    endpoint = _server_endpoint(args.server)
    try:
        async with (
            _server_http_client() as http,
            streamable_http_client(endpoint, http_client=http) as streams,
            ClientSession(streams[0], streams[1], read_timeout_seconds=60) as client,
        ):
            await client.initialize()
            common = {"max_tokens": args.max_tokens, "include_sensitive": args.include_sensitive}
            if args.cmd == "open":
                body = _server_payload(await client.call_tool("proharness_open", {
                    **common, "refs": args.refs, "view": args.view, "offset": args.offset,
                }), "open")
                return {"schema_version": 1, "kind": "open", "result": body}, int(
                    any(status != "available" for status in body.get("statuses", [])))
            pages, cursor, identity = [], None, None
            for _ in range(args.pages):
                arguments = {**common, **({"cursor": cursor} if cursor else {"query": args.question})}
                page = _server_payload(await client.call_tool("proharness_find", arguments), "ask")
                store = page.get("store", {}).get("id")
                if pages and store != identity:
                    raise ValueError("The selected library changed during the search; rerun the question.")
                identity = store
                cursor = page["continuation"].get("next_cursor")
                # Keep the same CLI envelope for local and server use;
                # consume continuations here, not across invocations.
                page["continuation"]["next_cursor"] = None
                budget_report(page, args.max_tokens)
                pages.append(page)
                if not cursor:
                    break
            return {"schema_version": 1, "kind": "ask", "question": args.question,
                    "pages": pages, "has_more": bool(cursor)}, 0
    except Exception as exc:
        # AnyIO wraps transport errors in exception groups. Keep the actionable
        # cause visible rather than "unhandled errors in a TaskGroup".
        reason = exc
        while isinstance(reason, BaseExceptionGroup) and reason.exceptions:
            reason = reason.exceptions[0]
        raise RuntimeError(f"Could not use the selected library at {endpoint}: {reason}. No local library was opened.") from exc


def cmd_evidence(args, settings: Settings | None) -> int:
    if getattr(args, "server", None) is not None:
        with contextlib.redirect_stdout(sys.stderr):
            result, code = asyncio.run(_server_evidence(args))
    else:
        result, code = _local_evidence(args, settings)
    if args.json:
        # Default ASCII escaping preserves arbitrary source text losslessly without
        # emitting terminal escape sequences, bidi controls or C1 controls.
        print(json.dumps(result, separators=(",", ":")))
    else:
        output = Console(markup=False, highlight=False)
        (render_ask if args.cmd == "ask" else render_open)(result, output)
    return code


def _local_evidence(args, settings: Settings | None) -> tuple[dict, int]:
    # Libraries/model loaders occasionally print progress; never contaminate JSON.
    with contextlib.redirect_stdout(sys.stderr):
        settings = _existing_settings(args, settings)
        if args.cmd == "ask":
            from proharness.memory.embeddings import get_embedder
            from proharness.memory.output_budget import budget_report
            from proharness.memory.simple_search import find
            from proharness.memory.store import MemoryStore

            # Validate before MemoryStore's schema/cache initialization. A typo or
            # unrelated SQLite file must not become a new library as a read side effect.
            with _source_reader(settings) as reader:
                embedder = get_embedder(settings)
                table = reader._sqlite.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_meta'").fetchone()
                stored = reader._sqlite.execute("SELECT value FROM store_meta WHERE key='embedder_fingerprint'").fetchone() if table else None
                if stored is not None and stored[0] != embedder.fingerprint():
                    raise ValueError("Configured embedding model does not match this library. Use its original PROHARNESS_EMBED_MODEL setting, or explicitly rebuild its vectors after a backup. Ask did not modify the database; hunt open can still read exact citations.")
            # Legacy commands retain their configurable warn/error policy. New
            # Ask always refuses mixed embedding identities rather than poor results.
            settings = settings.model_copy(update={"embedder_mismatch": "error"})
            pages, cursor = [], None
            with MemoryStore(settings, embedder=embedder) as store:
                harness = SimpleNamespace(store=store, settings=settings)
                for _ in range(args.pages):
                    page = find(harness, query=args.question, cursor=cursor,
                                max_tokens=args.max_tokens, include_sensitive=args.include_sensitive)
                    cursor = page.get("continuation", {}).get("next_cursor")
                    # Find cursors live in this store process only. Do not advertise
                    # a dead token after exit; --pages consumes them in this session.
                    page["continuation"]["next_cursor"] = None
                    # Nulling the token shrinks the existing packet. Page metadata
                    # retains its original bounded contract; envelope reports more.
                    budget_report(page, args.max_tokens)
                    pages.append(page)
                    if not cursor:
                        break
            result = {"schema_version": 1, "kind": "ask", "question": args.question,
                      "pages": pages, "has_more": bool(cursor)}
            code = 0
        else:
            from proharness.memory.simple_open import open_sources

            with _source_reader(settings) as store:
                body = open_sources(SimpleNamespace(store=store, settings=settings), args.refs,
                                    view=args.view, offset=args.offset, max_tokens=args.max_tokens,
                                    include_sensitive=args.include_sensitive)
            result = {"schema_version": 1, "kind": "open", "result": body}
            code = int(bool(body.get("error")) or any(status != "available" for status in body.get("statuses", [])))
    return result, code
