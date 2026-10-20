"""Durable, bounded presentation choices; never retrieval facts or source edges.

Stored separately from memory metadata, with optimistic revisions and filtered
at every read. Images stay local data URLs: no remote fetches or active SVG.
"""
from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import uuid
from contextlib import suppress
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

BOARD_KEY = "library_board_v1"
MAX_IMAGE_BYTES = 1024 * 1024
MAX_BOARD_BYTES = 8 * 1024 * 1024
ICONS = {"user", "graduation-cap", "compass", "folder", "check-circle", "sliders", "wrench",
         "file-text", "inbox", "hash", "briefcase", "book-open", "star", "heart", "sparkles",
         "bookmark", "lightbulb", "map", "palette", "code", "coffee", "music", "image",
         "globe", "calendar", "target", "layers", "pen", "flag", "rocket", "leaf",
         "topics", "source", "workflow", "preferences", "decision", "project", "education",
         "archive", "knowledge", "work", "personal", "memory", "lock", "pin"}


def valid_icon(value):
    if not isinstance(value, str) or not value or len(value) > 32:
        raise ValueError("Icon must be an allowed glyph name or a short emoji.")
    if value in ICONS:
        return value
    # Reject ASCII text, markup, controls and bidi characters. Emoji may carry
    # joiners, variation selectors, keycaps and skin-tone modifiers.
    if not any(0x2300 <= ord(c) <= 0x27BF or 0x1F000 <= ord(c) <= 0x1FAFF for c in value):
        raise ValueError("Icon must be an allowed glyph name or a short emoji.")
    if any(not (0x2300 <= ord(c) <= 0x27BF or 0x1F000 <= ord(c) <= 0x1FAFF)
           and c not in "\u200d\ufe0f\u20e3#*0123456789" for c in value):
        raise ValueError("Icon contains unsupported characters.")
    return value


