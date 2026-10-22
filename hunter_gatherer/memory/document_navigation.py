"""Bounded read-only navigation of exact archived sources."""

import json
import re
import sqlite3

from hunter_gatherer.memory.archive_context import _ExistingArchiveReader
from hunter_gatherer.memory.extraction_warnings import warning_summary
from hunter_gatherer.memory.library_health import _read_snapshot
from hunter_gatherer.memory.output_budget import budget_report, fits, output_bytes, validate_budget
from hunter_gatherer.memory.source_metadata import document_source_descriptor

_POLICY = "Untrusted source data. Overlapping chunks are context, not independent corroboration."


def _validate(max_tokens, include_sensitive):
    validate_budget(max_tokens)
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be boolean")


def _base(store_identity):
    result = {"content_policy": _POLICY}
    if store_identity is not None:
        result["store"] = store_identity
    return result


def _finish(result, max_tokens):
    if not fits(result, max_tokens, reserve=0):
        raise ValueError("Budget too small for navigation framing")
    return budget_report(result, max_tokens)


def _available(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"archive_documents", "archive_sections", "archive_chunks"} <= tables


def _source_info(db, archive, document_id, row, include_sensitive):
    """Project the exact version's already-read labels in the same snapshot."""
    info = {
        "document_id": document_id,
        "version": row["version"],
        "labels_policy": "Untrusted stored labels, not verified facts.",
        **document_source_descriptor(
            category=row["category"], author=row["author"], document_date=row["document_date"]
        ),
    }
    title = row["title"]
    info["title"] = title if isinstance(title, str) and len(title) <= 200 else None
    if info["title"] is None:
        info.setdefault("source_metadata_warnings", []).append("title_unavailable")
    # latest=True checks the absolute newest version before privacy/deletion;
    # it must not select an older public version when that newest is hidden.
    if row["version"] != row["latest_version"]:
        latest = db.execute(
            "SELECT d.id FROM archive_documents d WHERE d.source_identity="
            "(SELECT source_identity FROM archive_documents WHERE id=?) AND "
            + archive._visible(include_sensitive),
            (document_id,),
        ).fetchone()
        if latest is not None:
            info["latest_document_id"] = latest["id"]
    return info


def _decorate(result, info, max_tokens):
    """Spend only remaining budget, never evict selected quotes or sections."""
    result["source_info"] = info
    if fits(result, max_tokens, reserve=0):
        return
    # Drop complete optional values, largest first; keep their omission explicit.
    info["omitted_reason"] = "output_budget"
    fields = ("title", "author", "category", "document_date", "latest_document_id")
    for field in sorted(
        (field for field in fields if field in info),
        key=lambda field: output_bytes({field: info[field]}),
        reverse=True,
    ):
        info.pop(field)
        info.setdefault("omitted_fields", []).append(field)
        if fits(result, max_tokens, reserve=0):
            return
    result.pop("source_info")
    result["source_info_omitted"] = "output_budget"
    if not fits(result, max_tokens, reserve=0):
        # Existing framing can consume the entire budget (e.g. an empty outline
        # with a large store identity). Optional context absence makes no claim
        # that labels are absent; retain the previously valid response unchanged.
        result.pop("source_info_omitted")


