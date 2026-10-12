"""Builds the sample SQLite database from data/schema.sql with deterministic fake data.

Run directly (`python -m app.seed`) or let the API build it on first start.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from app.config import settings

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda", "David", "Elizabeth",
    "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Priya", "Arjun",
    "Wei", "Mei", "Carlos", "Sofia", "Ahmed", "Fatima", "Kenji", "Yuki", "Olivia", "Noah",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
    "Hernandez", "Lopez", "Wilson", "Anderson", "Taylor", "Thomas", "Moore", "Jackson", "Lee", "Patel",
    "Kumar", "Chen", "Nguyen", "Kim", "Khan", "Tanaka", "Clark", "Lewis", "Walker", "Hall",
]
STATES_CITIES = {
    "California": ["Los Angeles", "San Francisco", "San Diego", "Sacramento"],
    "Texas": ["Austin", "Houston", "Dallas"],
    "New York": ["New York City", "Buffalo", "Albany"],
    "Florida": ["Miami", "Orlando", "Tampa"],
    "Washington": ["Seattle", "Spokane"],
    "Illinois": ["Chicago", "Springfield"],
    "Massachusetts": ["Boston", "Cambridge"],
    "Colorado": ["Denver", "Boulder"],
    "Georgia": ["Atlanta", "Savannah"],
    "Arizona": ["Phoenix", "Tucson"],
}
DEPARTMENTS = [
    ("Sales", "New York City"), ("Engineering", "San Francisco"), ("Marketing", "Chicago"),
    ("Finance", "New York City"), ("Human Resources", "Austin"), ("Customer Support", "Phoenix"),
    ("Operations", "Atlanta"), ("Product", "Seattle"),
]
TITLES = {
    "Sales": ["Sales Representative", "Account Executive", "Sales Manager"],
    "Engineering": ["Software Engineer", "Senior Software Engineer", "Engineering Manager"],
    "Marketing": ["Marketing Specialist", "Content Strategist", "Marketing Manager"],
    "Finance": ["Accountant", "Financial Analyst", "Finance Manager"],
    "Human Resources": ["HR Generalist", "Recruiter", "HR Manager"],
    "Customer Support": ["Support Agent", "Support Specialist", "Support Manager"],
    "Operations": ["Operations Analyst", "Logistics Coordinator", "Operations Manager"],
    "Product": ["Product Manager", "Product Designer", "Head of Product"],
}
CATEGORIES = {
    "Electronics": [("Wireless Mouse", 24.99), ("Mechanical Keyboard", 89.0), ("27in Monitor", 279.0),
                    ("USB-C Hub", 39.5), ("Noise Cancelling Headphones", 199.0), ("Webcam HD", 59.0)],
    "Home & Kitchen": [("Coffee Maker", 79.0), ("Blender", 49.99), ("Cookware Set", 129.0),
                       ("Air Fryer", 99.0), ("Knife Set", 64.0)],
    "Books": [("SQL for Beginners", 29.99), ("Data Science Handbook", 44.0), ("The Pragmatic Analyst", 35.0),
              ("Mystery at Midnight", 14.99), ("Cooking Made Simple", 22.5)],
    "Sports & Outdoors": [("Yoga Mat", 25.0), ("Camping Tent", 149.0), ("Running Shoes", 89.99),
                          ("Water Bottle", 18.0), ("Dumbbell Set", 119.0)],
    "Office Supplies": [("Notebook Pack", 12.99), ("Gel Pens (12)", 9.5), ("Desk Organizer", 27.0),
                        ("Ergonomic Chair", 249.0), ("Standing Desk", 399.0)],
    "Toys & Games": [("Board Game Classic", 34.99), ("Puzzle 1000pc", 19.99), ("RC Car", 59.99),
                     ("Building Blocks", 44.99)],
}


def _rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def build_database(db_path: Path | None = None, schema_path: Path | None = None) -> Path:
    db_path = Path(db_path or settings.db_path)
    schema_path = Path(schema_path or settings.schema_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    rng = random.Random(42)
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_path.read_text(encoding="utf-8"))

    conn.executemany(
        "INSERT INTO Departments (DepartmentID, DepartmentName, Location) VALUES (?, ?, ?)",
        [(i + 1, name, loc) for i, (name, loc) in enumerate(DEPARTMENTS)],
    )

    # Employees: one manager per department first, then staff reporting to them.
    employees, emp_id, used_emails = [], 1, set()

    def email_for(first: str, last: str, domain: str) -> str:
        base = f"{first}.{last}".lower()
        candidate, n = f"{base}@{domain}", 1
        while candidate in used_emails:
            n += 1
            candidate = f"{base}{n}@{domain}"
        used_emails.add(candidate)
        return candidate

    managers = {}
    for dept_id, (dept, _) in enumerate(DEPARTMENTS, start=1):
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        hire = _rand_date(rng, date(2018, 1, 1), date(2021, 12, 31))
        employees.append((emp_id, first, last, email_for(first, last, "retailco.com"), TITLES[dept][2],
                          hire.isoformat(), round(rng.uniform(140_000, 190_000), -2), dept_id, None))
        managers[dept_id] = emp_id
        emp_id += 1
    for _ in range(52):
        dept_id = rng.randint(1, len(DEPARTMENTS))
        dept = DEPARTMENTS[dept_id - 1][0]
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        hire = _rand_date(rng, date(2019, 1, 1), date(2025, 9, 30))
        employees.append((emp_id, first, last, email_for(first, last, "retailco.com"),
                          rng.choice(TITLES[dept][:2]), hire.isoformat(),
                          round(rng.uniform(55_000, 135_000), -2), dept_id, managers[dept_id]))
        emp_id += 1
    conn.executemany("INSERT INTO Employees VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", employees)
    sales_reps = [e[0] for e in employees if e[7] == 1]

    customers = []
    for cid in range(1, 201):
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        state = rng.choice(list(STATES_CITIES))
        customers.append((cid, first, last, email_for(first, last, "example.com"),
                          rng.choice(STATES_CITIES[state]), state, "USA",
                          _rand_date(rng, date(2021, 1, 1), date(2025, 9, 30)).isoformat()))
    conn.executemany("INSERT INTO Customers VALUES (?, ?, ?, ?, ?, ?, ?, ?)", customers)

    products, pid = [], 1
    for cat_id, (cat, items) in enumerate(CATEGORIES.items(), start=1):
        conn.execute("INSERT INTO Categories VALUES (?, ?)", (cat_id, cat))
        for name, price in items:
            products.append((pid, name, cat_id, price, rng.randint(0, 250), 1 if rng.random() < 0.08 else 0))
            pid += 1
    conn.executemany("INSERT INTO Products VALUES (?, ?, ?, ?, ?, ?)", products)

    orders, items, item_id = [], [], 1
    for oid in range(1, 801):
        cust = rng.choice(customers)
        signup = date.fromisoformat(cust[7])
        order_date = _rand_date(rng, max(signup, date(2023, 1, 1)), date(2025, 10, 31))
        status = rng.choices(["Delivered", "Shipped", "Pending", "Cancelled"], weights=[60, 15, 15, 10])[0]
        shipped = (order_date + timedelta(days=rng.randint(1, 6))).isoformat() if status in ("Delivered", "Shipped") else None
        total = 0.0
        for product in rng.sample(products, rng.randint(1, 4)):
            qty = rng.randint(1, 5)
            discount = rng.choice([0, 0, 0, 0.05, 0.1, 0.15, 0.2])
            items.append((item_id, oid, product[0], qty, product[3], discount))
            total += qty * product[3] * (1 - discount)
            item_id += 1
        orders.append((oid, cust[0], rng.choice(sales_reps), order_date.isoformat(), shipped, status, round(total, 2)))
    conn.executemany("INSERT INTO Orders VALUES (?, ?, ?, ?, ?, ?, ?)", orders)
    conn.executemany("INSERT INTO OrderItems VALUES (?, ?, ?, ?, ?, ?)", items)

    conn.commit()
    conn.execute("ANALYZE")  # gives the query planner statistics for cost estimation
    conn.commit()
    conn.close()
    return db_path


if __name__ == "__main__":
    path = build_database()
    print(f"Sample database written to {path}")