def valid_image(value):
    if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 4 // 3 + 100:
        raise ValueError("Image must be a local raster data URL of at most 1 MiB.")
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})", value)
    if not match:
        raise ValueError("Only local PNG, JPEG and WebP data URLs are supported.")
    try:
        data = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Image has invalid base64 data.") from exc
    mime = match[1]
    signature = (data.startswith(b"\x89PNG\r\n\x1a\n") if mime == "png" else
                 data.startswith(b"\xff\xd8\xff") if mime == "jpeg" else
                 data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    if not data or len(data) > MAX_IMAGE_BYTES or not signature:
        raise ValueError("Image contents must match the declared raster type and fit within 1 MiB.")
    return value


class VisualInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    icon: str | None = Field(default=None, max_length=32)
    image: str | None = Field(default=None, max_length=MAX_IMAGE_BYTES * 4 // 3 + 100)

    @field_validator("icon", "image")
    @classmethod
    def validate_visual(cls, value, info):
        return value if value is None else (valid_icon(value) if info.field_name == "icon" else valid_image(value))


class GroupInput(VisualInput):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    width: StrictInt | None = Field(default=None, ge=240, le=4000)
    height: StrictInt | None = Field(default=None, ge=160, le=16000)
    kind: Literal["group", "topic"] | None = None
    record_ids: list[str] | None = Field(default=None, max_length=5000)
    sensitive: StrictBool | None = None

    @field_validator("record_ids")
    @classmethod
    def unique_records(cls, value):
        if value is None:
            return value
        if len(set(value)) != len(value) or any(not re.fullmatch(r"(?:memory|document):[\w-]{1,100}", identity) for identity in value):
            raise ValueError("Record IDs must be unique memory:<id> or document:<id> values.")
        return value


class BoardInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=0)
    include_sensitive: StrictBool = False
    layout: Literal["board", "constellation", "columns"] | None = None
    grouping: Literal["subjects", "kind", "source"] | None = None
    follow_agent: StrictBool | None = None
    groups: dict[str, GroupInput | None] = Field(default_factory=dict, max_length=100)
    records: dict[str, VisualInput | None] = Field(default_factory=dict, max_length=5000)
    aliases: dict[str, str | None] = Field(default_factory=dict, max_length=100)

    @field_validator("aliases")
    @classmethod
    def bounded_alias_ids(cls, value):
        if any(not source or len(source) > 200 or (target is not None and (not target or len(target) > 200))
               for source, target in value.items()):
            raise ValueError("Alias IDs must be nonempty strings of at most 200 characters.")
        return value


class CreateGroupInput(GroupInput):
    revision: StrictInt = Field(ge=0)
    include_sensitive: StrictBool = False
    title: str = Field(min_length=1, max_length=200)


class BoardConflict(ValueError):
    pass


def default_board():
    return {"version": 1, "revision": 0, "layout": "board", "grouping": "subjects",
            "follow_agent": True, "groups": {}, "records": {}, "aliases": {}, "alias_sensitive": {}}


def read_board(store):
    with store._lock:
        raw = store.get_meta(BOARD_KEY)
    if not raw:
        return default_board()
    board = json.loads(raw)
    if not isinstance(board, dict) or board.get("version") != 1:
        raise RuntimeError("Unsupported board version; preserve the stored board before resetting.")
    board.setdefault("aliases", {})
    board.setdefault("alias_sensitive", {})
    return board


def visual_metadata(metadata):
    """Treat generic MCP metadata as untrusted input; discard invalid visuals."""
    raw = metadata.get("presentation", {}) if isinstance(metadata, dict) else {}
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, validator in (("icon", valid_icon), ("image", valid_image)):
        if raw.get(key) is not None:
            with suppress(ValueError):
                result[key] = validator(raw[key])
    return result



def record_visual(metadata, override):
    """Null masks a stored metadata visual; deleting the override restores it."""
    visual = visual_metadata(metadata)
    visual.update(visual_metadata({"presentation": override}))
    for key in ("icon", "image"):
        if key in override and override[key] is None:
            visual.pop(key, None)
    return visual


def visible_board(board, visible_records, visible_groups, include_sensitive=False):
    result = {key: value for key, value in board.items() if key not in {"groups", "records", "aliases", "alias_sensitive"}}
    result["aliases"] = {source: target for source, target in board.get("aliases", {}).items()
                         if source in visible_groups and target in visible_groups
                         and (include_sensitive or not board.get("alias_sensitive", {}).get(source))}
    result["records"] = {key: value for key, value in board["records"].items() if key in visible_records}
    result["groups"] = {}
    for identity, value in board["groups"].items():
        if not include_sensitive and (value.get("sensitive") or any(key not in visible_records for key in value.get("record_ids", []))):
            continue
        if identity.startswith("group:manual:") or identity in visible_groups:
            result["groups"][identity] = {**value, **({"record_ids": [key for key in value["record_ids"] if key in visible_records]}
                                                    if "record_ids" in value else {})}
    return result


def save_board(store, body, visibility, *, create=False, include_activity=False):
    """CAS under SQLite's writer lock, including visibility eligibility reads."""
    with store._lock:
        if store._sqlite.in_transaction:
            raise RuntimeError("Board writes require an independent transaction.")
        store._sqlite.execute("BEGIN IMMEDIATE")
        try:
            board = read_board(store)
            if board["revision"] != body.revision:
                raise BoardConflict("Board changed; reload before saving.")
            records, groups = visibility(body.include_sensitive)
            projection = visible_board(board, records, groups, body.include_sensitive)
            changes = body.model_dump(exclude_unset=True)
            group_id = None
            if create:
                group_id = "group:manual:" + str(uuid.uuid4())
                changes = {"groups": {group_id: {key: value for key, value in changes.items()
                                                if key not in {"revision", "include_sensitive"}}}}
                changes["groups"][group_id].setdefault("kind", "group")
                changes["groups"][group_id].setdefault("record_ids", [])
            updated = copy.deepcopy(board)
            for key in ("layout", "grouping", "follow_agent"):
                if changes.get(key) is not None:
                    updated[key] = changes[key]
            from proharness.memory.library_group_aliases import validate_aliases

            for source, target in changes.get("aliases", {}).items():
                if source not in groups or (source in board["aliases"] and source not in projection["aliases"]):
                    raise ValueError("Alias group not found or excluded.")
                if target is None:
                    updated["aliases"].pop(source, None)
                    updated["alias_sensitive"].pop(source, None)
                    continue
                validate_aliases({source: target}, groups)
                updated["aliases"][source] = target
                public_records, public_groups = visibility(False)
                updated["alias_sensitive"][source] = bool(body.include_sensitive and any(
                    identity not in public_groups or any(record not in public_records for record in groups[identity])
                    for identity in (source, target)))
            if any(target in updated["aliases"] for target in updated["aliases"].values()):
                raise ValueError("Alias chains and cycles are not supported; select a canonical target directly.")
            if len(updated["aliases"]) > 100:
                raise ValueError("At most 100 reviewed aliases are supported.")
            for section, allowed in (("groups", groups), ("records", records)):
                for identity, value in changes.get(section, {}).items():
                    exists = identity in board[section]
                    if section == "groups":
                        available = identity in allowed or identity in projection["groups"] or identity == group_id
                    else:
                        available = identity in allowed
                    if not available or (exists and identity not in projection[section]):
                        raise ValueError("Board target not found or excluded.")
                    if value is None:
                        updated[section].pop(identity, None)
                        continue
                    value = dict(value)
                    if section == "groups" and identity.startswith(("kind:", "source:")) and "record_ids" in value:
                        raise ValueError("Alternate grouping customizations only change presentation, not membership.")
                    if any(key not in records for key in value.get("record_ids", []) or []):
                        raise ValueError("Group membership contains a record not found or excluded.")
                    if value.get("sensitive") and not body.include_sensitive:
                        raise ValueError("Private group changes require include_sensitive=true.")
                    if section == "groups" and body.include_sensitive:
                        # Custom content written while viewing hidden members must
                        # never become public through a shared automatic group.
                        public_records, public_groups = visibility(False)
                        if (not identity.startswith("group:manual:") and
                                (identity not in public_groups or any(key not in public_records for key in groups.get(identity, set())))) or any(key not in public_records for key in value.get("record_ids", []) or []):
                            value["sensitive"] = True
                    target = updated[section].setdefault(identity, {})
                    for key, item in value.items():
                        if item is None and section != "records":
                            target.pop(key, None)
                        else:
                            target[key] = item
            if len(updated["groups"]) > 100 or len(updated["records"]) > 5000:
                raise ValueError("Board supports at most 100 customizations and 5000 record visuals.")
            updated["revision"] += 1
            raw = json.dumps(updated, ensure_ascii=False, separators=(",", ":"))
            if len(raw.encode()) > MAX_BOARD_BYTES:
                raise ValueError("Board customization exceeds the 8 MiB storage limit.")
            store._sqlite.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)", (BOARD_KEY, raw))
            store._sqlite.commit()
            result = visible_board(updated, records, groups, body.include_sensitive)
            if group_id:
                result["group_id"] = group_id
            if include_activity:
                changed_ids = set()
                for identity in set(board["records"]) | set(updated["records"]):
                    if board["records"].get(identity) != updated["records"].get(identity):
                        changed_ids.add(identity)
                for identity in set(board["groups"]) | set(updated["groups"]):
                    old, new = board["groups"].get(identity, {}), updated["groups"].get(identity, {})
                    if old != new:
                        changed_ids.update(old.get("record_ids", []))
                        changed_ids.update(new.get("record_ids", []))
                        changed_ids.update(groups.get(identity, set()))
                for source in set(board["aliases"]) | set(updated["aliases"]):
                    if board["aliases"].get(source) != updated["aliases"].get(source):
                        for identity in (source, board["aliases"].get(source), updated["aliases"].get(source)):
                            changed_ids.update(groups.get(identity, set()))
                result["changed_record_ids"] = sorted(changed_ids & records)[:64]
                result["changed"] = any(board[key] != updated[key] for key in
                                        ("layout", "grouping", "follow_agent", "groups", "records", "aliases"))
            return result
        except Exception:
            store._sqlite.rollback()
            raise


