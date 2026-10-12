import pytest

from app.sql_guard import extract_sql, transpile, validate_sql


@pytest.mark.parametrize("sql", [
    "SELECT * FROM Employees WHERE HireDate >= '2024-01-01'",
    "select e.firstname, d.DepartmentName from employees e join departments d on e.DepartmentID = d.DepartmentID",
    "WITH t AS (SELECT CustomerID, COUNT(*) n FROM Orders GROUP BY CustomerID) "
    "SELECT c.State, t.n FROM t JOIN Customers c ON c.CustomerID = t.CustomerID",
    "SELECT State, COUNT(*) AS cnt FROM Customers GROUP BY State HAVING cnt > 5 ORDER BY cnt DESC",
    "SELECT FirstName FROM Customers UNION SELECT FirstName FROM Employees",
    "SELECT e.FirstName, m.FirstName AS Manager FROM Employees e LEFT JOIN Employees m ON e.ManagerID = m.EmployeeID",
    "SELECT * FROM Orders WHERE Status = 'Deleted';",   # forbidden word inside a string literal is fine
    "SELECT strftime('%Y', OrderDate) AS yr, SUM(TotalAmount) FROM Orders GROUP BY yr",
])
def test_valid_queries(sql):
    r = validate_sql(sql)
    assert r.ok, r.errors
    assert all(v for v in r.checks.values())


@pytest.mark.parametrize("sql", [
    "DELETE FROM Orders", "UPDATE Employees SET Salary = 0", "INSERT INTO Categories VALUES (99, 'x')",
    "DROP TABLE Orders", "ALTER TABLE Orders ADD COLUMN x INT", "TRUNCATE TABLE Orders",
    "SELECT 1; DROP TABLE Orders", "PRAGMA table_info(Orders)", "ATTACH DATABASE 'x.db' AS x",
    "CREATE TABLE x (a INT)", "WITH d AS (DELETE FROM Orders RETURNING *) SELECT * FROM d",
])
def test_destructive_queries_rejected(sql):
    r = validate_sql(sql)
    assert not r.ok and r.destructive and r.checks["read_only"] is False


def test_unknown_table_with_suggestion():
    r = validate_sql("SELECT * FROM Employee")
    assert not r.ok and r.checks["tables"] is False
    assert "Did you mean 'Employees'" in r.errors[0]


def test_system_tables_are_not_in_schema():
    assert not validate_sql("SELECT * FROM sqlite_master").ok


def test_unknown_column_with_suggestion():
    r = validate_sql("SELECT FirstNme FROM Employees")
    assert not r.ok and r.checks["columns"] is False
    assert "Did you mean 'FirstName'" in r.errors[0]


def test_column_on_wrong_table():
    r = validate_sql("SELECT c.Salary FROM Customers c")
    assert not r.ok and r.checks["columns"] is False


def test_syntax_error_uses_engine_message():
    r = validate_sql("SELEC * FROM Orders")
    assert not r.ok and r.checks["syntax"] is False and "syntax error" in r.errors[0].lower()


def test_multiple_statements_rejected():
    r = validate_sql("SELECT 1; SELECT 2")
    assert not r.ok and "one SQL statement" in r.errors[0]


def test_join_type_mismatch_is_error():
    r = validate_sql("SELECT c.FirstName FROM Customers c JOIN Orders o ON o.Status = c.CustomerID")
    assert not r.ok and r.checks["joins"] is False


def test_non_fk_join_and_cartesian_are_warnings():
    r = validate_sql("SELECT c.FirstName FROM Customers c JOIN Orders o ON o.EmployeeID = c.CustomerID")
    assert r.ok and any("foreign-key" in w for w in r.warnings)
    r = validate_sql("SELECT * FROM Customers, Categories")
    assert r.ok and any("cartesian" in w for w in r.warnings)


def test_engine_catches_unknown_function():
    r = validate_sql("SELECT nosuchfunc(FirstName) FROM Customers")
    assert not r.ok and r.checks["engine"] is False


def test_transpile_dialects():
    out = transpile("SELECT FirstName FROM Customers LIMIT 5")
    assert set(out) == {"sqlite", "postgres", "mysql"}
    assert "LIMIT 5" in out["postgres"]


def test_extract_sql():
    assert extract_sql("fix this ```sql\nSELECT * FROM Orders;\n```") == "SELECT * FROM Orders"
    assert extract_sql("why does SELECT x FROM y fail?").startswith("SELECT x FROM y")
    assert extract_sql("show me all customers") is None
