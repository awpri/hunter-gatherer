"""Reviewed group presentation merges; no source facts or memberships are rewritten."""
from __future__ import annotations

import copy


def validate_aliases(aliases, available):
    if len(aliases) > 100:
        raise ValueError("At most 100 reviewed group aliases are supported.")
    for source, target in aliases.items():
        if source == target or source not in available or target not in available:
            raise ValueError("Alias groups must be distinct existing visible automatic groups.")
        if not source.startswith("group:") or not target.startswith("group:") or any(value.startswith("group:manual:") for value in (source, target)):
            raise ValueError("Aliases combine automatic subject groups; manual groups retain their membership.")
    if any(target in aliases for target in aliases.values()):
        raise ValueError("Alias chains and cycles are not supported; select a canonical target directly.")


def apply_aliases(groups, aliases):
    output = copy.deepcopy(groups)
    by_id = {group["id"]: group for group in output}
    removed = set()
    for source, target in sorted(aliases.items()):
        if source not in by_id or target not in by_id or by_id[source].get("manual") or by_id[target].get("manual"):
            continue
        canonical = by_id[target]
        canonical["record_ids"] = sorted(set(canonical["record_ids"]) | set(by_id[source]["record_ids"]))
        canonical["count"] = len(canonical["record_ids"])
        canonical["memory_count"] = sum(value.startswith("memory:") for value in canonical["record_ids"])
        canonical["document_count"] = canonical["count"] - canonical["memory_count"]
        canonical.setdefault("alias_group_ids", []).append(source)
        removed.add(source)
    return [group for group in output if group["id"] not in removed]


def preview_aliases(grouping, board, patch):
    groups = {group["id"]: group for group in grouping["groups"] if not group.get("manual")}
    aliases = dict(board.get("aliases", {}))
    for source, target in patch.items():
        if target is None:
            aliases.pop(source, None)
        else:
            aliases[source] = target
    # Existing aliases whose groups disappeared are inactive, not a reason to
    # expose excluded groups or fail a separate current-visible review.
    active = {source: target for source, target in aliases.items() if source in groups and target in groups}
    validate_aliases(active, set(groups))
    for source, target in patch.items():
        if source not in groups or (target is not None and target not in groups):
            raise ValueError("Alias group not found or excluded.")
    merges = []
    for source, target in sorted(active.items()):
        first, second = groups[source], groups[target]
        merges.append({"source_group_id": source, "target_group_id": target,
                       "source_label": first["label"], "target_label": second["label"],
                       "source_count": first["count"], "target_count": second["count"],
                       "combined_count": len(set(first["record_ids"]) | set(second["record_ids"]))})
    combined = apply_aliases(grouping["groups"], active)
    return {"revision": board["revision"], "merges": merges,
            "group_count_before": len(grouping["groups"]), "group_count_after": len(combined),
            "affected_count": sum(groups[source]["count"] for source in active)}
