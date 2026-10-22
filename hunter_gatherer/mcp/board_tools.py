"""One explicit presentation-only board tool backed by the library service."""
import json

from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.library_board import (
    BoardInput,
    CreateGroupInput,
    board_visibility,
    read_board,
    save_board,
    visible_board,
)
from hunter_gatherer.memory.library_health import _read_snapshot


def board(harness, action="read", revision=None, changes=None, include_sensitive=False, max_items=200):
    if action not in {"read", "update", "create_group"}:
        raise ValueError("Board action must be read, update or create_group.")
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be a boolean.")
    if type(max_items) is not int or not 1 <= max_items <= 1000:
        raise ValueError("max_items must be an integer between 1 and 1000.")
    if changes is not None and not isinstance(changes, dict):
        raise ValueError("changes must be an object.")
    DocumentArchive(harness.store)
    if action == "read":
        if changes or revision is not None:
            raise ValueError("Read does not accept changes or revision.")
        with _read_snapshot(harness.store):
            records, groups = board_visibility(harness, include_sensitive)
            result = visible_board(read_board(harness.store), records, groups, include_sensitive)
        result["changed"] = False
        result["changed_record_ids"] = []
    else:
        if revision is None or not changes:
            raise ValueError("Writes require the latest revision and explicit changes.")
        if "revision" in changes or "include_sensitive" in changes:
            raise ValueError("Put revision and include_sensitive in the tool arguments.")
        model = CreateGroupInput if action == "create_group" else BoardInput
        body = model.model_validate({**changes, "revision": revision, "include_sensitive": include_sensitive})
        result = save_board(harness.store, body, lambda opted: board_visibility(harness, opted),
                            create=action == "create_group", include_activity=True)
    # Raster bytes belong to the browser, not context windows. The read is
    # bounded explicitly and reports omissions instead of claiming completeness.
    omitted = {}
    sections = {section: result[section] for section in ("groups", "records")}
    result["groups"], result["records"] = {}, {}
    for section in ("groups", "records"):
        values = sections[section]
        for identity, value in list(sorted(values.items()))[:max_items]:
            clean = {key: item for key, item in value.items() if key != "image"}
            if value.get("image"):
                clean["image_present"] = True
            elif "image" in value:
                clean["image"] = None
            if len(clean.get("description", "")) > 400:
                clean["description"] = clean["description"][:400]
                clean["description_truncated"] = True
            if "record_ids" in clean and len(clean["record_ids"]) > max_items:
                clean["record_ids_omitted"] = len(clean["record_ids"]) - max_items
                clean["record_ids"] = clean["record_ids"][:max_items]
            result[section][identity] = clean
            if len(json.dumps(result, ensure_ascii=False).encode()) > 24_000:
                result[section].pop(identity)
                break
        omitted[section] = len(values) - len(result[section])
    result["omitted"] = omitted
    result["images_included"] = False
    result["output_budget_bytes"] = 24_000
    return result


SCHEMAS = {"hg_board": {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["read", "update", "create_group"], "default": "read"},
        "revision": {"type": "integer", "minimum": 0},
        "changes": {"type": "object", "maxProperties": 10},
        "include_sensitive": {"type": "boolean", "default": False},
        "max_items": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
    },
}}
DESCRIPTIONS = {"hg_board": (
    "Read or explicitly customize the persistent library board. Presentation is never evidence. "
    "Read first for revision and IDs; updates require that revision (stale writes fail). "
    "update changes: layout board/constellation/columns, grouping subjects/kind/source, follow_agent boolean, "
    "groups {group_id:{title,description,icon,image,width,height,record_ids}} or null to remove customization, "
    "records {memory:<id>|document:<id>:{icon,image}} or null. Use null visual fields to clear. "
    "create_group changes: title, description, kind group/topic, icon, image, width (240..4000), "
    "height (160..16000), record_ids (visible memory:<id>/document:<id>), sensitive. "
    "Use emoji or documented icon glyphs; images only local PNG/JPEG/WebP base64 data URLs up to 1 MiB. "
    "No external image fetches. Sensitive content requires explicit relevant include_sensitive each call. "
    "Reads omit raster bytes and bound groups/records/members by max_items; omissions are explicit."
)}
HANDLERS = {"hg_board": board}
