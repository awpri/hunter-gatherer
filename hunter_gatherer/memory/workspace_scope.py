"""Read-only privacy for explicit scopes in the trusted local store.

Unregistered scope labels (including ``global``) are supported for temporal and
procedure records. They are organization labels, not authenticated tenants.
When a label identifies a coordination object, its current privacy and its
known parent workspace privacy are inherited. No constructor or schema writes
are used, so this check is safe inside another service's transaction.
"""

from __future__ import annotations

import json


def scope_visible(store, scope: str, include_sensitive: bool = False) -> bool:
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 500:
        raise ValueError("scope must be a nonempty string of at most 500 characters")
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be a boolean")
    with store._lock:
        if (
            store._sqlite.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coordination_objects'"
            ).fetchone()
            is None
        ):
            return True

        def visible(label, ancestors, *, required=False):
            if label in ancestors or len(ancestors) >= 32:
                return False
            row = store._sqlite.execute(
                "SELECT id,workspace_id,data FROM coordination_objects WHERE id=?", (label,)
            ).fetchone()
            if row is None:
                return not required
            try:
                data = json.loads(row["data"])
            except (ValueError, TypeError):
                return False
            if not isinstance(data, dict):
                return False
            if data.get("sensitive") and not include_sensitive:
                return False
            parents = set()
            for parent in (
                row["workspace_id"],
                data.get("workspace_id"),
                data.get("parent_workspace_id"),
            ):
                if parent in (None, "", label):
                    continue
                if not isinstance(parent, str) or len(parent) > 500:
                    return False
                parents.add(parent)
            return all(visible(parent, ancestors | {label}, required=True) for parent in parents)

        return visible(scope, set())
