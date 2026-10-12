"""Deterministic SQL validation — the agent's hard guardrail.

Every SQL statement the agent returns passes through `validate_sql`, regardless of what the LLM
said. Checks, in order:
  1. syntax        – parses as exactly one SQLite statement
  2. read_only     – SELECT / WITH … SELECT / set operations only; no DML/DDL/PRAGMA/ATTACH
  3. tables        – every referenced table exists in the schema (CTE names excepted)
  4. columns       – every column resolves against the schema (via sqlglot's qualifier)
  5. joins         – join keys are type-compatible; non-FK joins and cartesian products are flagged
  6. engine        – SQLite compiles the statement (EXPLAIN QUERY PLAN) without running it
"""
from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.tokens import TokenType

from app.db import SchemaCatalog, check_with_engine, get_catalog

FORBIDDEN_KEYWORDS = {
    "DELETE", "UPDATE", "INSERT", "DROP", "ALTER", "TRUNCATE", "CREATE", "MERGE", "UPSERT",
    "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX", "GRANT", "REVOKE", "EXEC", "EXECUTE", "CALL",
}
FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter, exp.Create, exp.Command,
    exp.TruncateTable, exp.Merge, exp.Pragma, exp.Attach, exp.Detach, exp.Grant,
)
READ_ONLY_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_NUMERIC = ("INT", "REAL", "NUM", "DEC", "FLOA", "DOUB")


@dataclass
class ValidationReport:
    sql: str
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, bool | None] = field(default_factory=lambda: {
        "syntax": None, "read_only": None, "tables": None, "columns": None, "joins": None, "engine": None})
    tables: list[str] = field(default_factory=list)
    destructive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fail(self, check: str, message: str) -> "ValidationReport":
        self.checks[check] = False
        self.errors.append(message)
        self.ok = False
        return self


def clean_sql(sql: str) -> str:
    sql = sql.strip()
    sql = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.I).strip()
    return sql.rstrip(";").strip()


def find_forbidden_keywords(sql: str) -> list[str]:
    """Keyword scan on tokens (so string literals like 'Deleted' or column names don't trigger)."""
    try:
        tokens = sqlglot.tokenize(sql, read="sqlite")
    except (TokenError, Exception):
        return sorted({w for w in re.findall(r"[A-Za-z_]+", sql.upper()) if w in FORBIDDEN_KEYWORDS})
    found = {t.text.upper() for t in tokens
             if t.token_type not in (TokenType.STRING, TokenType.IDENTIFIER) and t.text.upper() in FORBIDDEN_KEYWORDS}
    return sorted(found)


def _suggest(name: str, options: list[str]) -> str:
    match = difflib.get_close_matches(name.lower(), [o.lower() for o in options], n=1, cutoff=0.6)
    if not match:
        return ""
    original = next(o for o in options if o.lower() == match[0])
    return f" Did you mean '{original}'?"


def _is_numeric(type_: str) -> bool:
    return any(k in type_.upper() for k in _NUMERIC)


def validate_sql(sql: str, catalog: SchemaCatalog | None = None, engine_check: bool = True) -> ValidationReport:
    catalog = catalog or get_catalog()
    sql = clean_sql(sql or "")
    report = ValidationReport(sql=sql)
    if not sql:
        return report.fail("syntax", "Empty SQL statement.")

    # ---- 2a. fast keyword guard (runs before parsing so even unparsable writes are caught)
    forbidden = find_forbidden_keywords(sql)
    if forbidden:
        report.destructive = True
        report.fail("read_only", f"Only read-only queries are allowed; found forbidden operation(s): {', '.join(forbidden)}.")

    # ---- 1. syntax
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except (ParseError, TokenError) as e:
        # SQLite's own message ('near "SELEC": syntax error') is usually clearer than the parser's.
        engine_msg = check_with_engine(sql) if not report.destructive else None
        msg = engine_msg or _ANSI.sub("", str(e)).split("\n")[0]
        return report.fail("syntax", f"Syntax error: {msg}")
    if len(statements) != 1:
        return report.fail("syntax", "Exactly one SQL statement is allowed per query.")
    tree = statements[0]
    report.checks["syntax"] = True

    # ---- 2b. structural read-only check
    if not isinstance(tree, READ_ONLY_ROOTS) or any(isinstance(n, FORBIDDEN_NODES) for n in tree.walk()):
        report.destructive = True
        if report.checks["read_only"] is not False:
            report.fail("read_only", "Only read-only SELECT queries are allowed.")
    if report.checks["read_only"] is False:
        return report
    report.checks["read_only"] = True

    # ---- 3. tables
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    known = [t.name for t in catalog.tables.values()]
    referenced, unknown = [], []
    for t in tree.find_all(exp.Table):
        name = t.name
        if not name or name.lower() in cte_names:
            continue
        table = catalog.table(name)
        if table is None:
            unknown.append(name)
        elif table.name not in referenced:
            referenced.append(table.name)
    report.tables = referenced
    if unknown:
        for name in dict.fromkeys(unknown):
            report.errors.append(f"Table '{name}' does not exist.{_suggest(name, known)} "
                                 f"Available tables: {', '.join(known)}.")
        report.checks["tables"] = False
        return report
    report.checks["tables"] = True

    # ---- 4. columns
    try:
        qualified = qualify(tree.copy(), schema=catalog.as_sqlglot_schema(), dialect="sqlite",
                            validate_qualify_columns=True, identify=False)
    except OptimizeError as e:
        msg = str(e)
        col = re.search(r"Column '([^']+)'", msg)
        if col:  # the qualifier lower-cases identifiers; show the user's spelling
            original = re.search(rf"\b{re.escape(col.group(1))}\b", sql, re.I)
            if original:
                msg = msg.replace(f"'{col.group(1)}'", f"'{original.group(0)}'", 1)
        hint = ""
        if col:
            all_cols = sorted({c.name for t in referenced for c in catalog.table(t).columns})
            hint = _suggest(col.group(1), all_cols)
        return report.fail("columns", f"{msg.split('. Line')[0]}.{hint} "
                                      f"Valid columns for {', '.join(referenced)}: " +
                           "; ".join(f"{t}({', '.join(c.name for c in catalog.table(t).columns)})" for t in referenced))
    except Exception as e:  # sqlglot can raise on exotic syntax; let the engine be the judge
        report.warnings.append(f"Column resolution skipped: {e}")
        qualified = None
    report.checks["columns"] = True

    # ---- 5. joins
    report.checks["joins"] = True
    if qualified is not None:
        _check_joins(qualified, catalog, report)
        if report.checks["joins"] is False:
            return report

    # ---- 6. engine compile check
    if engine_check:
        err = check_with_engine(sql)
        if err:
            return report.fail("engine", f"SQLite rejected the query: {err}")
        report.checks["engine"] = True

    report.ok = not report.errors
    return report


