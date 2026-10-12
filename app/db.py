"""Database access: schema introspection, read-only execution, and query-plan cost estimation."""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings

# Text columns with few distinct values get their values listed in the prompt so the
# model writes WHERE State = 'California' instead of guessing 'CA'.
_ENUM_MAX_DISTINCT = 12


@dataclass
class Column:
    name: str
    type: str
    primary_key: bool
    not_null: bool
    sample_values: list[str] = field(default_factory=list)


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class Table:
    name: str
    columns: list[Column]
    foreign_keys: list[ForeignKey]
    indexes: dict[str, list[str]]  # index name -> columns
    row_count: int

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name.lower() == name.lower()), None)

    def indexed_leading_columns(self) -> set[str]:
        cols = {c.name.lower() for c in self.columns if c.primary_key}
        cols.update(idx[0].lower() for idx in self.indexes.values() if idx)
        return cols


@dataclass
class SchemaCatalog:
    tables: dict[str, Table]  # keyed by lower-case table name

    def table(self, name: str) -> Table | None:
        return self.tables.get(name.lower())

    def as_sqlglot_schema(self) -> dict[str, dict[str, str]]:
        return {t.name: {c.name: c.type or "TEXT" for c in t.columns} for t in self.tables.values()}

    def relationships(self) -> set[frozenset[tuple[str, str]]]:
        """Every declared FK edge as an unordered pair of (table, column), lower-cased."""
        rels = set()
        for t in self.tables.values():
            for fk in t.foreign_keys:
                rels.add(frozenset({(t.name.lower(), fk.column.lower()), (fk.ref_table.lower(), fk.ref_column.lower())}))
        return rels

    def to_prompt(self, table_names: list[str] | None = None) -> str:
        """Compact, LLM-friendly DDL-ish description of (a subset of) the schema."""
        chosen = [self.tables[n.lower()] for n in table_names] if table_names else list(self.tables.values())
        lines = []
        for t in chosen:
            lines.append(f"TABLE {t.name}  -- {t.row_count} rows")
            for c in t.columns:
                bits = [c.type or "TEXT"]
                if c.primary_key:
                    bits.append("PRIMARY KEY")
                if c.not_null and not c.primary_key:
                    bits.append("NOT NULL")
                fk = next((f for f in t.foreign_keys if f.column == c.name), None)
                if fk:
                    bits.append(f"REFERENCES {fk.ref_table}({fk.ref_column})")
                if c.sample_values:
                    bits.append("values: " + ", ".join(repr(v) for v in c.sample_values))
                lines.append(f"  {c.name} {' '.join(bits)}")
            if t.indexes:
                lines.append("  indexes: " + "; ".join(f"{n}({', '.join(cols)})" for n, cols in t.indexes.items()))
            lines.append("")
        return "\n".join(lines).strip()


def _connect_ro(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.db_path).resolve()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    return conn


@lru_cache(maxsize=1)
def get_catalog() -> SchemaCatalog:
    return load_catalog()


def load_catalog(db_path: Path | None = None) -> SchemaCatalog:
    conn = _connect_ro(db_path)
    try:
        tables: dict[str, Table] = {}
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
        for name in names:
            cols = [Column(name=r[1], type=(r[2] or "").upper(), not_null=bool(r[3]), primary_key=bool(r[5]))
                    for r in conn.execute(f'PRAGMA table_info("{name}")')]
            fks = [ForeignKey(column=r[3], ref_table=r[2], ref_column=r[4])
                   for r in conn.execute(f'PRAGMA foreign_key_list("{name}")')]
            indexes = {}
            for r in conn.execute(f'PRAGMA index_list("{name}")'):
                idx_cols = [ir[2] for ir in conn.execute(f'PRAGMA index_info("{r[1]}")')]
                indexes[r[1]] = idx_cols
            row_count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for c in cols:
                if c.type in ("TEXT", "") and not c.primary_key:
                    vals = [r[0] for r in conn.execute(
                        f'SELECT DISTINCT "{c.name}" FROM "{name}" WHERE "{c.name}" IS NOT NULL LIMIT {_ENUM_MAX_DISTINCT + 1}')]
                    if 0 < len(vals) <= _ENUM_MAX_DISTINCT and row_count > len(vals) * 2:
                        c.sample_values = sorted(str(v) for v in vals)
            tables[name.lower()] = Table(name, cols, fks, indexes, row_count)
        return SchemaCatalog(tables)
    finally:
        conn.close()