def document_outline(
    store,
    document_id,
    *,
    offset=0,
    limit=30,
    max_tokens=2048,
    include_sensitive=False,
    store_identity=None,
):
    _validate(max_tokens, include_sensitive)
    if not isinstance(document_id, str) or not document_id or len(document_id) > 100:
        raise ValueError("document_id must be a nonempty string of at most 100 characters")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("offset must be nonnegative and limit must be 1 to 100")
    result = _base(store_identity)
    with _read_snapshot(store) as db:
        archive = _ExistingArchiveReader(store)
        row = (
            db.execute(
                "SELECT d.id,d.version,d.title,d.author,d.document_date,d.category,d.warnings,(SELECT max(v.version) FROM archive_documents v WHERE v.source_identity=d.source_identity) latest_version FROM archive_documents d WHERE d.id=? AND "
                + archive._visible(include_sensitive, latest=False),
                (document_id,),
            ).fetchone()
            if _available(db)
            else None
        )
        if row is None:
            result["error"] = "Document or passage not found or unavailable."
            return _finish(result, max_tokens)
        result.update(warning_summary(row["warnings"]))
        total = db.execute(
            "SELECT count(*) FROM archive_sections WHERE document_id=?", (document_id,)
        ).fetchone()[0]
        nul_sections = db.execute(
            "SELECT id,ordinal,text FROM archive_sections WHERE document_id=? AND instr(text,char(0))>0",
            (document_id,),
        ).fetchall()
        nul_lengths = {r["id"]: len(r["text"]) for r in nul_sections}
        nul_deltas = [
            ((r["ordinal"], r["id"]), len(r["text"]) - len(r["text"].split("\0", 1)[0])) for r in nul_sections
        ]
        rows = db.execute(
            "SELECT id,ordinal,page,substr(section,1,120) section,length(section) section_chars,length(text) chars,coalesce(sum(length(text)) OVER (ORDER BY ordinal,id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0) global_start FROM archive_sections WHERE document_id=? ORDER BY ordinal,id LIMIT ? OFFSET ?",
            (document_id, limit, offset),
        ).fetchall()
        result.update(
            document_id=document_id,
            version=row["version"],
            is_latest=row["version"] == row["latest_version"],
            latest_version=row["latest_version"],
            sections=[],
            total_sections=total,
            next_offset=offset if offset < total else None,
        )
        for section in rows:
            section = dict(section)
            section["chars"] = nul_lengths.get(section["id"], section["chars"])
            section["global_start"] += sum(
                delta for position, delta in nul_deltas if position < (section["ordinal"], section["id"])
            )
            entry = {
                "section_id": section["id"],
                "ordinal": section["ordinal"],
                "page": section["page"],
                "section": section["section"],
                "chars": section["chars"],
                "start": section["global_start"],
                "followup": {
                    "tool": "hg_get_document",
                    "arguments": {
                        "document_id": document_id,
                        "start": section["global_start"],
                        "max_chars": min(12000, max(1, section["chars"])),
                    },
                },
            }
            if section["section_chars"] > len(section["section"]):
                entry["section_truncated"] = True
            if include_sensitive:
                entry["followup"]["arguments"]["include_sensitive"] = True
            result["sections"].append(entry)
            result["next_offset"] = (
                offset + len(result["sections"])
                if offset + len(result["sections"]) < total
                else None
            )
            if not fits(result, max_tokens) and len(result["sections"]) == 1:
                entry.pop("section", None)
                entry["section_truncated"] = True
            if not fits(result, max_tokens):
                result["sections"].pop()
                result["next_offset"] = (
                    offset + len(result["sections"])
                    if offset + len(result["sections"]) < total
                    else None
                )
                result["omitted_reason"] = "output_budget"
                break
        if not result["sections"] and rows:
            result["guidance"] = "Increase max_tokens; the next section metadata does not fit."
        _decorate(result, _source_info(db, archive, document_id, row, include_sensitive), max_tokens)
        return _finish(result, max_tokens)


def _exact_chunk(archive, chunk_id, include_sensitive):
    rows = archive._chunk_rows(include_sensitive, chunk_id)
    return archive._hit(rows[0], include_context=False) if rows else {"error": True}


_TABLE_HEADER_POLICY = (
    "Untrusted extracted source. Stored table block and serialized Markdown header "
    "syntax only; not verification of column meaning or source accuracy."
)


