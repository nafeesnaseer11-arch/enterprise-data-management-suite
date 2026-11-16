"""Generate a realistic (and deliberately messy) sample AWS estate for local runs.

S3 buckets (folders):  acme-sales-prod/{customers,orders,products}
                       acme-support-prod/support_tickets
                       acme-finance-prod/invoices
RDS database (SQLite): acme-hr-db -> employees, departments

Messiness injected to exercise the Intelligent ETL: inconsistent headers, duplicates,
"N/A"/blank nulls, currency-formatted numbers, padded strings, mixed-case emails,
missing primary keys and mixed boolean spellings.
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

FIRST = ["Olivia", "Liam", "Emma", "Noah", "Ava", "Elijah", "Sophia", "James", "Isabella", "Lucas", "Mia", "Mason",
         "Amelia", "Ethan", "Harper", "Aiden", "Fatima", "Omar", "Priya", "Arjun", "Mei", "Wei", "Yuki", "Kenji",
         "Sofia", "Mateo", "Chloe", "Leo", "Zara", "Ali", "Nadia", "Hassan", "Elena", "Ivan", "Grace", "Daniel"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Garcia", "Miller", "Davis", "Khan", "Patel", "Chen", "Wang",
        "Tanaka", "Sato", "Rossi", "Silva", "Martin", "Ali", "Hussain", "Novak", "Kim", "Lopez", "Clark", "Lewis"]
COUNTRIES = {"United States": "NA", "Canada": "NA", "United Kingdom": "EMEA", "Germany": "EMEA", "France": "EMEA",
             "United Arab Emirates": "EMEA", "India": "APAC", "Japan": "APAC", "Singapore": "APAC",
             "Australia": "APAC", "Brazil": "LATAM", "Mexico": "LATAM"}
SEGMENTS = ["Enterprise", "Mid-Market", "SMB"]
CATEGORIES = {"Cloud Storage": (40, 400), "Analytics": (200, 2500), "Security": (150, 1800),
              "Networking": (80, 900), "AI Services": (300, 4000), "Support Plans": (100, 1200)}
PRODUCT_WORDS = ["Pro", "Plus", "Enterprise", "Lite", "Edge", "Core", "Max", "Flex"]
TICKET_TOPICS = {
    "Billing": ["Invoice amount does not match the quote", "Charged twice for the monthly subscription",
                "Need a copy of last quarter's invoices for audit", "Refund request for unused licences"],
    "Technical": ["API returns 503 errors during peak hours", "Data export job fails with timeout",
                  "Dashboard is slow to load for large accounts", "SSO login loop after password reset"],
    "Onboarding": ["Need help configuring the first data pipeline", "Requesting training for new analysts",
                   "Unable to invite team members to the workspace", "Question about migrating historical data"],
    "Account": ["Request to upgrade plan to Enterprise", "Change the billing contact on the account",
                "Close a duplicate account created by mistake", "Update company address for tax purposes"],
}
DEPARTMENTS = [(10, "Engineering", "CC-100"), (20, "Sales", "CC-200"), (30, "Customer Success", "CC-300"),
               (40, "Finance", "CC-400"), (50, "Human Resources", "CC-500"), (60, "Marketing", "CC-600"),
               (70, "Data & Analytics", "CC-700"), (80, "Legal", "CC-800")]
TITLES = {10: ["Software Engineer", "Senior Engineer", "Staff Engineer"], 20: ["Account Executive", "Sales Manager"],
          30: ["Support Specialist", "CS Manager"], 40: ["Accountant", "Financial Analyst"],
          50: ["HR Generalist", "Recruiter"], 60: ["Marketing Manager", "Content Strategist"],
          70: ["Data Engineer", "Data Scientist", "Analytics Lead"], 80: ["Counsel", "Paralegal"]}
SALARY = {10: (95, 210), 20: (70, 180), 30: (55, 110), 40: (65, 140), 50: (55, 120), 60: (60, 150),
          70: (90, 200), 80: (90, 230)}


def _d(start: date, end: date, rnd: random.Random) -> date:
    return start + timedelta(days=rnd.randint(0, (end - start).days))


def generate(s3_root: Path, rds_root: Path, seed: int = 42) -> None:
    rnd = random.Random(seed)
    s3_root, rds_root = Path(s3_root), Path(rds_root)
    today = date(2026, 11, 1)

    # ---------------- customers (CSV, messy headers & values)
    customers = []
    for i in range(1, 601):
        fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
        country = rnd.choice(list(COUNTRIES))
        email = f"{fn}.{ln}{i}@{rnd.choice(['example.com', 'mail.example.org', 'corp.example.net'])}"
        customers.append({
            "Customer ID": str(1000 + i), "First Name": fn, "Last Name": f" {ln} " if rnd.random() < 0.05 else ln,
            "Email Address": email.upper() if rnd.random() < 0.1 else email.lower(),
            "Phone": f"+1 ({rnd.randint(200, 999)}) {rnd.randint(200, 999)}-{rnd.randint(1000, 9999)}",
            "Country": country, "Segment": rnd.choices(SEGMENTS, [2, 3, 5])[0],
            "Signup Date": str(_d(date(2021, 1, 1), date(2026, 6, 30), rnd)),
            "Date of Birth": str(_d(date(1955, 1, 1), date(2002, 12, 31), rnd)),
        })
    for c in rnd.sample(customers, 25):  # null tokens
        c[rnd.choice(["Phone", "Segment", "Signup Date"])] = rnd.choice(["N/A", "", "null"])
    dupes = [dict(c) for c in rnd.sample(customers, 30)]
    bad_keys = [{**dict(rnd.choice(customers)), "Customer ID": rnd.choice(["", "N/A"])} for _ in range(6)]
    p = s3_root / "acme-sales-prod" / "customers"
    p.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(customers + dupes + bad_keys).sample(frac=1, random_state=seed).to_csv(p / "customers_export.csv", index=False)

    # ---------------- products (JSON lines)
    products = []
    for i, (cat, (lo, hi)) in enumerate([(c, r) for c in CATEGORIES for r in [CATEGORIES[c]] * 7], start=1):
        products.append({"product_id": f"P-{i:03d}", "product_name": f"{cat.split()[0]} {rnd.choice(PRODUCT_WORDS)} {i}",
                         "category": cat, "list_price": round(rnd.uniform(lo, hi), 2),
                         "active": rnd.choice([True, True, "yes", "Y", False])})
    p = s3_root / "acme-sales-prod" / "products"
    p.mkdir(parents=True, exist_ok=True)
    (p / "catalog.jsonl").write_text("\n".join(json.dumps(x) for x in products))

    # ---------------- orders (two CSV files, currency formatting, cancelled orders)
    orders = []
    for i in range(1, 6001):
        c = rnd.choice(customers)
        prod = rnd.choice(products)
        price = prod["list_price"] * rnd.uniform(0.85, 1.0)
        od = _d(date(2025, 1, 1), date(2026, 10, 31), rnd)
        orders.append({"order_id": 500000 + i, "customer_id": c["Customer ID"], "product_id": prod["product_id"],
                       "quantity": rnd.choices([1, 2, 3, 5, 10, 20], [30, 25, 15, 12, 10, 8])[0],
                       "unit_price": f"${price:,.2f}" if rnd.random() < 0.15 else f"{price:.2f}",
                       "order_date": str(od), "status": rnd.choices(
                           ["completed", "shipped", "processing", "cancelled", "Completed"], [60, 15, 10, 8, 7])[0],
                       "region": COUNTRIES[c["Country"]]})
    orders += [dict(o) for o in rnd.sample(orders, 120)]  # replayed duplicates
    p = s3_root / "acme-sales-prod" / "orders"
    p.mkdir(parents=True, exist_ok=True)
    odf = pd.DataFrame(orders)
    odf[odf["order_date"] < "2026-01-01"].to_csv(p / "orders_2025.csv", index=False)
    odf[odf["order_date"] >= "2026-01-01"].to_csv(p / "orders_2026.csv", index=False)

    # ---------------- support tickets (JSON lines, free text)
    tickets = []
    for i in range(1, 1501):
        cat = rnd.choice(list(TICKET_TOPICS))
        subject = rnd.choice(TICKET_TOPICS[cat])
        created = datetime.combine(_d(date(2025, 6, 1), date(2026, 10, 31), rnd), datetime.min.time()) + timedelta(
            hours=rnd.randint(7, 19), minutes=rnd.randint(0, 59))
        status = rnd.choices(["resolved", "closed", "open", "in_progress"], [50, 25, 15, 10])[0]
        prio = rnd.choices(["low", "medium", "high", "urgent"], [30, 40, 22, 8])[0]
        resolved = created + timedelta(hours=rnd.uniform(1, {"urgent": 12, "high": 36, "medium": 72, "low": 140}[prio])) \
            if status in ("resolved", "closed") else None
        cust = rnd.choice(customers)
        tickets.append({"ticket_id": f"T-{i:05d}", "customer_id": int(cust["Customer ID"]),
                        "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
                        "resolved_at": resolved.strftime("%Y-%m-%d %H:%M:%S") if resolved else None,
                        "priority": prio, "category": cat, "subject": subject,
                        "description": f"{subject}. Customer from {cust['Country']} ({cust['Segment']} segment) reported "
                                       f"this issue; it is impacting their {rnd.choice(['finance team', 'analysts', 'engineers', 'operations'])} "
                                       f"and they asked for an update {rnd.choice(['today', 'this week', 'before month end'])}.",
                        "status": status, "agent_email": f"agent{rnd.randint(1, 25)}@acme.example"})
    p = s3_root / "acme-support-prod" / "support_tickets"
    p.mkdir(parents=True, exist_ok=True)
    (p / "tickets.jsonl").write_text("\n".join(json.dumps(t) for t in tickets))

    # ---------------- invoices (Parquet, card numbers = PII)
    inv = []
    for i, o in enumerate(rnd.sample(orders[:6000], 4500), start=1):
        amt = o["quantity"] * float(str(o["unit_price"]).replace("$", "").replace(",", ""))
        method = rnd.choice(["card", "card", "wire", "ach"])
        inv.append({"invoice_id": f"INV-{i:06d}", "order_id": o["order_id"], "amount": round(amt, 2),
                    "currency": "USD", "issued_date": o["order_date"], "paid": rnd.random() < 0.85,
                    "payment_method": method,
                    "card_number": " ".join(f"{rnd.randint(0, 9999):04d}" for _ in range(4)) if method == "card" else None})
    p = s3_root / "acme-finance-prod" / "invoices"
    p.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(inv).to_parquet(p / "invoices.parquet", index=False)

    # ---------------- HR database (RDS -> SQLite)
    rds_root.mkdir(parents=True, exist_ok=True)
    db = rds_root / "acme_hr.db"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE departments (department_id INTEGER PRIMARY KEY, department_name TEXT, cost_center TEXT)")
    con.executemany("INSERT INTO departments VALUES (?,?,?)", DEPARTMENTS)
    con.execute("CREATE TABLE employees (employee_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, work_email TEXT, "
                "ssn TEXT, department_id INTEGER, job_title TEXT, hire_date TEXT, salary REAL, manager_id INTEGER, location TEXT)")
    emps = []
    for i in range(1, 241):
        dep = rnd.choices([d[0] for d in DEPARTMENTS], [30, 18, 14, 8, 5, 9, 12, 4])[0]
        fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
        lo, hi = SALARY[dep]
        emps.append((i, fn, ln, f"{fn.lower()}.{ln.lower()}{i}@acme.example",
                     f"{rnd.randint(100, 899):03d}-{rnd.randint(10, 99):02d}-{rnd.randint(1000, 9999):04d}", dep,
                     rnd.choice(TITLES[dep]), str(_d(date(2014, 1, 1), today, rnd)),
                     round(rnd.uniform(lo, hi) * 1000, -2), rnd.randint(1, 20) if i > 20 else None,
                     rnd.choice(["Seattle", "London", "Dubai", "Bangalore", "Singapore", "Remote"])))
    con.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?,?,?)", emps)
    con.commit()
    con.close()


if __name__ == "__main__":
    from edms.config import ROOT
    generate(ROOT / "data" / "aws_s3", ROOT / "data" / "aws_rds")
    print("Sample AWS estate generated under data/aws_s3 and data/aws_rds")
