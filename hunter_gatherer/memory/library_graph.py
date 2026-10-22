"""An honest, bounded view of explicit archive relationships.

Source edges describe a resolvable citation, never proof of a memory's claim.
Topic edges group recorded tags/categories; embedding neighbors are not edges.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.evidence import (
    _kind,
    _normal_category,
    _resolve_sources,
    _source_refs,
    _verification,
    is_sensitive_memory,
)
from hunter_gatherer.memory.library_board import read_board, record_visual
from hunter_gatherer.memory.library_groups import (
    _SourceCache,
    build_library_groups,
    current_library_documents,
    group_assignments,
    group_sections,
)


def _topic(field, value):
    return f"topic:{field}:{quote(value, safe='')}"


def _groups(memory):
    groups = {("tag", value) for value in memory.tags if isinstance(value, str) and value.strip()}
    category = memory.metadata.get("category")
    if isinstance(category, str) and category.strip():
        groups.add(("category", category))
    return sorted(groups)


def _edge(source, target, kind, **extra):
    identity = "|".join((source, target, kind, extra.get("citation", "")))
    return {"id": "edge:" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            "source": source, "target": target, "kind": kind, **extra}


def _document_media(record):
    # Filename and media type come from archive provenance, never the title.
    filename = record.get("file_name") or Path(record.get("source_path", "").replace("\\", "/")).name
    return {"media_type": record.get("media_type", ""), "file_name": filename,
            "file_type": Path(filename).suffix.lower().lstrip("."),
            "source_path": record.get("source_path", "")}


def _document_preview(harness, archive, identity, include_sensitive):
    # Fetch only the indexed first passage, not vectors, whole sections, file
    # bytes or a full get_document body. Recheck the current visibility predicate
    # in this query too, including deleted/latest-version privacy.
    with harness.store._lock:
        row = harness.store._sqlite.execute(
            "SELECT c.id, substr(c.text,1,400) AS excerpt, s.page, s.section "
            "FROM archive_chunks c JOIN archive_documents d ON d.id=c.document_id "
            "JOIN archive_sections s ON s.id=c.section_id AND s.document_id=d.id "
            f"WHERE d.id=? AND {archive._visible(include_sensitive)} "
            "ORDER BY c.ordinal,c.id LIMIT 1", (identity,),
        ).fetchone()
    if row is None:
        return {"preview": "", "preview_available": False}
    return {"preview": row["excerpt"], "preview_available": True,
            "preview_citation": f"[doc:{identity}#{row['id']}]",
            "preview_location": {"page": row["page"], "section": row["section"]}}


def build_library_graph(harness, memories, documents, *, limit=150, include_sensitive=False,
                        grouping=None, group="", show_empty_groups=True, mode="default"):
    if mode not in {"default", "grouped"}:
        raise ValueError("mode must be default or grouped")
    if type(include_sensitive) is not bool:
        raise TypeError("include_sensitive must be a boolean")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be 1–500")
    archive = DocumentArchive(harness.store, initialize_search=False)
    # Caller filtering is a convenience, never the privacy boundary.
    memories = [memory for memory in memories if memory.is_retrievable()
                and (include_sensitive or not is_sensitive_memory(memory, harness.store))]
    visible_documents = current_library_documents(harness, documents, include_sensitive=include_sensitive,
                                                 archive=archive)
    if grouping is None:
        grouping = build_library_groups(harness, memories, list(visible_documents.values()),
                                        include_sensitive=include_sensitive)
    assignments = group_assignments(grouping)
    board_records = read_board(harness.store)["records"]
    if group:
        memories = [memory for memory in memories if assignments.get("memory:" + memory.id) == group]
        visible_documents = {identity: document for identity, document in visible_documents.items()
                             if assignments.get("document:" + identity) == group}
    groups = {_topic(field, value): (field, value)
              for memory in memories for field, value in _groups(memory)}
    for document in visible_documents.values():
        if document.get("category"):
            groups[_topic("category", document["category"])] = ("category", document["category"])

    # Stable selection, with isolated memories kept as ordinary record nodes.
    records = [("memory", memory.id, memory) for memory in memories]
    records += [("document", identity, document) for identity, document in visible_documents.items()]
    records.sort(key=lambda item: (-item[2].created_at if item[0] == "memory" else -item[2]["created_at"],
                                   item[0], item[1]))
    # Leave room for explicit hubs and referenced sources when the archive is
    # larger than the view. Otherwise a full record page would have no links.
    topic_headroom = min(len(groups), max(1, limit // 4))
    source_headroom = min(max(1, limit // 8), len(memories)) if any(_source_refs(m) for m in memories) else 0
    if mode == "grouped":
        topic_headroom = source_headroom = 0
    record_budget = max(1, limit - topic_headroom - source_headroom)
    selected = records[:record_budget]
    nodes, edges, memberships, sources = [], [], [], []
    source_cache = _SourceCache(archive)
    for record_type, identity, record in selected:
        node_id = f"{record_type}:{identity}"
        if record_type == "memory":
            text = " ".join(record.text.split())
            category = record.metadata.get("category", "")
            nodes.append({"id": node_id, "record_id": identity, "type": "memory",
                          "label": text[:90], "preview": text[:240],
                          "kind": _kind(_normal_category(category), memory=record),
                          "category": category if isinstance(category, str) else "",
                          "importance": record.importance, "verification": _verification(record),
                          "sensitive": is_sensitive_memory(record, harness.store), "tags": record.tags,
                          "group_id": assignments.get(node_id)})
            memberships.extend((node_id, field, value) for field, value in _groups(record))
            resolved, _unresolved = _resolve_sources(source_cache, record, include_sensitive)
            # A historical citation can be fetched explicitly, but is never
            # silently promoted to a graph edge to a current source.
            for hit in resolved:
                if hit.get("is_latest") and (not group or assignments.get("document:" + hit["document_id"]) == group):
                    sources.append((node_id, hit))
        else:
            nodes.append({"id": node_id, "record_id": identity, "type": "document",
                          "label": record["title"], "kind": "source",
                          **_document_preview(harness, archive, identity, include_sensitive),
                          "category": record["category"], "sensitive": record["sensitive"], "tags": [],
                          "group_id": assignments.get(node_id), **_document_media(record)})
            if record["category"]:
                memberships.append((node_id, "category", record["category"]))

    # Include an explicitly cited current source when query/category filtering
    # did not select it, while respecting the same total-node budget.
    extra_sources = {}
    selected_ids = {node["id"] for node in nodes}
    for _source, hit in sources:
        target = "document:" + hit["document_id"]
        if target not in selected_ids:
            extra_sources[hit["document_id"]] = hit
    selected_topic_count = len({_topic(field, value) for _node, field, value in memberships})
    topic_headroom = min(selected_topic_count, max(1, limit // 4))
    if mode == "grouped":
        topic_headroom = 0
    extra_metadata = current_library_documents(harness,
                                               [{"document_id": identity} for identity in extra_sources],
                                               include_sensitive=include_sensitive, archive=archive)
    for identity, hit in sorted(extra_sources.items()):
        if len(nodes) >= limit - topic_headroom:
            break
        metadata = extra_metadata.get(identity)
        if metadata is None:
            continue
        node_id = "document:" + identity
        nodes.append({"id": node_id, "record_id": identity, "type": "document",
                      "label": hit["title"], "kind": "source",
                      **_document_preview(harness, archive, identity, include_sensitive),
                      "category": hit["category"], "sensitive": hit["sensitive"], "tags": [],
                      "group_id": assignments.get(node_id), **_document_media(metadata)})
        if hit["category"]:
            memberships.append((node_id, "category", hit["category"]))
            groups[_topic("category", hit["category"])] = ("category", hit["category"])

    selected_groups = {_topic(field, value): (field, value) for _node, field, value in memberships}
    topic_assignments = {}
    for source, field, value in memberships:
        topic_assignments.setdefault(_topic(field, value), set()).add(assignments.get(source))
    for topic_id, (field, value) in sorted(selected_groups.items()):
        if mode == "grouped":
            break
        if len(nodes) >= limit:
            break
        nodes.append({"id": topic_id, "record_id": value, "type": "topic",
                      "label": value, "preview": f"Explicit {field}: {value}", "kind": field,
                      "group_id": next(iter(topic_assignments[topic_id]))
                      if len(topic_assignments[topic_id]) == 1 else None})
    memory_metadata = {"memory:" + memory.id: memory.metadata for memory in memories}
    for node in nodes:
        visual = record_visual(memory_metadata.get(node["id"], {}), board_records.get(node["id"], {}))
        node.update({**visual, "presentation": visual})
    selected_ids = {node["id"] for node in nodes}
    for source, field, value in memberships:
        target = _topic(field, value)
        if target in selected_ids:
            edges.append(_edge(source, target, "topic", label=f"Recorded {field}"))
    for source, hit in sources:
        target = "document:" + hit["document_id"]
        if target in selected_ids:
            edges.append(_edge(source, target, "source", label="Cites source", citation=hit["citation"]))
    edges = sorted({edge["id"]: edge for edge in edges}.values(), key=lambda edge: edge["id"])
    potential = len(records) + len(extra_sources) + (len(groups) if mode == "default" else 0)
    warnings = ["Source edges mean a current citation resolves; claim support is not verified.",
                "Topic edges group only explicitly recorded tags and categories; similar wording creates no edge."]
    if potential > limit:
        warnings.append("Node limit reached; some records, topics, and their links are omitted.")
    visible_group_ids = {node.get("group_id") for node in nodes}
    visible_groups = [item for item in grouping["groups"]
                      if item["id"] in visible_group_ids or
                      (item.get("manual") and not item["record_ids"]
                       and (show_empty_groups or item["id"] == group)
                       and (not group or item["id"] == group))]
    matching_ids = {f"{kind}:{identity}" for kind, identity, _ in records}
    return {"nodes": nodes, "edges": edges,
            "mode": mode,
            "counts": {"matching_records": len(records),
                       "shown_records": sum(node["type"] != "topic" for node in nodes),
                       "shown_matching_records": sum(node["id"] in matching_ids for node in nodes),
                       "hidden_topics": len(groups) - sum(node["type"] == "topic" for node in nodes)},
            "groups": visible_groups, "sections": group_sections(visible_groups),
            "grouping_method": grouping["method"],
            "scope": {"memories": len(memories), "documents": len(visible_documents), "topics": len(groups)},
            "truncated": potential > limit, "warnings": warnings,
            "include_sensitive": include_sensitive, "limit": limit}
