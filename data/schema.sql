-- Sample "Retail Co." database used by the SQL Query AI Agent.
-- SQLite dialect. The agent may ONLY reference the tables and columns below.

CREATE TABLE Departments (
    DepartmentID   INTEGER PRIMARY KEY,
    DepartmentName TEXT    NOT NULL UNIQUE,
    Location       TEXT    NOT NULL           -- office city
);

CREATE TABLE Employees (
    EmployeeID   INTEGER PRIMARY KEY,
    FirstName    TEXT    NOT NULL,
    LastName     TEXT    NOT NULL,
    Email        TEXT    NOT NULL UNIQUE,
    JobTitle     TEXT    NOT NULL,
    HireDate     DATE    NOT NULL,            -- ISO-8601 'YYYY-MM-DD'
    Salary       REAL    NOT NULL,            -- annual, USD
    DepartmentID INTEGER NOT NULL REFERENCES Departments(DepartmentID),
    ManagerID    INTEGER          REFERENCES Employees(EmployeeID)  -- NULL for top-level managers
);

CREATE TABLE Customers (
    CustomerID INTEGER PRIMARY KEY,
    FirstName  TEXT NOT NULL,
    LastName   TEXT NOT NULL,
    Email      TEXT NOT NULL UNIQUE,
    City       TEXT NOT NULL,
    State      TEXT NOT NULL,                 -- full US state name, e.g. 'California'
    Country    TEXT NOT NULL DEFAULT 'USA',
    SignupDate DATE NOT NULL
);

CREATE TABLE Categories (
    CategoryID   INTEGER PRIMARY KEY,
    CategoryName TEXT NOT NULL UNIQUE
);

CREATE TABLE Products (
    ProductID    INTEGER PRIMARY KEY,
    ProductName  TEXT    NOT NULL,
    CategoryID   INTEGER NOT NULL REFERENCES Categories(CategoryID),
    UnitPrice    REAL    NOT NULL,
    UnitsInStock INTEGER NOT NULL,
    Discontinued INTEGER NOT NULL DEFAULT 0   -- 0 = active, 1 = discontinued
);

CREATE TABLE Orders (
    OrderID     INTEGER PRIMARY KEY,
    CustomerID  INTEGER NOT NULL REFERENCES Customers(CustomerID),
    EmployeeID  INTEGER NOT NULL REFERENCES Employees(EmployeeID),  -- sales rep
    OrderDate   DATE    NOT NULL,
    ShippedDate DATE,                                               -- NULL until shipped
    Status      TEXT    NOT NULL,             -- 'Pending' | 'Shipped' | 'Delivered' | 'Cancelled'
    TotalAmount REAL    NOT NULL
);

CREATE TABLE OrderItems (
    OrderItemID INTEGER PRIMARY KEY,
    OrderID     INTEGER NOT NULL REFERENCES Orders(OrderID),
    ProductID   INTEGER NOT NULL REFERENCES Products(ProductID),
    Quantity    INTEGER NOT NULL,
    UnitPrice   REAL    NOT NULL,             -- price at time of sale
    Discount    REAL    NOT NULL DEFAULT 0    -- fraction, 0.0 - 0.3
);

-- Indexes (some columns are deliberately left unindexed so the agent can recommend indexes)
CREATE INDEX idx_employees_department ON Employees(DepartmentID);
CREATE INDEX idx_orders_customer      ON Orders(CustomerID);
CREATE INDEX idx_orders_orderdate     ON Orders(OrderDate);
CREATE INDEX idx_orderitems_order     ON OrderItems(OrderID);
CREATE INDEX idx_products_category    ON Products(CategoryID);
