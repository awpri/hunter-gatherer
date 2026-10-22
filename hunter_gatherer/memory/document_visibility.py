"""Shared catalog visibility without initializing archives or reading originals.

Sensitivity follows the undirected source/hash closure, including tombstones.
The uncorrelated closure is evaluated once per SQL statement, not once per
candidate document/chunk. Historical public editions remain visible unless
their selected record or latest source-family record is deleted.
"""
from __future__ import annotations


def sensitive_document_ids_sql():
    # Traverse distinct source/hash identities instead of document rows. A
    # thousand copies of one hash expand that hash once, rather than joining
    # every copied row to every other copy at each recursive step.
    return (
        "WITH RECURSIVE private(kind,value) AS ("
        "SELECT 'source',source_identity FROM archive_documents WHERE sensitive<>0 "
        "UNION SELECT 'hash',content_hash FROM archive_documents WHERE sensitive<>0 "
        "UNION SELECT 'hash',d.content_hash FROM archive_documents d JOIN private p "
        "ON p.kind='source' AND d.source_identity=p.value "
        "UNION SELECT 'source',d.source_identity FROM archive_documents d JOIN private p "
        "ON p.kind='hash' AND d.content_hash=p.value) "
        "SELECT id FROM archive_documents WHERE source_identity IN ("
        "SELECT value FROM private WHERE kind='source') OR content_hash IN ("
        "SELECT value FROM private WHERE kind='hash')"
    )


def live_document_clause(alias="d", *, latest=False, exact=False):
    clause = f"{alias}.deleted_at IS NULL"
    if exact:
        clause += (" AND (SELECT newest.deleted_at FROM archive_documents newest "
                   f"WHERE newest.source_identity={alias}.source_identity "
                   "ORDER BY newest.version DESC LIMIT 1) IS NULL")
    else:
        clause += (
            f" AND {alias}.source_identity NOT IN ("
            "SELECT dead.source_identity FROM archive_documents dead "
            "WHERE dead.deleted_at IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM archive_documents newer WHERE newer.source_identity=dead.source_identity "
            "AND newer.version>dead.version))"
        )
    if latest:
        clause += (f" AND NOT EXISTS (SELECT 1 FROM archive_documents newer "
                   f"WHERE newer.source_identity={alias}.source_identity AND newer.version>{alias}.version)")
    return clause


def _related_sql(seed_source, seed_hash, *, name="related"):
    return (f"{name}(kind,value) AS ({seed_source} UNION {seed_hash} "
            f"UNION SELECT 'hash',d.content_hash FROM archive_documents d JOIN {name} r "
            "ON r.kind='source' AND d.source_identity=r.value "
            f"UNION SELECT 'source',d.source_identity FROM archive_documents d JOIN {name} r "
            "ON r.kind='hash' AND d.content_hash=r.value)")


def _private_related_sql(root_condition=""):
    return ("SELECT 1 FROM related r JOIN archive_documents p ON p.source_identity=r.value "
            "WHERE r.kind='source' AND p.sensitive<>0 " + root_condition
            + " UNION ALL SELECT 1 FROM related r JOIN archive_documents p ON p.content_hash=r.value "
            "WHERE r.kind='hash' AND p.sensitive<>0 " + root_condition + " LIMIT 1")


def visible_document_clause(include_sensitive, *, latest=True, alias="d", exact=False):
    clause = live_document_clause(alias, latest=latest, exact=exact)
    if not include_sensitive:
        if exact:
            related = _related_sql(f"SELECT 'source',{alias}.source_identity",
                                   f"SELECT 'hash',{alias}.content_hash")
            clause += f" AND NOT EXISTS (WITH RECURSIVE {related} {_private_related_sql()})"
        else:
            clause += f" AND {alias}.id NOT IN ({sensitive_document_ids_sql()})"
    return clause


def document_reference_states(db, *, document_ids=(), chunk_ids=()):
    """Batch exact dependency availability/privacy from the caller's snapshot."""
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('archive_documents','archive_chunks')")}
    if "archive_documents" not in tables:
        return {}
    seeds = [("document", value) for value in dict.fromkeys(document_ids)]
    if "archive_chunks" in tables:
        seeds += [("chunk", value) for value in dict.fromkeys(chunk_ids)]
    result = {}
    for start in range(0, len(seeds), 500):
        batch = seeds[start:start + 500]
        docs = [value for kind, value in batch if kind == "document"]
        chunks = [value for kind, value in batch if kind == "chunk"]
        conditions = []
        if docs:
            conditions.append("d.id IN (" + ",".join("?" for _ in docs) + ")")
        if chunks:
            conditions.append("d.id IN (SELECT document_id FROM archive_chunks WHERE id IN ("
                              + ",".join("?" for _ in chunks) + "))")
        selected = ("selected AS MATERIALIZED (SELECT d.id,d.source_identity,d.content_hash,d.deleted_at "
                    "FROM archive_documents d WHERE " + " OR ".join(conditions) + ")")
        # Expand the union of selected components once, then propagate privacy
        # from sensitive rows inside those components. This preserves individual
        # states without expanding a shared copied-source component per ref.
        related = _related_sql("SELECT 'source',source_identity FROM selected",
                               "SELECT 'hash',content_hash FROM selected")
        private = _related_sql(
            "SELECT 'source',p.source_identity FROM related r JOIN archive_documents p "
            "ON r.kind='source' AND p.source_identity=r.value WHERE p.sensitive<>0",
            "SELECT 'hash',p.content_hash FROM related r JOIN archive_documents p "
            "ON r.kind='hash' AND p.content_hash=r.value WHERE p.sensitive<>0", name="private")
        rows = db.execute(
            f"WITH RECURSIVE {selected},{related},{private} "
            f"SELECT d.id,({live_document_clause(exact=True)}),"
            "d.source_identity IN (SELECT value FROM private WHERE kind='source') "
            "FROM selected d", docs + chunks)
        result.update({row[0]: {"available": bool(row[1]), "sensitive": bool(row[2])} for row in rows})
    return result