# ---------------------------------------------------------------- execution

_READ_ONLY_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _READ_ONLY_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)


def _authorizer(action: int, *_: Any) -> int:
    # Defence in depth: even if a write slipped past the validator, SQLite refuses to prepare it.
    return sqlite3.SQLITE_OK if action in _READ_ONLY_ACTIONS else sqlite3.SQLITE_DENY


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float


def execute_readonly(sql: str, max_rows: int | None = None, db_path: Path | None = None) -> QueryResult:
    max_rows = max_rows or settings.max_result_rows
    conn = _connect_ro(db_path)
    conn.set_authorizer(_authorizer)
    deadline = time.monotonic() + settings.query_timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        start = time.perf_counter()
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows + 1)
        elapsed = (time.perf_counter() - start) * 1000
        columns = [d[0] for d in cur.description or []]
        truncated = len(rows) > max_rows
        rows = [list(r) for r in rows[:max_rows]]
        return QueryResult(columns, rows, len(rows), truncated, round(elapsed, 2))
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise sqlite3.OperationalError(f"Query exceeded the {settings.query_timeout_s:g}s time limit") from e
        raise
    finally:
        conn.close()


def check_with_engine(sql: str, db_path: Path | None = None) -> str | None:
    """Ask SQLite itself to compile the query (without running it). Returns an error message or None."""
    conn = _connect_ro(db_path)
    conn.set_authorizer(_authorizer)
    try:
        conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
        return None
    except sqlite3.Error as e:
        msg = str(e)
        return "Statement is not read-only" if msg == "not authorized" else msg
    finally:
        conn.close()


# ---------------------------------------------------------------- cost estimation

_PLAN_STEP_RE = re.compile(r"^(SCAN|SEARCH) (?:TABLE )?(\w+)", re.I)


def _alias_map(sql: str) -> dict[str, str]:
    """alias -> table name, so plan steps like 'SCAN o' resolve to Orders."""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return {}
    return {t.alias_or_name.lower(): t.name for t in tree.find_all(exp.Table)}


def estimate_cost(sql: str, catalog: SchemaCatalog | None = None, db_path: Path | None = None) -> dict[str, Any]:
    """Heuristic cost estimate from SQLite's EXPLAIN QUERY PLAN plus table row counts."""
    catalog = catalog or get_catalog()
    conn = _connect_ro(db_path)
    conn.set_authorizer(_authorizer)
    try:
        plan = [r[3] for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()]
    finally:
        conn.close()

    aliases = _alias_map(sql)
    full_scans, index_lookups, est_rows, temp_btrees = [], [], 0, 0
    for step in plan:
        upper = step.upper()
        if m := _PLAN_STEP_RE.match(step):
            kind, name = m.group(1).upper(), m.group(2)
            table_name = aliases.get(name.lower(), name)
            t = catalog.table(table_name)
            rows = t.row_count if t else 1000
            if kind == "SCAN" and "USING" not in upper:
                full_scans.append(t.name if t else table_name)
                est_rows += rows
            elif kind == "SCAN":  # full pass, but over an index
                index_lookups.append(step)
                est_rows += rows // 2
            else:
                index_lookups.append(step)
                est_rows += max(1, int(rows ** 0.5))
        if "TEMP B-TREE" in upper:
            temp_btrees += 1

    # Nested full scans multiply; approximate by penalising >1 scan.
    if len(full_scans) > 1:
        est_rows = int(est_rows * (1 + 0.5 * (len(full_scans) - 1)))
    score = est_rows + temp_btrees * 200
    level = "low" if score < 500 else "medium" if score < 5000 else "high"
    return {
        "level": level,
        "estimated_rows_examined": est_rows,
        "full_table_scans": full_scans,
        "index_lookups": len(index_lookups),
        "temp_sorts": temp_btrees,
        "plan": plan,
    }