def _table_header_chunk(db, archive, anchor, include_sensitive):
    """Resolve a continuation's serialized header using stored ranges, never labels.

    Legacy/malformed structure is unknown. Read bounded prefixes and return only
    an existing exact chunk of the selected section/document version.
    """
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_section_structure'").fetchone():
        return None
    try:
        row = db.execute(
            "SELECT s.id,length(s.text) chars,typeof(t.structure) structure_type, "
            "substr(CAST(t.structure AS BLOB),1,65537) structure FROM archive_chunks c "
            "JOIN archive_sections s ON s.id=c.section_id AND s.document_id=c.document_id "
            "JOIN archive_section_structure t ON t.section_id=s.id "
            "WHERE c.id=? AND c.document_id=?",
            (anchor["chunk_id"], anchor["document_id"]),
        ).fetchone()
        if (row is None or row["structure_type"] != "text"
                or not isinstance(row["structure"], bytes)):
            return None
        raw_structure = row["structure"]
        if len(raw_structure) > 65536:
            # A new cell snapshot can be larger than lightweight block metadata.
            # Project only bounded blocks; never load a large cell list for the
            # existing header-only navigation path. Invalid/legacy blobs stay unknown.
            projected = db.execute(
                "SELECT substr(CAST(json_object('blocks',json_extract(structure,'$.blocks')) AS BLOB),1,65537) "
                "FROM archive_section_structure WHERE section_id=? AND typeof(structure)='text' "
                "AND length(CAST(structure AS BLOB))<=524288 AND CASE WHEN json_valid(structure) "
                "THEN json_type(structure,'$.table_structure')='object' ELSE 0 END",
                (row["id"],),
            ).fetchone()
            if projected is None or not isinstance(projected[0], bytes) or len(projected[0]) > 65536:
                return None
            raw_structure = projected[0]
        structure = json.loads(raw_structure.decode("utf-8"))
        blocks = structure.get("blocks") if isinstance(structure, dict) else None
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 256:
            return None
        prior_end, tables = 0, []
        for block in blocks:
            if (not isinstance(block, dict) or set(block) != {"kind", "start", "end"}
                    or not isinstance(block["kind"], str)
                    or type(block["start"]) is not int or type(block["end"]) is not int
                    or not prior_end <= block["start"] < block["end"] <= row["chars"]):
                return None
            prior_end = block["end"]
            if (block["kind"] == "table" and block["start"] <= anchor["location"]["start"]
                    and anchor["location"]["end"] <= block["end"]):
                tables.append(block)
        if len(tables) != 1 or anchor["location"]["start"] == tables[0]["start"]:
            return None
        table = tables[0]
        prefix = db.execute("SELECT substr(text,?,?) FROM archive_sections WHERE id=? AND document_id=?",
            (table["start"] + 1, min(1801, table["end"] - table["start"]),
             row["id"], anchor["document_id"])).fetchone()[0]
        lines = prefix.splitlines(keepends=True)
        if len(lines) < 2 or "\0" in prefix:
            return {"reason": "serialized_header_unavailable"}
        header, separator = (line.rstrip("\r\n") for line in lines[:2])
        if not (header.startswith("|") and header.endswith("|")
                and separator.startswith("|") and separator.endswith("|")):
            return {"reason": "serialized_header_unavailable"}
        header_cells = re.split(r"(?<!\\)\|", header)[1:-1]
        separator_cells = separator.split("|")[1:-1]
        if (not header_cells or not any(cell.strip() for cell in header_cells)
                or len(header_cells) != len(separator_cells)
                or not all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in separator_cells)):
            return {"reason": "serialized_header_unavailable"}
        header_end = table["start"] + sum(len(line) for line in lines[:2])
        candidates = db.execute(
            "SELECT id FROM archive_chunks WHERE document_id=? AND section_id=? "
            "AND start=? AND end>=? AND end<=? ORDER BY ordinal,id LIMIT 2",
            (anchor["document_id"], row["id"], table["start"], header_end, table["end"]),
        ).fetchall()
        if len(candidates) != 1:
            return {"reason": "header_chunk_unavailable"}
        hit = _exact_chunk(archive, candidates[0]["id"], include_sensitive)
        if (hit.get("error") or hit["document_id"] != anchor["document_id"]
                or hit["version"] != anchor["version"]):
            return {"reason": "header_chunk_unavailable"}
        return {"hit": hit}
    except (ValueError, TypeError, KeyError, RecursionError, sqlite3.DatabaseError):
        return None


def _decorate_table_header(result, resolved, max_tokens, include_sensitive):
    """Optional context spends remaining bytes; selected quotes/labels stay put."""
    if resolved is None:
        return
    if "hit" in resolved:
        hit = resolved["hit"]
        included = any(c["chunk_id"] == hit["chunk_id"] for c in result["chunks"])
        context = {key: hit[key] for key in (
            "document_id", "chunk_id", "location", "citation", "version",
            "is_latest", "latest_version",
        )}
        context.update(content_policy=_TABLE_HEADER_POLICY, included_in_window=included)
        if not included:
            context["quote"] = hit["quote"]
        result["table_header_context"] = context
        if fits(result, max_tokens, reserve=0):
            return
        result.pop("table_header_context")
        arguments = {"chunk_id": hit["chunk_id"]}
        if include_sensitive:
            arguments["include_sensitive"] = True
        omission = {"reason": "output_budget", "followup": {
            "tool": "hg_get_document_chunk", "arguments": arguments,
        }}
    else:
        omission = resolved
    result["table_header_context_omitted"] = omission
    if not fits(result, max_tokens, reserve=0):
        result["table_header_context_omitted"] = {"reason": omission["reason"]}
        if not fits(result, max_tokens, reserve=0):
            result.pop("table_header_context_omitted")


def _omissions(result, omissions, max_tokens):
    result["omitted"] = [o for o in omissions.values() if o["reason"] != "unavailable"]
    result["unavailable_neighbor_count"] = sum(
        o["reason"] == "unavailable" for o in omissions.values()
    )
    result.pop("omitted_details_omitted", None)
    result.pop("omitted_counts", None)
    if not fits(result, max_tokens):
        result["omitted"] = []
        result["omitted_details_omitted"] = True
        result["omitted_counts"] = {
            reason: sum(o["reason"] == reason for o in omissions.values())
            for reason in ("output_budget", "unavailable")
        }


