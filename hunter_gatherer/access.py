"""Limits a trusted host places on what one caller may do with a library.

h/g does not authenticate anyone, manage accounts or decide who belongs to a
library. A host application does that, picks the library a request is for, and
may then narrow what the request can do with an :class:`AccessPolicy`.

The policy comes from host code: as a harness view (:func:`restricted_view`) or
around a call (:func:`access_scope`). It is never read from tool arguments, so a
model cannot grant itself anything. Without a policy, behaviour is the trusted
single-user default: everything is allowed, exactly as before.

What the tool arguments *can* still say remains advisory. ``include_sensitive``
is a request, honoured only where the policy allows sensitive reads. Actor and
workspace labels, tool profiles and ``source`` strings organize records; they
are not identities and grant nothing.
"""
from __future__ import annotations

import os
from collections.abc import Collection, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

READ = "read"
WRITE = "write"
IMPORT = "import_files"
ADMINISTER = "administer"

_ORDER = (READ, WRITE, IMPORT, ADMINISTER)

# What each tool action needs. A tool family that mixes reading and writing is
# classified action by action. Anything not listed needs ADMINISTER, so a tool
# added later is unavailable to restricted callers until it is classified.
_TOOLS: dict[str, str] = {
    # Six-tool library surface (single-purpose members).
    "hg_find": READ, "hg_open": READ, "hg_status": READ,
    # Reads in the full catalogue.
    "hg_profile": READ, "hg_recall": READ, "hg_search": READ, "hg_evidence": READ,
    "hg_context_packet": READ, "hg_fetch": READ, "hg_core_memory": READ,
    "hg_get_memory": READ, "hg_list_memories": READ, "hg_memory_history": READ,
    "hg_search_documents": READ, "hg_get_document": READ, "hg_get_document_chunk": READ,
    "hg_list_documents": READ, "hg_document_outline": READ, "hg_document_window": READ,
    "hg_document_page_image": READ, "hg_check_attribution": READ, "hg_library_health": READ,
    "hg_list_import_candidates": READ, "hg_get_import_candidate": READ,
    # Changes to records already in the library.
    "hg_remember": WRITE, "hg_replace_memory": WRITE, "hg_forget": WRITE,
    "hg_set_core_memory": WRITE, "hg_forget_document": WRITE, "hg_propose_import": WRITE,
    "hg_review_import_candidate": WRITE, "hg_retrieval_feedback": WRITE,
    # Reading files named by the caller from the server's own filesystem.
    "hg_ingest_document": IMPORT, "hg_reextract_document": IMPORT,
}
_ACTIONS: dict[str, dict[str, str]] = {
    "hg_memory": {"profile": READ, "history": READ, "remember": WRITE, "replace": WRITE, "forget": WRITE},
    "hg_collection": {"list": READ, "get": READ, "export": READ, "refs": READ, "save": WRITE},
    "hg_import_documents": {"preview": IMPORT, "create": IMPORT, "run": IMPORT, "status": READ,
                            "list": READ, "enrich": WRITE, "enrichments": READ,
                            "revoke_enrichment": WRITE},
    "hg_document_enrichment": {"attach": WRITE, "list": READ, "revoke": WRITE},
    "hg_board": {"read": READ, "update": WRITE, "create_group": WRITE},
    "hg_claim": {"record": WRITE, "query": READ, "resolve": WRITE, "history": READ},
    "hg_workspace": {"create": WRITE, "update": WRITE, "get": READ, "list": READ},
    "hg_task": {"create": WRITE, "get": READ, "list": READ, "history": READ, "update": WRITE,
                "claim": WRITE, "renew": WRITE, "release": WRITE, "checkpoint": WRITE,
                "complete": WRITE},
    "hg_handoff": {"offer": WRITE, "get": READ, "accept": WRITE, "reject": WRITE},
    "hg_artifact": {"publish": WRITE, "get": READ, "collection_refs": READ, "list": READ,
                    "collection_save": WRITE, "collection_get": READ, "collection_list": READ,
                    "collection_export": READ},
    "hg_message": {"send": WRITE, "inbox": READ, "ack": WRITE},
    "hg_coordination": {"changes": READ, "snapshot": READ},
    "hg_procedure": {"create": WRITE, "revise": WRITE, "quarantine": WRITE, "deprecate": WRITE,
                     "get": READ, "list": READ, "recommend": READ},
    "hg_procedure_outcome": {"record": WRITE, "list": READ},
}
_DEFAULT_ACTION = {"hg_import_documents": "preview"}
# Tools that act on a record by ID, or return its wording, without a per-call
# sensitive opt-in and so without checking whether the caller could read it. A
# caller who may not read sensitive records cannot use them directly. The
# six-tool family (hg_memory) checks visibility first and remains available.
_NO_VISIBILITY_CHECK = frozenset({"hg_replace_memory", "hg_forget", "hg_set_core_memory",
                                  "hg_forget_document", "hg_ingest_document"})


