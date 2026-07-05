"""
Minimal Supabase-shaped psycopg2 client for exercising real V48 RPCs and
table reads against a disposable/test PostgreSQL database.

This module is a thin *protocol adapter* only: it translates the
Supabase-py calling convention (``client.rpc(name, params).execute()`` /
``client.table(name).select(...).eq(...).execute()``) into plain SQL over a
``psycopg2`` connection. It contains zero V48 audit decision logic — no pass
sequencing, no dispute handling, no completion-shape logic, no persistence
rules. Those all continue to live exclusively in the real PostgreSQL RPCs
(``supabase/migrations/20260630130000_v48_ai_quality_audit_rpcs.sql``) and in
``workers.ai_quality_audit_worker`` / ``workers.ai_quality_audit_context``,
completely unmodified.

Originally authored inline in ``tests/test_ai_quality_audit_integration.py``.
Moved here (V58-QUALITY-04C) so non-test runtime code
(``workers.quality_benchmark_v48_orchestration``) can reuse it without
importing from a test module. ``tests/test_ai_quality_audit_integration.py``
now imports ``PsycopgV48Client`` from here instead of defining its own copy.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover - exercised only when psycopg2 absent
    psycopg2 = None  # type: ignore

_UUID_ARG_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def is_psycopg2_available() -> bool:
    """Return True if the optional ``psycopg2`` dependency is importable."""
    return psycopg2 is not None


def _adapt_rpc_arg(value: object):
    if isinstance(value, list):
        if value and all(
            isinstance(item, str) and _UUID_ARG_RE.match(item) for item in value
        ):
            elements = ",".join(f"'{item}'::uuid" for item in value)
            return psycopg2.extensions.AsIs(f"ARRAY[{elements}]")
        return psycopg2.extras.Json(value)
    if isinstance(value, dict):
        return psycopg2.extras.Json(value)
    return value


class RpcResult:
    """Mirrors the ``.data``/``.error`` shape of a Supabase-py response."""

    def __init__(self, data, error=None):
        self.data = data
        self.error = error


class _PsycopgRpcBuilder:
    def __init__(self, rows: List[dict], error=None):
        self._rows = rows
        self._error = error

    def execute(self):
        return RpcResult(self._rows, self._error)


class PsycopgV48Client:
    """Minimal Supabase-shaped client over psycopg2 for V48 RPC + table reads.

    Every RPC call is translated verbatim into ``SELECT * FROM
    public.<rpc_name>(<named args>)`` against whatever connection is passed
    in — the RPC itself (and therefore all V48 decision logic) always runs
    inside real PostgreSQL, unchanged.
    """

    def __init__(self, conn):
        self.conn = conn

    def rpc(self, name: str, params: dict):
        args = [_adapt_rpc_arg(value) for value in params.values()]
        placeholders = ", ".join(f"{key} => %s" for key in params)
        sql = f"SELECT * FROM public.{name}({placeholders})"
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(row) for row in cur.fetchall()]
        return _PsycopgRpcBuilder(rows)

    def table(self, name: str):
        return _PsycopgTableQuery(self.conn, name)


class _PsycopgTableQuery:
    def __init__(self, conn, table_name: str):
        self.conn = conn
        self.table_name = table_name
        self.select_fields = "*"
        self.filters: List[tuple] = []
        self.order_field: Optional[str] = None
        self.limit_count: Optional[int] = None

    def select(self, fields: str):
        self.select_fields = fields
        return self

    def eq(self, field: str, value: object):
        self.filters.append(("eq", field, value))
        return self

    def in_(self, field: str, values: list):
        self.filters.append(("in", field, tuple(values)))
        return self

    def order(self, field: str):
        self.order_field = field
        return self

    def limit(self, count: int):
        self.limit_count = count
        return self

    def execute(self):
        if (
            self.table_name == "resource_chunks"
            and "resource_versions(" in self.select_fields
        ):
            return self._execute_resource_chunks_nested_query()
        sql = f"SELECT {self.select_fields} FROM public.{self.table_name}"
        args: List[Any] = []
        if self.filters:
            clauses = []
            for op, field, value in self.filters:
                if op == "eq":
                    clauses.append(f"{field} = %s")
                    args.append(value)
                elif op == "in":
                    placeholders = ", ".join(["%s"] * len(value))
                    clauses.append(f"{field} IN ({placeholders})")
                    args.extend(value)
            sql += " WHERE " + " AND ".join(clauses)
        if self.order_field:
            sql += f" ORDER BY {self.order_field}"
        if self.limit_count is not None:
            sql += " LIMIT %s"
            args.append(self.limit_count)
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(row) for row in cur.fetchall()]
        return RpcResult(rows)

    def _execute_resource_chunks_nested_query(self) -> RpcResult:
        clauses: List[str] = []
        args: List[Any] = []
        for op, field, value in self.filters:
            if op == "eq":
                clauses.append(f"rc.{field} = %s")
                args.append(value)
            elif op == "in":
                placeholders = ", ".join(["%s"] * len(value))
                clauses.append(f"rc.id IN ({placeholders})")
                args.extend(value)
        sql = """
            SELECT
                rc.id,
                rc.chunk_text,
                rc.resource_version_id,
                rv.resource_id,
                rv.version_number,
                ors.id AS official_resource_id,
                ors.title,
                ors.certification_exam_name
            FROM public.resource_chunks rc
            JOIN public.resource_versions rv ON rv.id = rc.resource_version_id
            JOIN public.official_resources ors ON ors.id = rv.resource_id
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            raw_rows = [dict(row) for row in cur.fetchall()]
        rows = [
            {
                "id": row["id"],
                "chunk_text": row["chunk_text"],
                "resource_version_id": row["resource_version_id"],
                "resource_versions": {
                    "resource_id": row["resource_id"],
                    "version_number": row["version_number"],
                    "official_resources": {
                        "id": row["official_resource_id"],
                        "title": row["title"],
                        "certification_exam_name": row["certification_exam_name"],
                    },
                },
            }
            for row in raw_rows
        ]
        return RpcResult(rows)


__all__ = [
    "is_psycopg2_available",
    "RpcResult",
    "PsycopgV48Client",
]