def apply_board_groups(result, board):
    """Explicit manual membership overrides automatic browsing assignments."""
    groups = copy.deepcopy(result["groups"])
    custom = board["groups"]
    manual = [(identity, value) for identity, value in custom.items() if identity.startswith("group:manual:")]
    explicit = [(identity, value) for identity, value in custom.items() if "record_ids" in value]
    assigned = set()
    # Deterministic precedence if a record appears in multiple manual groups.
    overrides = {}
    for identity, value in sorted(explicit):
        members = [key for key in value.get("record_ids", []) if key not in assigned]
        assigned.update(members)
        overrides[identity] = members
    for identity, value in sorted(manual):
        members = overrides.get(identity, [])
        memory_count = sum(key.startswith("memory:") for key in members)
        groups.append({"id": identity, "label": value.get("title", "Untitled group"), "section_id": "section:topics",
                       "section_label": "Topics", "icon": "hash", "color_index": 8, "manual": True,
                       "kind": value.get("kind", "group"), "count": len(members), "memory_count": memory_count,
                       "document_count": len(members) - memory_count, "record_ids": members})
    output = []
    for group in groups:
        if not group.get("manual"):
            group["record_ids"] = overrides.get(group["id"], [key for key in group["record_ids"] if key not in assigned])
            group["count"] = len(group["record_ids"])
            group["memory_count"] = sum(key.startswith("memory:") for key in group["record_ids"])
            group["document_count"] = group["count"] - group["memory_count"]
            if not group["count"] and group["id"] not in overrides:
                continue
        value = custom.get(group["id"], {})
        group.update({key: value[key] for key in ("description", "icon", "image", "width", "height", "kind") if key in value})
        if value.get("title"):
            group["label"] = value["title"]
        output.append(group)
    from proharness.memory.library_group_aliases import apply_aliases
    from proharness.memory.library_groups import group_sections

    output = apply_aliases(output, board.get("aliases", {}))
    return {**result, "groups": output, "sections": group_sections(output)}