def required_capability(tool: str, arguments: dict | None = None) -> str:
    """The capability one tool call needs, judged by its action, never its name alone."""
    actions = _ACTIONS.get(tool)
    if actions is None:
        return _TOOLS.get(tool, ADMINISTER)
    action = (arguments or {}).get("action", _DEFAULT_ACTION.get(tool))
    if action in actions:
        return actions[action]
    # An unrecognized action is refused by the handler. Before it gets there,
    # require what the family's most demanding action needs, and never less
    # than write access: an action added later is not assumed to be a read.
    return max([WRITE, *actions.values()], key=_ORDER.index)


@dataclass(frozen=True)
class AccessPolicy:
    """What a host allows one caller to do. Reading ordinary records is implied.

    ``read_sensitive``  honour ``include_sensitive=True``; otherwise such calls are refused
    ``write``           change memories, collections, annotations and coordination records
    ``import_files``    read files from the server's filesystem into the library
    ``administer``      backups, maintenance, the agent runtime and unclassified tools;
                        also needed to see where the database lives on disk
    ``import_roots``    if set, imports may only read files below these directories
    ``principal``       the host's name for the caller, recorded as ``recorded_by`` on
                        memories it writes. Not used for any decision here.
    """

    read_sensitive: bool = True
    write: bool = True
    import_files: bool = True
    administer: bool = True
    import_roots: tuple[str, ...] = ()
    principal: str = ""

    def __post_init__(self):
        for name in ("read_sensitive", "write", "import_files", "administer"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")
        if not isinstance(self.principal, str) or len(self.principal) > 256:
            raise TypeError("principal must be a string of at most 256 characters")
        if not isinstance(self.import_roots, Collection) or isinstance(
            self.import_roots, (str, bytes, bytearray, memoryview, os.PathLike, Mapping)
        ):
            raise TypeError("import_roots must be a collection of directory paths")
        roots = []
        for root in self.import_roots:
            path = os.fspath(root)
            if not isinstance(path, str) or not path:
                raise ValueError("each import root must be a nonempty text path")
            roots.append(os.path.realpath(os.path.expanduser(path)))
        object.__setattr__(self, "import_roots", tuple(roots))

    @property
    def unrestricted(self) -> bool:
        return (self.read_sensitive and self.write and self.import_files and self.administer
                and not self.import_roots)

    def allows(self, capability: str) -> bool:
        return capability == READ or bool(getattr(self, capability, False))

    def narrowed_by(self, other: AccessPolicy) -> AccessPolicy:
        """Both policies apply: allowed only where each allows it."""
        if not self.import_roots or not other.import_roots:
            roots = self.import_roots or other.import_roots
        else:
            roots = tuple(root for root in self.import_roots if other._within_roots(root)) + tuple(
                root for root in other.import_roots if self._within_roots(root))
            # Disjoint folders leave nothing importable; say so with a root nothing is under.
            roots = roots or (os.devnull,)
        return AccessPolicy(
            read_sensitive=self.read_sensitive and other.read_sensitive,
            write=self.write and other.write,
            import_files=self.import_files and other.import_files,
            administer=self.administer and other.administer,
            import_roots=tuple(dict.fromkeys(roots)), principal=self.principal or other.principal)

    def _within_roots(self, path: str) -> bool:
        resolved = Path(os.path.realpath(path))
        return any(resolved == Path(root) or Path(root) in resolved.parents for root in self.import_roots)

    def check_import_path(self, path: str | os.PathLike) -> None:
        """Refuse a server-side path this caller may not read into the library."""
        if not self.import_files:
            raise PermissionError("This connection may not import files.")
        if self.import_roots and not self._within_roots(os.path.expanduser(str(path))):
            raise PermissionError("That path is outside the folders this connection may import from.")

    def denial(self, tool: str, arguments: dict | None = None) -> str | None:
        """Why this call is refused, or None when it may proceed."""
        arguments = arguments or {}
        capability = required_capability(tool, arguments)
        if not self.allows(capability):
            return _REFUSALS[capability]
        if not self.read_sensitive and (arguments.get("include_sensitive") is True
                                        or tool in _NO_VISIBILITY_CHECK):
            return "This connection may not read sensitive records."
        return None


_REFUSALS = {
    WRITE: "This connection is read-only.",
    IMPORT: "This connection may not import files.",
    ADMINISTER: "This connection may not use administrative tools.",
}

LOCAL = AccessPolicy()
"""The trusted single-user default: nothing is restricted."""

_scoped: ContextVar[AccessPolicy | None] = ContextVar("hg_access_policy", default=None)
_dispatching: ContextVar[bool] = ContextVar("hg_dispatching", default=False)


@contextmanager
def access_scope(policy: AccessPolicy):
    """Apply ``policy`` to every h/g call made inside this block.

    For a host that calls h/g in-process on behalf of an authenticated user.
    The policy follows the current task and the worker threads it starts
    through asyncio or anyio. Nested scopes narrow; they never widen.
    """
    if not isinstance(policy, AccessPolicy):
        raise TypeError("access_scope needs an AccessPolicy")
    outer = _scoped.get()
    token = _scoped.set(policy if outer is None else policy.narrowed_by(outer))
    try:
        yield
    finally:
        _scoped.reset(token)


def effective_policy(harness=None) -> AccessPolicy:
    """The policy in force: the harness's own, narrowed by any request scope."""
    scoped = _scoped.get()
    base = getattr(harness, "access_policy", None) if harness is not None else None
    if scoped is not None and base is not None:
        return scoped.narrowed_by(base)
    return scoped or base or LOCAL


def current_policy() -> AccessPolicy:
    """The policy of the request being served, for checks below the tool layer."""
    return _scoped.get() or LOCAL


class PolicyView:
    """One harness seen through a narrower policy; the store and state are shared."""

    def __init__(self, harness, policy: AccessPolicy):
        object.__setattr__(self, "_harness", harness)
        object.__setattr__(self, "access_policy", policy)

    def __getattr__(self, name):
        return getattr(self._harness, name)

    def __setattr__(self, name, value):
        # Per-harness registries (activity, maintenance, admission) stay shared.
        setattr(self._harness, name, value)


def restricted_view(harness, policy: AccessPolicy) -> PolicyView:
    """A view of ``harness`` for callers the host trusts less.

    Build one MCP server per view (``build_server(harness=view)``) and route
    each authenticated user to the endpoint that matches their role. The view
    can only narrow the policy the harness already has.
    """
    if not isinstance(policy, AccessPolicy):
        raise TypeError("restricted_view needs an AccessPolicy")
    base = getattr(harness, "access_policy", None)
    return PolicyView(harness, policy if base is None else policy.narrowed_by(base))


@contextmanager
def dispatching(harness):
    """Enter a tool call. Yields the refusal check for the outermost call only.

    A tool family calls other handlers on the caller's behalf. Those inner
    calls belong to the operation already admitted, so they are not judged
    again as if the client had named them.
    """
    outermost = not _dispatching.get()
    policy = effective_policy(harness)
    marker = _dispatching.set(True)
    scope = _scoped.set(policy)
    try:
        yield (policy.denial if outermost and not policy.unrestricted else None)
    finally:
        _scoped.reset(scope)
        _dispatching.reset(marker)
