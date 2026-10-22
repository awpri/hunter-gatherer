"""Read-only library organization from visible metadata and valid source links.

Groups are browsing aids, never new evidence relationships. Callers provide the
complete visible library within their read snapshot, before search or pagination.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict

from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.evidence import _resolve_sources, is_sensitive_memory

METHOD = "explicit_metadata_and_current_sources"
UNGROUPED = "group:ungrouped"
# These describe storage/record kinds rather than a useful subject. Specific
# explicit tags can organize records whose only category is one of these.
_BROAD = {"personal", "general", "misc", "miscellaneous", "uncategorized", "ungrouped",
          "memory", "memories", "source", "sources", "document", "documents", "imported",
          "fact", "facts", "semantic", "episodic", "procedural"}

# Presentation aliases only: source metadata, tags, and citation voting retain
# their original subjects. Unknown named subjects keep their established IDs.
_TAXONOMY = {
    "profile & background": ("personal", "Profile & background", "user",
                             ("profile", "background", "family responsibility")),
    "education & experience": ("personal", "Education & experience", "graduation-cap",
                              ("education", "employment context", "achievement")),
    "interests & goals": ("personal", "Interests & goals", "compass",
                         ("interest", "interests", "goal", "goals", "activity")),
    "projects": ("work", "Projects", "folder", ("project", "projects", "project context")),
    "decisions": ("work", "Decisions", "check-circle", ("product decision", "project decision", "decision", "decisions")),
    "preferences": ("knowledge", "Preferences", "sliders",
                    ("preference", "preferences", "design preference", "design preferences")),
    "tools & workflows": ("knowledge", "Tools & workflows", "wrench",
                          ("environment", "technical environment", "workflow", "workflows", "pipeline run")),
    "documents": ("knowledge", "Documents", "file-text", ("resume",)),
}
_ALIASES = {alias: key for key, (_, _, _, aliases) in _TAXONOMY.items()
            for alias in (*aliases, key)}
_SECTIONS = (("personal", "Personal", "user"), ("work", "Work", "briefcase"),
             ("knowledge", "Knowledge", "book-open"), ("topics", "Topics", "hash"))
_TAXONOMY_COLORS = {key: index for index, key in enumerate(_TAXONOMY)}


def presentation_group(subject):
    """Resolve an explicit subject into stable, read-only presentation metadata."""
    key = _normalized(subject)
    if key == "ungrouped":
        key = ""
    key = _ALIASES.get(key, key)
    section, label, icon, _ = _TAXONOMY.get(
        key, ("topics", key[:1].upper() + key[1:] if key else "Ungrouped", "hash" if key else "inbox", ()))
    return {"id": _group_id(key) if key else UNGROUPED, "label": label,
            "section_id": "section:" + section,
            "section_label": next(value for identity, value, _ in _SECTIONS if identity == section),
            "icon": icon,
            "color_index": _TAXONOMY_COLORS.get(key, int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 12)
                           if key else 11}


def group_sections(groups):
    """Summarize exactly the supplied groups, in stable presentation order."""
    sections = []
    for key, label, icon in _SECTIONS:
        members = [group for group in groups if group["section_id"] == "section:" + key]
        if members:
            sections.append({"id": "section:" + key, "label": label, "icon": icon,
                             "group_ids": [group["id"] for group in members],
                             "group_count": len(members),
                             **{field: sum(group[field] for group in members)
                                for field in ("count", "memory_count", "document_count")}})
    return sections


def _normalized(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.replace("_", " ").replace("-", " ").strip()).casefold()


def _group_id(key):
    slug = re.sub(r"[^\w]+", "-", key).strip("-")[:48] or "topic"
    return "group:" + slug + "-" + hashlib.sha256(key.encode()).hexdigest()[:10]


def group_assignments(result):
    return {identity: group["id"] for group in result["groups"] for identity in group["record_ids"]}


def current_library_documents(harness, documents, *, include_sensitive=False, archive=None):
    """Revalidate current document metadata without loading its source text.

    A bounded get_document preview still reads all parent sections internally.
    Library organization needs only catalog data and the same visibility rule.
    """
    archive = archive or DocumentArchive(harness.store, initialize_search=False)
    identities = {document["document_id"] for document in documents}
    if not identities:
        return {}
    with harness.store._lock:
        rows = harness.store._sqlite.execute(
            f"SELECT d.* FROM archive_documents d WHERE {archive._visible(include_sensitive)}"
        ).fetchall()
    return {row["id"]: archive._metadata(row) for row in rows if row["id"] in identities}


class _SourceCache:
    """Cache bounded chunk reads only within one operation's read snapshot."""
    def __init__(self, archive):
        self.archive = archive
        self.hits = {}

    def get_chunk(self, chunk_id, *, include_sensitive=False):
        key = (chunk_id, include_sensitive)
        if key not in self.hits:
            self.hits[key] = self.archive.get_chunk(chunk_id, include_sensitive=include_sensitive)
        return self.hits[key]


