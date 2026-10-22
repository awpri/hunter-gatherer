"""Bounded citation ranges for exact document reader slices.

Coordinates remain section-local Python codepoints. Only chunks accepted by
the existing SQL resolver are eligible; in particular, NUL-affected chunks may
remain uncited even though the document reader preserves their source text.
"""
import json
import re
from collections import defaultdict

MAX_CANDIDATES = 1024
MAX_SPANS = 128
MAX_METADATA_BYTES = 32768
_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def document_citation_map(db, document_id, passages, sections, *, page=None,
                          include_sensitive=False):
    """Read once inside the caller's catalog snapshot; never initialize tables."""
    result = {"coordinate_system": "section_codepoints_half_open",
              "coverage": "none", "complete": True, "spans": []}
    if not passages or not _ID.fullmatch(document_id):
        return result
    section_lookup = {s["id"]: s for s in sections}
    by_section = {p["section_id"]: i for i, p in enumerate(passages)}
    first, last = passages[0], passages[-1]
    lower = (section_lookup[first["section_id"]]["ordinal"], first["section_id"])
    upper = (section_lookup[last["section_id"]]["ordinal"], last["section_id"])
    # All intermediate sections are fully included in a contiguous reader page.
    # One range query avoids an archive-wide scan for each returned passage and
    # does not create a parameter for every tiny section in a large reader page.
    query = (
        "WITH candidates AS MATERIALIZED (SELECT c.id,c.section_id,c.ordinal,c.start,c.end "
        "FROM archive_documents d CROSS JOIN archive_chunks c INDEXED BY idx_archive_chunks "
        "ON c.document_id=d.id "
    )
    args = [document_id, include_sensitive]
    if first["section_id"] == last["section_id"]:
        # The reader already established this exact section's ownership/page
        # inside our snapshot. Avoid rejoining its possibly huge parent row for
        # every chunk scanned just to inspect its ordinal.
        query += ("WHERE d.id=? AND d.deleted_at IS NULL AND (? OR d.sensitive=0) "
                  "AND c.section_id=? AND c.start<? AND c.end>? ")
        args += [first["section_id"], first["end"], first["start"]]
    else:
        query += (
            "CROSS JOIN archive_sections s ON s.id=c.section_id AND s.document_id=c.document_id "
            "WHERE d.id=? AND d.deleted_at IS NULL AND (? OR d.sensitive=0) "
            "AND (s.ordinal,s.id)>=(?,?) AND (s.ordinal,s.id)<=(?,?) "
        )
        args += [*lower, *upper]
        if page is not None:
            query += "AND s.page=? "
            args.append(int(page))
        query += ("AND (s.id<>? OR (c.start<? AND c.end>?)) "
                  "AND (s.id<>? OR (c.start<? AND c.end>?)) ")
        args += [first["section_id"], first["end"], first["start"],
                 last["section_id"], last["end"], last["start"]]
    query += (
        "ORDER BY c.ordinal,c.id LIMIT ?), "
        "lengths AS MATERIALIZED (SELECT s.id,CASE "
        "WHEN s.id=? AND ?=0 THEN ? WHEN s.id=? AND ?=0 THEN ? "
        "ELSE length(s.text) END chars FROM archive_sections s "
        "JOIN (SELECT DISTINCT section_id FROM candidates) touched ON touched.section_id=s.id), "
        "bounds AS MATERIALIZED (SELECT c.section_id,min(c.start) span_start,max(c.end) span_end "
        "FROM candidates c JOIN lengths l ON l.id=c.section_id "
        "WHERE typeof(c.start)='integer' AND typeof(c.end)='integer' "
        "AND c.start>=0 AND c.end>c.start AND c.end<=l.chars GROUP BY c.section_id), "
        "windows AS MATERIALIZED (SELECT b.section_id,b.span_start,b.span_end,"
        "substr(s.text,b.span_start+1,b.span_end-b.span_start) text FROM bounds b "
        "JOIN archive_sections s ON s.id=b.section_id) "
        "SELECT CASE WHEN typeof(c.id)='text' AND instr(c.id,char(0))=0 "
        "AND length(c.id) IN (32,36) THEN c.id ELSE '' END id,c.section_id,"
        "CASE WHEN typeof(c.ordinal)='integer' THEN c.ordinal ELSE 0 END ordinal,"
        "CASE WHEN typeof(c.start)='integer' THEN c.start ELSE -1 END start,"
        "CASE WHEN typeof(c.end)='integer' THEN c.end ELSE -1 END end,"
        "CASE WHEN typeof(c.start)='integer' AND typeof(c.end)='integer' "
        "AND c.start>=w.span_start AND c.end>c.start AND c.end<=w.span_end "
        "AND substr(w.text,c.start-w.span_start+1,c.end-c.start)=stored.text "
        "THEN 1 ELSE 0 END valid FROM candidates c "
        "JOIN archive_chunks stored ON stored.id=c.id "
        "LEFT JOIN windows w ON w.section_id=c.section_id ORDER BY c.ordinal,c.id"
    )
    args.append(MAX_CANDIDATES + 1)
    for passage in (first, last):
        section = section_lookup[passage["section_id"]]
        args += [section["id"], section["has_nul"], section["chars"]]
    rows = db.execute(query, args).fetchall()
    reasons = []
    if len(rows) > MAX_CANDIDATES:
        rows.pop()
        reasons.append("candidate_limit")
    intervals = defaultdict(list)
    for row in rows:
        if not row["valid"] or not _ID.fullmatch(row["id"]):
            continue
        index = by_section.get(row["section_id"])
        if index is None:
            continue
        passage = passages[index]
        left, right = max(passage["start"], row["start"]), min(passage["end"], row["end"])
        if left < right:
            intervals[index].append((left, right, row["ordinal"], row["id"]))
    covered = 0
    metadata_bytes = _bytes(result) + 160
    stopped = False
    for index, passage in enumerate(passages):
        candidates = intervals[index]
        cursor = passage["start"]
        while cursor < passage["end"]:
            active = [item for item in candidates if item[0] <= cursor < item[1]]
            if not active:
                following = [item[0] for item in candidates if item[0] > cursor]
                if not following:
                    break
                cursor = min(following)
                continue
            chosen = min(active, key=lambda item: (-item[1], item[2], item[3]))
            span = {"passage_index": index, "start": cursor, "end": chosen[1],
                    "chunk_id": chosen[3], "citation": f"[doc:{document_id}#{chosen[3]}]"}
            if len(result["spans"]) >= MAX_SPANS:
                reasons.append("citation_limit")
                stopped = True
                break
            result["spans"].append(span)
            # Reserve bounded final coverage/completeness and omission framing.
            span_bytes = _bytes(span) + (1 if len(result["spans"]) > 1 else 0)
            if metadata_bytes + span_bytes > MAX_METADATA_BYTES:
                result["spans"].pop()
                reasons.append("metadata_budget")
                stopped = True
                break
            metadata_bytes += span_bytes
            covered += span["end"] - span["start"]
            cursor = span["end"]
        if stopped:
            break
    total = sum(len(p["text"]) for p in passages)
    result["coverage"] = "full" if covered == total and total else "partial" if covered else "none"
    if reasons:
        result["complete"] = False
        result["omitted_reasons"] = reasons
    return result
