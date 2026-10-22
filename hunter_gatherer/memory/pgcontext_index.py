"""Opt-in, synthetic-only pgContext 0.3.0 candidate-index experiment.

Nothing imports this module from the production memory path. The caller owns
embedding generation and authoritative snippet hydration. Results contain IDs
and scores only. Requires an already-installed local PG17/18 + pgContext 0.3.0;
never installs extensions, services, or migrates an existing store.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import shlex
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit


class PgContextError(RuntimeError):
    """Sanitized experiment failure (never exposes driver credentials or SQL)."""


def local_connection_parameters(dsn: str) -> dict[str, str | int]:
    """Accept only explicit Unix sockets or literal loopback, without DNS.

    Reject service files, hostaddr overrides, multi-host failover, libpq options,
    implicit defaults, and URL query overrides. Return explicit safe kwargs so
    the driver cannot reinterpret the original DSN differently.
    """
    try:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError
        params: dict[str, str | int] = {}
        if dsn.startswith(("postgresql://", "postgres://")):
            url = urlsplit(dsn)
            if url.fragment:
                raise ValueError
            if url.hostname:
                params["host"] = unquote(url.hostname)
            if url.port:
                params["port"] = url.port
            if url.username is not None:
                params["user"] = unquote(url.username)
            if url.password is not None:
                params["password"] = unquote(url.password)
            if url.path and url.path != "/":
                params["dbname"] = unquote(url.path[1:])
            for key, value in parse_qsl(url.query, keep_blank_values=True):
                if key in params:
                    raise ValueError
                params[key] = value
        else:
            for token in shlex.split(dsn):
                key, value = token.split("=", 1)
                if key in params:
                    raise ValueError
                params[key] = value
        if set(params) - {"host", "port", "dbname", "user", "password", "sslmode", "connect_timeout"}:
            raise ValueError
        host = str(params.get("host", ""))
        if not host or "," in host or "\x00" in host:
            raise ValueError
        if host.startswith("/"):
            if ".." in host.split("/"):
                raise ValueError
        elif not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        if not params.get("dbname"):
            raise ValueError
        port = int(params.get("port", 5432))
        if not 1 <= port <= 65535:
            raise ValueError
        if params.get("sslmode", "disable") not in {"disable", "require", "verify-ca", "verify-full"}:
            raise ValueError
        timeout = int(params.get("connect_timeout", 5))
        if not 1 <= timeout <= 30:
            raise ValueError
        params.update(port=port, connect_timeout=timeout, sslmode=params.get("sslmode", "disable"))
        return params
    except (ValueError, TypeError):
        raise PgContextError("Explicit local Unix-socket or literal-loopback DSN required") from None


@dataclass(frozen=True)
class CandidateDocument:
    id: str
    text: str
    vector: Sequence[float]
    document_id: str | None = None


@dataclass(frozen=True)
class CandidateHit:
    """Higher-is-better cosine similarity for dense/exact/ann, RRF for hybrid."""
    id: str
    score: float


class PgContextIndex:
    """Disposable isolated index; no automatic sync or production authority."""

    def __init__(self, dsn: str, dimensions: int, namespace: str | None = None,
                 *, connection_factory: Callable | None = None):
        self._parameters = local_connection_parameters(dsn)
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 1 <= dimensions <= 16000:
            raise ValueError("dimensions must be in 1..16000")
        prefix = namespace or "experiment"
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", prefix):
            raise ValueError("namespace must be a short lowercase identifier")
        # A caller-provided label never targets a pre-existing schema.
        self.schema = f"ph_pgctx_{prefix}_{uuid.uuid4().hex[:20]}"
        self.collection = self.schema
        self.table = f'"{self.schema}"."candidates"'
        self.dimensions = dimensions
        self._factory = connection_factory
        self._connection = None
        self._created = False
        self.version = None

    def __repr__(self):
        return f"PgContextIndex(dimensions={self.dimensions}, schema={self.schema!r})"

    def connect(self):
        if self._connection is not None:
            return self
        if any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")):
            raise PgContextError("Remove libpq routing/service/options environment overrides for this experiment")
        factory = self._factory
        if factory is None:
            try:
                import psycopg
            except ImportError:
                raise PgContextError("Install optional hunter_gatherer[pgcontext] experiment dependency") from None
            factory = psycopg.connect
        conn = None
        try:
            conn = factory(**self._parameters)
            with conn.transaction():
                row = conn.execute("SELECT current_setting('server_version_num')::integer, "
                                   "extversion FROM pg_extension WHERE extname = 'pgcontext'").fetchone()
                if not row or not (170000 <= row[0] < 190000) or row[1] != "0.3.0":
                    raise PgContextError("Requires PostgreSQL 17/18 and installed pgContext 0.3.0")
                # No IF NOT EXISTS: collisions abort rather than claim someone else's schema.
                conn.execute(f'CREATE SCHEMA "{self.schema}"')
                conn.execute(f"CREATE TABLE {self.table} (id text PRIMARY KEY, text text NOT NULL, document_id text NOT NULL, "
                             f"embedding pgcontext.vector({self.dimensions}) NOT NULL)")
                conn.execute("SELECT * FROM pgcontext.create_collection(%s, %s)",
                             (self.collection, f"{self.schema}.candidates"))
                conn.execute("SELECT pgcontext.register_vector(%s, 'embedding', 'embedding', %s, 'cosine')",
                             (self.collection, self.dimensions))
                conn.execute("SELECT pgcontext.register_filter_column(%s, 'document_id', 'document_id')",
                             (self.collection,))
                conn.execute(f"CREATE INDEX candidates_embedding_hnsw ON {self.table} "
                             "USING pgcontext_hnsw (embedding pgcontext.vector_hnsw_cosine_ops)")
                # Composite execute_query uses the explicit vector-index binding,
                # unlike direct SQL KNN or the collection exact-search surface.
                # This attachment API is experimental in the pinned 0.3.0 tag.
                conn.execute("SELECT pgcontext.attach_hnsw_index(%s, 'embedding', %s)",
                             (self.collection, f"{self.schema}.candidates_embedding_hnsw"))
                conn.execute("SELECT pgcontext.register_lexical_source(%s, 'body', ARRAY['text'], "
                             "'pg_catalog.english', ARRAY['A'], NULL::text[], 'ts_rank_cd', "
                             "0, ARRAY[0.1,0.2,0.4,1.0]::real[])", (self.collection,))
                conn.execute("SELECT pgcontext.create_lexical_index(%s, 'body', 'gin')", (self.collection,))
            self.version = {"postgres_version_num": row[0], "pgcontext": row[1]}
            self._connection = conn
            self._created = True
            return self
        except Exception as exc:
            if conn is not None:
                # Preserve the sanitized primary initialization failure.
                with suppress(Exception):
                    conn.close()
            if isinstance(exc, PgContextError):
                raise
            raise PgContextError("Local pgContext experiment initialization failed") from None

    def _conn(self):
        if self._connection is None or not self._created:
            raise PgContextError("Experiment index is not connected")
        return self._connection

    def _vector(self, values: Sequence[float]) -> str:
        if len(values) != self.dimensions:
            raise ValueError("Vector dimension mismatch")
        numbers = [float(v) for v in values]
        if not all(math.isfinite(v) and abs(v) <= 3.4028234e38 for v in numbers):
            raise ValueError("Vector values must be finite float32 values")
        if not any(numbers):
            raise ValueError("Cosine vectors must have nonzero norm")
        return "[" + ",".join(str(v) for v in numbers) + "]"

    def upsert(self, records: Sequence[CandidateDocument]) -> None:
        prepared = []
        for record in records:
            if not isinstance(record.id, str) or not record.id or not isinstance(record.text, str):
                raise ValueError("Candidate requires nonempty text ID and text body")
            document_id = record.id if record.document_id is None else record.document_id
            if not isinstance(document_id, str) or not document_id:
                raise ValueError("document_id must be a nonempty string")
            prepared.append((record.id, record.text, self._vector(record.vector), document_id))
        if not prepared:
            return
        conn = self._conn()
        try:
            with conn.transaction():
                for values in prepared:
                    conn.execute(f"INSERT INTO {self.table} (id,text,embedding,document_id) VALUES (%s,%s,%s::pgcontext.vector,%s) "
                                 "ON CONFLICT (id) DO UPDATE SET text=EXCLUDED.text, embedding=EXCLUDED.embedding, document_id=EXCLUDED.document_id", values)
                conn.execute("SELECT pgcontext.upsert_points(%s, %s::text[])",
                             (self.collection, [row[0] for row in prepared]))
        except Exception:
            raise PgContextError("Local pgContext candidate upsert failed") from None

    def delete(self, ids: Sequence[str]) -> None:
        ids = list(ids)
        if not all(isinstance(item, str) and item for item in ids):
            raise ValueError("Candidate IDs must be nonempty strings")
        if not ids:
            return
        conn = self._conn()
        try:
            with conn.transaction():
                conn.execute("SELECT pgcontext.delete_points(%s, %s::text[])", (self.collection, ids))
                conn.execute(f"DELETE FROM {self.table} WHERE id = ANY(%s::text[])", (ids,))
        except Exception:
            raise PgContextError("Local pgContext candidate delete failed") from None

    def query(self, vector: Sequence[float], limit: int = 10, text: str | None = None,
              mode: str = "dense", allowed_document_ids: Sequence[str] | None = None) -> list[CandidateHit]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("limit must be in 1..10000")
        if mode not in {"dense", "exact", "hybrid", "ann"}:
            raise ValueError("mode must be dense, exact, hybrid, or ann")
        if mode == "hybrid" and (not isinstance(text, str) or not text.strip()):
            raise ValueError("hybrid query requires text")
        value = self._vector(vector)
        conn = self._conn()
        allowed = None if allowed_document_ids is None else list(allowed_document_ids)
        if allowed is not None:
            if not all(isinstance(item, str) and item for item in allowed) or len(allowed) > 256:
                raise ValueError("Allowed document IDs require at most 256 nonempty strings")
            if not allowed:
                return []
        filter_json = None if allowed is None else json.dumps({"must": [{"key": "document_id", "match": {"any": allowed}}]})
        try:
            with conn.transaction():
                if mode == "ann":
                    # Genuine native KNN operator. The planner may still pick a full
                    # scan; query_plan records that rather than forcing index use.
                    sql, parameters = self._ann_statement(value, limit, allowed)
                    rows = conn.execute(sql, parameters).fetchall()
                elif mode == "exact":
                    # Function + score DESC prevents an ANN operator scan: exhaustive oracle.
                    where = "" if allowed is None else "WHERE document_id = ANY(%s::text[]) "
                    parameters = (value, limit) if allowed is None else (value, allowed, limit)
                    rows = conn.execute(f"SELECT id, 1.0 - pgcontext.cosine_distance(embedding, "
                                        f"%s::pgcontext.vector) AS score FROM {self.table} "
                                        f"{where}ORDER BY score DESC, id COLLATE \"C\" LIMIT %s", parameters).fetchall()
                elif mode == "hybrid" and allowed is not None:
                    # Both native branches receive the same registered-field filter.
                    rows = conn.execute("SELECT source_key, score FROM pgcontext.execute_query(%s, "
                                        "pgcontext.query_rerank(pgcontext.query_prefetch(ARRAY["
                                        "pgcontext.query_nearest('embedding', %s::pgcontext.vector, %s::jsonb, %s),"
                                        "pgcontext.query_lexical('body', %s::jsonb, %s::jsonb, %s)]), %s))",
                                        (self.collection, value, filter_json, limit,
                                         json.dumps({"form": "plain", "text": text}), filter_json, limit, limit)).fetchall()
                elif mode == "hybrid":
                    rows = conn.execute("SELECT source_key, score FROM pgcontext.query("
                                        "%s,%s::pgcontext.vector,%s,'body',%s)",
                                        (self.collection, value, text, limit)).fetchall()
                else:
                    if allowed is None:
                        rows = conn.execute("SELECT source_key, 1.0 - score AS score FROM pgcontext.search("
                                            "%s,%s::pgcontext.vector,%s)", (self.collection, value, limit)).fetchall()
                    else:
                        rows = conn.execute("SELECT source_key, 1.0 - score AS score FROM pgcontext.search("
                                            "%s,%s::pgcontext.vector,%s::text,%s)",
                                            (self.collection, value, filter_json, limit)).fetchall()
            return [CandidateHit(str(row[0]), float(row[1])) for row in rows]
        except Exception:
            raise PgContextError("Local pgContext candidate query failed") from None

    def _ann_statement(self, value, limit, allowed):
        where = "" if allowed is None else "WHERE document_id = ANY(%s::text[]) "
        parameters = (value, value, limit) if allowed is None else (value, allowed, value, limit)
        sql = (f"SELECT id, 1.0 - (embedding OPERATOR(pgcontext.<=>) %s::pgcontext.vector) AS score "
               f"FROM {self.table} {where}ORDER BY embedding OPERATOR(pgcontext.<=>) "
               "%s::pgcontext.vector LIMIT %s")
        return sql, parameters

    def analyze(self) -> None:
        """Update only owned experiment-table statistics before inspecting plans."""
        try:
            with self._conn().transaction():
                self._conn().execute(f"ANALYZE {self.table}")
        except Exception:
            raise PgContextError("Local pgContext experiment analyze failed") from None

    def query_plan(self, vector: Sequence[float], limit: int = 10,
                   allowed_document_ids: Sequence[str] | None = None) -> dict:
        """EXPLAIN the direct KNN query, without forcing plans or executing it.

        `uses_hnsw` is true only for a chosen scan of this experiment's HNSW
        index. Scope in this SQL path is relational WHERE filtering; it is not
        evidence that the collection API selected its filter-aware ANN path.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("limit must be in 1..10000")
        value = self._vector(vector)
        allowed = None if allowed_document_ids is None else list(allowed_document_ids)
        if allowed is not None and (len(allowed) > 256 or not all(isinstance(item, str) and item for item in allowed)):
            raise ValueError("Allowed document IDs require at most 256 nonempty strings")
        sql, parameters = self._ann_statement(value, limit, allowed)
        try:
            with self._conn().transaction():
                row = self._conn().execute("EXPLAIN (FORMAT JSON) " + sql, parameters).fetchone()
            explain = row[0]
            if isinstance(explain, str):
                explain = json.loads(explain)

            def chosen_index(node):
                if isinstance(node, dict):
                    if node.get("Node Type") in {"Index Scan", "Index Only Scan"} and node.get("Index Name") == "candidates_embedding_hnsw":
                        return True
                    return any(chosen_index(value) for value in node.values())
                return isinstance(node, list) and any(chosen_index(value) for value in node)

            return {"explain": explain, "uses_hnsw": chosen_index(explain)}
        except Exception:
            raise PgContextError("Local pgContext experiment plan inspection failed") from None

    def close(self, cleanup: bool = True) -> None:
        conn = self._connection
        if conn is None:
            return
        failure = None
        try:
            if cleanup and self._created:
                with conn.transaction():
                    conn.execute("SELECT pgcontext.drop_collection(%s)", (self.collection,))
                    conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        except Exception:
            failure = PgContextError(f"Experiment cleanup failed; isolated schema may remain: {self.schema}")
        finally:
            try:
                conn.close()
            except Exception:
                failure = failure or PgContextError("Local pgContext connection close failed")
            self._connection = None
            self._created = False
        if failure is not None:
            raise failure from None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except PgContextError as cleanup_error:
            if exc_value is None:
                raise
            exc_value.add_note(str(cleanup_error))