def board_visibility(harness, include_sensitive=False):
    """Complete current catalog eligibility, usable by HTTP and MCP callers."""
    from proharness.memory.documents import DocumentArchive
    from proharness.memory.evidence import (
        _kind,
        _normal_category,
        _resolve_sources,
        is_sensitive_memory,
    )
    from proharness.memory.library_groups import (
        _SourceCache,
        build_library_groups,
        group_assignments,
    )
    from proharness.memory.schemas import Memory

    archive = DocumentArchive(harness.store, initialize_search=False)
    with harness.store._lock:
        rows = harness.store._sqlite.execute("SELECT * FROM memories WHERE deleted_at IS NULL").fetchall()
        memories = [Memory.from_sql_row(tuple(row)) for row in rows]
        memories = [memory for memory in memories if memory.is_retrievable() and
                    (include_sensitive or not is_sensitive_memory(memory, harness.store))]
        documents = [archive._metadata(row) for row in harness.store._sqlite.execute(
            f"SELECT d.* FROM archive_documents d WHERE {archive._visible(include_sensitive)}").fetchall()]
        grouping = build_library_groups(harness, memories, documents, include_sensitive=include_sensitive)
    groups = {group["id"]: set(group["record_ids"]) for group in grouping["groups"]}

    def assign(identity, record):
        groups.setdefault(identity, set()).add(record)

    def source_label(document):
        path = document.get("source_path", "").replace("\\", "/")
        parts = [part for part in path.rsplit("/", 1)[0].split("/") if part] if "/" in path else []
        return "/".join(parts[-2:]) if parts else "Saved files"

    sources = {document["document_id"]: source_label(document) for document in documents}
    for document in documents:
        record = "document:" + document["document_id"]
        assign("kind:Files", record)
        assign("source:" + sources[document["document_id"]], record)
    cache = _SourceCache(archive)
    for memory in memories:
        record = "memory:" + memory.id
        kind = _kind(_normal_category(memory.metadata.get("category")), memory=memory)
        assign("kind:" + (kind[:1].upper() + kind[1:] if kind else "Memories"), record)
        resolved, _ = _resolve_sources(cache, memory, include_sensitive)
        linked = sorted({hit["document_id"] for hit in resolved if hit.get("is_latest") and hit["document_id"] in sources})
        assign("source:" + (sources[linked[0]] if linked else "Without a source file"), record)
    return set(group_assignments(grouping)), groups