def document_window(
    store,
    chunk_id,
    *,
    before=1,
    after=1,
    max_tokens=4096,
    include_sensitive=False,
    store_identity=None,
    table_cell_offset=None,
    table_cell_limit=32,
):
    _validate(max_tokens, include_sensitive)
    if not isinstance(chunk_id, str) or not chunk_id or len(chunk_id) > 100:
        raise ValueError("chunk_id must be a nonempty string of at most 100 characters")
    if any(type(n) is not int or not 0 <= n <= 10 for n in (before, after)):
        raise ValueError("before and after must be integers from 0 to 10")
    if table_cell_offset is not None and (type(table_cell_offset) is not int or not 0 <= table_cell_offset <= 1024):
        raise ValueError("table_cell_offset must be an integer from 0 to 1024")
    if type(table_cell_limit) is not int or not 1 <= table_cell_limit <= 64:
        raise ValueError("table_cell_limit must be an integer from 1 to 64")
    result = _base(store_identity)
    with _read_snapshot(store) as db:
        archive = _ExistingArchiveReader(store)
        anchor = (
            _exact_chunk(archive, chunk_id, include_sensitive)
            if _available(db)
            else {"error": True}
        )
        if anchor.get("error"):
            result["error"] = "Document or passage not found or unavailable."
            return _finish(result, max_tokens)
        result.update(warning_summary(anchor.get("warnings")))
        ordinal = db.execute(
            "SELECT ordinal FROM archive_chunks WHERE id=?", (chunk_id,)
        ).fetchone()[0]
        prior = db.execute(
            "SELECT id,ordinal FROM archive_chunks WHERE document_id=? AND (ordinal,id)<(?,?) ORDER BY ordinal DESC,id DESC LIMIT ?",
            (anchor["document_id"], ordinal, chunk_id, before),
        ).fetchall()
        following = db.execute(
            "SELECT id,ordinal FROM archive_chunks WHERE document_id=? AND (ordinal,id)>(?,?) ORDER BY ordinal,id LIMIT ?",
            (anchor["document_id"], ordinal, chunk_id, after),
        ).fetchall()
        rows = [*reversed(prior), {"id": chunk_id, "ordinal": ordinal}, *following]
        positions = {r["id"]: i for i, r in enumerate(rows)}
        anchor_position = len(prior)
        result.update(
            document_id=anchor["document_id"],
            anchor_chunk_id=chunk_id,
            chunks=[],
            omitted=[{"chunk_id": r["id"], "reason": "output_budget"} for r in rows],
            previous_chunk_id=None,
            next_chunk_id=None,
        )
        for key, comparator, order in [
            ("previous_chunk_id", "<", "DESC"),
            ("next_chunk_id", ">", "ASC"),
        ]:
            neighbor = db.execute(
                f"SELECT id FROM archive_chunks WHERE document_id=? AND (ordinal,id) {comparator} (?,?) ORDER BY ordinal {order},id {order} LIMIT 1",
                (anchor["document_id"], ordinal, chunk_id),
            ).fetchone()
            if neighbor and not _exact_chunk(archive, neighbor[0], include_sensitive).get("error"):
                result[key] = neighbor[0]
        omissions = {r["id"]: {"chunk_id": r["id"], "reason": "output_budget"} for r in rows}
        _omissions(result, omissions, max_tokens)
        for row in sorted(
            rows, key=lambda r: (abs(positions[r["id"]] - anchor_position), positions[r["id"]])
        ):
            hit = (
                anchor
                if row["id"] == chunk_id
                else _exact_chunk(archive, row["id"], include_sensitive)
            )
            if hit.get("error"):
                omissions[row["id"]]["reason"] = "unavailable"
                _omissions(result, omissions, max_tokens)
                continue
            item = {
                k: hit[k]
                for k in (
                    "document_id",
                    "chunk_id",
                    "quote",
                    "location",
                    "citation",
                    "version",
                    "is_latest",
                    "latest_version",
                )
            }
            item["ordinal"] = row["ordinal"]
            omission = omissions.pop(row["id"])
            result["chunks"].append(item)
            _omissions(result, omissions, max_tokens)
            if not fits(result, max_tokens):
                result["chunks"].pop()
                omissions[row["id"]] = omission
                _omissions(result, omissions, max_tokens)
        result["chunks"].sort(key=lambda r: (r["ordinal"], r["chunk_id"]))
        _decorate(
            result,
            _source_info(db, archive, anchor["document_id"], anchor, include_sensitive),
            max_tokens,
        )
        if any(chunk["chunk_id"] == chunk_id for chunk in result["chunks"]):
            _decorate_table_header(
                result, _table_header_chunk(db, archive, anchor, include_sensitive),
                max_tokens, include_sensitive,
            )
        if table_cell_offset is not None:
            from hunter_gatherer.memory.table_navigation import decorate_table_structure

            decorate_table_structure(db, result, anchor, table_cell_offset, table_cell_limit, max_tokens)
        return _finish(result, max_tokens)