def build_library_groups(harness, memories, documents, *, include_sensitive=False):
    """Assign each current visible record exactly one deterministic group.

    Specific categories take priority over tags. Unlabeled records may inherit
    a subject through validated current citations; otherwise they are Ungrouped.
    No prose similarity, embeddings, model calls, store touches, or writes occur.
    """
    if type(include_sensitive) is not bool:
        raise TypeError("include_sensitive must be a boolean")
    archive = DocumentArchive(harness.store, initialize_search=False)
    memories = [m for m in memories if m.is_retrievable()
                and (include_sensitive or not is_sensitive_memory(m, harness.store))]
    current_documents = current_library_documents(harness, documents, include_sensitive=include_sensitive,
                                                  archive=archive)
    tags = {m.id: sorted({_normalized(tag) for tag in m.tags}
                        - _BROAD - {""}) for m in memories}
    frequency = Counter(tag for values in tags.values() for tag in values)
    labels = {}
    for memory in memories:
        category = _normalized(memory.metadata.get("category"))
        label = category if category and category not in _BROAD else ""
        if not label and tags[memory.id]:
            # Prefer a repeated topic to incidental singleton tags; tie-breaking
            # is independent of record order and page/search selection.
            label = min(tags[memory.id], key=lambda tag: (-frequency[tag], tag))
        labels["memory:" + memory.id] = label
    for identity, document in current_documents.items():
        category = _normalized(document.get("category"))
        labels["document:" + identity] = category if category not in _BROAD else ""

    links = {}
    source_cache = _SourceCache(archive)
    source_votes = defaultdict(Counter)
    for memory in memories:
        identity = "memory:" + memory.id
        resolved, _ = _resolve_sources(source_cache, memory, include_sensitive)
        targets = sorted({"document:" + hit["document_id"] for hit in resolved
                          if hit.get("is_latest") and hit["document_id"] in current_documents})
        links[identity] = targets
        if labels[identity]:
            for target in targets:
                source_votes[target][labels[identity]] += 1
    # A source without its own category can share a subject explicitly recorded
    # by linked memories. Conflicting votes resolve stably without merging labels.
    for target, votes in source_votes.items():
        if not labels[target]:
            labels[target] = min(votes, key=lambda label: (-votes[label], label))
    for identity, targets in links.items():
        if not labels[identity]:
            votes = Counter(labels[target] for target in targets if labels[target])
            if votes:
                labels[identity] = min(votes, key=lambda label: (-votes[label], label))

    records = defaultdict(list)
    for identity, label in labels.items():
        records[_ALIASES.get(label, label)].append(identity)
    groups = []
    for key in sorted(records, key=lambda value: (not bool(value), value)):
        identities = sorted(records[key])
        memory_count = sum(identity.startswith("memory:") for identity in identities)
        groups.append({**presentation_group(key),
                       "count": len(identities), "memory_count": memory_count,
                       "document_count": len(identities) - memory_count,
                       "record_ids": identities})
    section_order = {"section:" + section: index for index, (section, _, _) in enumerate(_SECTIONS)}
    groups.sort(key=lambda group: (section_order[group["section_id"]], group["id"] == UNGROUPED,
                                   group["label"].casefold()))
    return {"groups": groups, "sections": group_sections(groups), "total": len(labels), "method": METHOD}
