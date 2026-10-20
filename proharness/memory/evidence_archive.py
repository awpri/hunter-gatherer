"""Reuse parent-text hydration only inside one evidence read snapshot."""
from collections import OrderedDict
from contextlib import contextmanager

from proharness.memory.documents import DocumentArchive

_MAX_ENTRIES = 128
_MAX_BYTES = 1024 * 1024


class EvidenceArchive(DocumentArchive):
    @contextmanager
    def reuse_parent_context(self, *, include_context=True):
        # This instance belongs to one evidence request. The caller pins its
        # read snapshot first; never retain bodies across requests or versions.
        if not self.store._sqlite.in_transaction:
            raise ValueError("Parent context reuse requires a read snapshot")
        self._include_context = include_context
        self._parent_context_cache = OrderedDict()
        self._parent_context_bytes = 0
        try:
            yield
        finally:
            self._include_context = True
            self._parent_context_cache = None
            self._parent_context_bytes = 0

    def _hit(self, row, *, include_context=True):
        if not getattr(self, "_include_context", True):
            hit = super()._hit(row, include_context=False)
            hit.pop("context")
            hit.pop("context_location")
            hit["context_omitted"] = True
            return hit
        cache = getattr(self, "_parent_context_cache", None)
        if cache is None or not include_context:
            return super()._hit(row, include_context=include_context)
        # Authoritative search/get_chunk still resolves row visibility and
        # source coordinates on every call; no access decision is cached.
        key = (row["document_id"], row["section_id"], row["start"], row["end"])
        if key in cache:
            text, location, _ = cache[key]
            cache.move_to_end(key)
            hit = super()._hit(row, include_context=False)
            hit.update(context=text, context_location=dict(location))
            return hit
        hit = super()._hit(row, include_context=True)
        size = len(hit["context"].encode("utf-8"))
        if size <= _MAX_BYTES:
            while cache and (len(cache) >= _MAX_ENTRIES or self._parent_context_bytes + size > _MAX_BYTES):
                _, (_, _, removed) = cache.popitem(last=False)
                self._parent_context_bytes -= removed
            cache[key] = (hit["context"], dict(hit["context_location"]), size)
            self._parent_context_bytes += size
        return hit