def _check_joins(tree: exp.Expression, catalog: SchemaCatalog, report: ValidationReport) -> None:
    rels = catalog.relationships()
    for select in tree.find_all(exp.Select):
        alias_to_table = {}
        for t in select.find_all(exp.Table):
            if catalog.table(t.name):
                alias_to_table[t.alias_or_name.lower()] = catalog.table(t.name).name
        for join in select.args.get("joins") or []:
            on, using = join.args.get("on"), join.args.get("using")
            target = join.this.name if isinstance(join.this, exp.Table) else "subquery"
            if on is None and not using:
                alias = join.this.alias_or_name.lower()
                where = select.args.get("where")
                linked = where is not None and any(
                    isinstance(eq.this, exp.Column) and isinstance(eq.expression, exp.Column)
                    and alias in (eq.this.table.lower(), eq.expression.table.lower())
                    for eq in where.find_all(exp.EQ))
                if linked:
                    report.warnings.append(
                        f"'{target}' is joined implicitly via WHERE — prefer an explicit JOIN … ON for readability.")
                else:
                    report.warnings.append(
                        f"'{target}' is joined without any join condition — this produces a cartesian product.")
                continue
            if on is None:
                continue
            fk_backed = False
            for eq in on.find_all(exp.EQ):
                left, right = eq.this, eq.expression
                if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                    continue
                lt, rt = alias_to_table.get(left.table.lower()), alias_to_table.get(right.table.lower())
                if not (lt and rt):
                    fk_backed = True  # involves a CTE/subquery; can't judge, don't nag
                    continue
                lc, rc = catalog.table(lt).column(left.name), catalog.table(rt).column(right.name)
                if lc and rc and _is_numeric(lc.type) != _is_numeric(rc.type):
                    report.fail("joins", f"Invalid join: {lt}.{lc.name} ({lc.type}) and {rt}.{rc.name} ({rc.type}) "
                                         "have incompatible types.")
                    return
                pair = frozenset({(lt.lower(), left.name.lower()), (rt.lower(), right.name.lower())})
                if pair in rels:
                    fk_backed = True
            if not fk_backed:
                report.warnings.append(
                    f"JOIN with '{target}' is not on a declared foreign-key relationship — verify the join keys.")


def transpile(sql: str, dialects: tuple[str, ...] = ("sqlite", "postgres", "mysql")) -> dict[str, str]:
    """Render a validated SQLite query in other dialects (bonus: multi-dialect support)."""
    out = {}
    for d in dialects:
        try:
            out[d] = sqlglot.transpile(sql, read="sqlite", write=d, pretty=True)[0]
        except Exception:
            out[d] = sql if d == "sqlite" else f"-- could not transpile to {d}\n{sql}"
    return out


def pretty(sql: str) -> str:
    try:
        return sqlglot.transpile(sql, read="sqlite", write="sqlite", pretty=True)[0]
    except Exception:
        return sql


_SQL_START = re.compile(r"(?is)\b(SELECT|WITH|DELETE|UPDATE|INSERT|DROP|ALTER|TRUNCATE)\b\s+.+")


def extract_sql(text: str) -> str | None:
    """Pull an SQL statement out of a chat message (fenced block first, then a bare statement)."""
    fence = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    if fence:
        return clean_sql(fence.group(1))
    m = _SQL_START.search(text)
    if m and re.search(r"(?i)\b(FROM|TABLE|INTO|SET)\b", m.group(0)):
        return clean_sql(m.group(0))
    return None
