"""Deterministic SQL optimization analysis: performance hints, index recommendations, cost estimate.

These run on every query the agent returns, so suggestions are grounded in the real schema and
SQLite's query plan, not only the LLM's opinion.
"""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from app.db import SchemaCatalog, estimate_cost, get_catalog

_RANGE_OPS = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between, exp.Like)


def analyze(sql: str, catalog: SchemaCatalog | None = None) -> dict[str, Any]:
    catalog = catalog or get_catalog()
    hints: list[str] = []
    index_recs: list[str] = []
    unnecessary_joins: list[str] = []

    try:
        cost = estimate_cost(sql, catalog)
    except Exception as e:  # never fail the request because of the estimator
        cost = {"level": "unknown", "error": str(e), "plan": []}

    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
        q = qualify(tree.copy(), schema=catalog.as_sqlglot_schema(), dialect="sqlite", identify=False)
    except Exception:
        return {"hints": hints, "index_recommendations": index_recs, "unnecessary_joins": unnecessary_joins, "cost": cost}

    alias_to_table = {t.alias_or_name.lower(): catalog.table(t.name).name
                      for t in q.find_all(exp.Table) if catalog.table(t.name)}

    # SELECT *
    if any(isinstance(e, exp.Star) for s in tree.find_all(exp.Select) for e in s.expressions):
        hints.append("Avoid SELECT * — list only the columns you need to reduce I/O and keep results stable "
                     "if the schema changes.")

    # Missing LIMIT on large, non-aggregated result sets
    if isinstance(tree, exp.Select) and not tree.args.get("limit") and not tree.args.get("group"):
        has_agg = any(isinstance(e, exp.AggFunc) for e in tree.expressions for e in e.walk())
        biggest = max((catalog.table(t).row_count for t in alias_to_table.values()), default=0)
        if not has_agg and biggest > 200:
            hints.append("Consider adding a LIMIT (or pagination) — this query can return many rows.")

    # Non-sargable predicates and leading wildcards
    for where in q.find_all(exp.Where):
        for pred in where.find_all(*_RANGE_OPS):
            if isinstance(pred.this, exp.Func) and pred.this.find(exp.Column):
                col = pred.this.find(exp.Column)
                table = catalog.table(alias_to_table.get(col.table.lower(), ""))
                name = (table.column(col.name).name if table and table.column(col.name) else col.name)
                func = pred.this.sql("sqlite").split("(")[0].upper()
                hints.append(f"Wrapping {name} in {func}() inside WHERE prevents index use — compare the raw "
                             f"column instead (e.g. {name} >= '2024-01-01' AND {name} < '2025-01-01').")
            if isinstance(pred, exp.Like):
                pattern = pred.expression
                if isinstance(pattern, exp.Literal) and pattern.this.startswith("%"):
                    hints.append("LIKE patterns starting with '%' cannot use an index and force a full scan.")

    # Index recommendations: filter / join columns on tables the plan fully scans
    scanned = {s.lower() for s in cost.get("full_table_scans", [])}
    candidates: dict[str, list[str]] = {}
    for pred in q.find_all(*_RANGE_OPS):
        if isinstance(pred, exp.Like) and isinstance(pred.expression, exp.Literal) and pred.expression.this.startswith("%"):
            continue
        col = pred.this if isinstance(pred.this, exp.Column) else None
        if col is None or not col.table:
            continue
        table_name = alias_to_table.get(col.table.lower())
        if not table_name or table_name.lower() not in scanned:
            continue
        table = catalog.table(table_name)
        if col.name.lower() in table.indexed_leading_columns():
            continue
        if table.row_count < 100:
            continue
        candidates.setdefault(table.name, [])
        real = table.column(col.name)
        if real and real.name not in candidates[table.name]:
            candidates[table.name].append(real.name)
    for table, cols in candidates.items():
        index_recs.append(f"CREATE INDEX idx_{table.lower()}_{'_'.join(c.lower() for c in cols[:2])} "
                          f"ON {table}({', '.join(cols[:2])});")

    # Unnecessary joins: the joined table contributes no columns AND is joined on its primary key
    # (so it cannot multiply rows). LEFT JOIN → always removable; INNER JOIN → removable when the
    # other side is a NOT NULL foreign key (every row is guaranteed exactly one match).
    for select in q.find_all(exp.Select):
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            if not isinstance(join.this, exp.Table) or on is None or not catalog.table(join.this.name):
                continue
            joined = catalog.table(join.this.name)
            alias = join.this.alias_or_name.lower()
            if any(c.table.lower() == alias and not _within(c, on) for c in select.find_all(exp.Column)):
                continue
            pk_match, fk_guaranteed = False, False
            for eq in on.find_all(exp.EQ):
                cols = [eq.this, eq.expression]
                if not all(isinstance(c, exp.Column) for c in cols):
                    continue
                mine = next((c for c in cols if c.table.lower() == alias), None)
                other = next((c for c in cols if c.table.lower() != alias), None)
                if mine is None or other is None:
                    continue
                if (jc := joined.column(mine.name)) and jc.primary_key:
                    pk_match = True
                    other_table = catalog.table(alias_to_table.get(other.table.lower(), ""))
                    oc = other_table.column(other.name) if other_table else None
                    fk_guaranteed = bool(oc and oc.not_null and any(
                        fk.column.lower() == oc.name.lower() and fk.ref_table.lower() == joined.name.lower()
                        for fk in other_table.foreign_keys))
            if not pk_match:
                continue
            side = (join.args.get("side") or "").upper()
            if side == "LEFT" or fk_guaranteed:
                unnecessary_joins.append(
                    f"JOIN {joined.name} is unnecessary — none of its columns are used and it is joined on its "
                    "primary key, so removing it does not change the result.")
            else:
                unnecessary_joins.append(
                    f"JOIN {joined.name} contributes no columns and only acts as an existence filter; "
                    "remove it unless that filtering is intended.")

    if cost.get("temp_sorts"):
        hints.append("The plan uses a temporary B-tree for sorting/grouping; an index on the ORDER BY / GROUP BY "
                     "columns can avoid it on large tables.")

    return {
        "hints": list(dict.fromkeys(hints)),
        "index_recommendations": index_recs,
        "unnecessary_joins": unnecessary_joins,
        "cost": cost,
    }


_INDEX_REC_RE = re.compile(r"\bON\s+[\"`\[]?(\w+)[\"`\]]?\s*\(\s*[\"`\[]?(\w+)", re.I)


def drop_existing_indexes(recs: list[str], catalog: SchemaCatalog | None = None) -> list[str]:
    """Remove CREATE INDEX advice whose leading column is already indexed (or is the primary key)."""
    catalog = catalog or get_catalog()
    kept = []
    for rec in recs:
        m = _INDEX_REC_RE.search(rec)
        table = catalog.table(m.group(1)) if m else None
        if table and m.group(2).lower() in table.indexed_leading_columns():
            continue
        kept.append(rec)
    return kept


def _within(node: exp.Expression, ancestor: exp.Expression) -> bool:
    p = node
    while p is not None:
        if p is ancestor:
            return True
        p = p.parent
    return False
