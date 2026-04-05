"""
End-to-end database test suite.
Tests all supported DB types: SQLite, DuckDB, PostgreSQL, MySQL.
Runs against the dev-mode local server (no auth required).
"""

import json
import os
import sys
import tempfile
import urllib.request
import urllib.error

BASE = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8766")
PASS = []
FAIL = []

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read())

def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

def delete(path):
    req = urllib.request.Request(BASE + path, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

def ok(name, result=None):
    PASS.append(name)
    print(f"  ✓ {name}" + (f": {result}" if result else ""))

def fail(name, reason):
    FAIL.append(name)
    print(f"  ✗ {name}: {reason}")

# ── Test helpers ──────────────────────────────────────────────────────────────

def add_conn(cfg):
    r = post("/api/connections", cfg)
    if "id" not in r:
        raise Exception(f"Could not create connection: {r}")
    return r["id"]

def rm_conn(cid):
    delete(f"/api/connections/{cid}")

def test_schema(cid, label):
    schema = get(f"/api/connections/{cid}/schema")
    if not isinstance(schema, list) or len(schema) == 0:
        fail(f"{label}: schema", f"empty schema: {schema}")
        return False
    tables = [t["table"] for t in schema]
    ok(f"{label}: schema ({len(schema)} tables: {', '.join(tables[:4])})")
    return True

def test_query(cid, sql, label, expected_col=None):
    r = post(f"/api/connections/{cid}/execute", {"sql": sql})
    if "error" in r:
        fail(f"{label}: query '{sql[:60]}'", r["error"])
        return None
    cols, rows = r["columns"], r["rows"]
    snippet = f"{len(rows)} row(s), cols={cols[:3]}"
    if expected_col and expected_col not in cols:
        fail(f"{label}: query", f"expected col '{expected_col}' in {cols}")
        return None
    ok(f"{label}: query → {snippet}")
    return rows

def test_preview(cid, table, label):
    r = get(f"/api/connections/{cid}/table/{table}/preview?limit=5")
    if "error" in r:
        fail(f"{label}: preview {table}", r["error"])
        return
    ok(f"{label}: preview '{table}' → {len(r['rows'])} rows")

# ── SQLite ────────────────────────────────────────────────────────────────────

def test_sqlite():
    print("\n── SQLite ─────────────────────────────────────────────────────")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    cid = add_conn({"name": "Test SQLite", "type": "sqlite", "file": db_path})
    ok("SQLite: connection created")

    # Create tables
    test_query(cid, """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            price REAL,
            stock INTEGER
        )
    """, "SQLite")
    test_query(cid, "INSERT INTO products VALUES (1,'Widget',9.99,100)", "SQLite")
    test_query(cid, "INSERT INTO products VALUES (2,'Gadget',24.99,50)", "SQLite")
    test_query(cid, "INSERT INTO products VALUES (3,'Doohickey',4.99,200)", "SQLite")

    test_schema(cid, "SQLite")
    test_query(cid, "SELECT * FROM products", "SQLite", "name")
    test_query(cid, "SELECT name, price FROM products WHERE stock > 60 ORDER BY price DESC", "SQLite", "price")
    test_query(cid, "SELECT COUNT(*) as cnt FROM products", "SQLite", "cnt")
    test_query(cid, "SELECT AVG(price) as avg_price FROM products", "SQLite", "avg_price")
    test_preview(cid, "products", "SQLite")

    # Update + select
    test_query(cid, "UPDATE products SET stock = 999 WHERE id = 1", "SQLite")
    rows = test_query(cid, "SELECT stock FROM products WHERE id=1", "SQLite")
    if rows and rows[0][0] == 999:
        ok("SQLite: UPDATE persisted correctly")
    else:
        fail("SQLite: UPDATE", f"expected stock=999, got {rows}")

    rm_conn(cid)
    ok("SQLite: connection deleted")

# ── DuckDB ────────────────────────────────────────────────────────────────────

def test_duckdb():
    print("\n── DuckDB ─────────────────────────────────────────────────────")
    import uuid, os
    db_path = os.path.join(tempfile.gettempdir(), f"test_{uuid.uuid4().hex}.duckdb")
    # Do NOT pre-create the file — DuckDB creates it itself

    cid = add_conn({"name": "Test DuckDB", "type": "duckdb", "file": db_path})
    ok("DuckDB: connection created")

    test_query(cid, """
        CREATE TABLE sales (
            sale_id INTEGER PRIMARY KEY,
            region VARCHAR,
            amount DOUBLE,
            sale_date DATE
        )
    """, "DuckDB")
    test_query(cid, """
        INSERT INTO sales VALUES
        (1,'North',1500.00,'2024-01-10'),
        (2,'South',2200.50,'2024-01-15'),
        (3,'North',800.75,'2024-02-01'),
        (4,'East',3100.00,'2024-02-20'),
        (5,'South',950.25,'2024-03-05')
    """, "DuckDB")

    test_schema(cid, "DuckDB")
    test_query(cid, 'SELECT * FROM "sales"', "DuckDB", "region")
    test_query(cid, 'SELECT region, SUM(amount) as total FROM "sales" GROUP BY region ORDER BY total DESC', "DuckDB", "total")

    # DuckDB-specific: window functions
    test_query(cid, """
        SELECT region, amount,
               RANK() OVER (PARTITION BY region ORDER BY amount DESC) as rnk
        FROM "sales"
    """, "DuckDB", "rnk")

    # DuckDB-specific: date functions
    test_query(cid, """
        SELECT DATE_TRUNC('month', sale_date) as month, SUM(amount) as total
        FROM "sales" GROUP BY 1 ORDER BY 1
    """, "DuckDB", "month")

    test_preview(cid, "sales", "DuckDB")
    rm_conn(cid)
    ok("DuckDB: connection deleted")

# ── PostgreSQL ────────────────────────────────────────────────────────────────

def test_postgresql():
    print("\n── PostgreSQL ─────────────────────────────────────────────────")
    pg_host = os.environ.get("PG_HOST", "localhost")
    pg_port = int(os.environ.get("PG_PORT", 5432))
    pg_db   = os.environ.get("PG_DB", "postgres")
    pg_user = os.environ.get("PG_USER", "postgres")
    pg_pass = os.environ.get("PG_PASS", "")

    try:
        import psycopg2
        conn = psycopg2.connect(host=pg_host, port=pg_port, dbname=pg_db,
                                user=pg_user, password=pg_pass, connect_timeout=5)
        conn.close()
    except Exception as e:
        print(f"  ⚠  PostgreSQL not available ({e}) — skipping")
        return

    cid = add_conn({
        "name": "Test PostgreSQL", "type": "postgresql",
        "host": pg_host, "port": pg_port,
        "database": pg_db, "user": pg_user, "password": pg_pass
    })
    ok("PostgreSQL: connection created")

    # Setup
    test_query(cid, 'DROP TABLE IF EXISTS "pg_test_orders"', "PostgreSQL")
    test_query(cid, """
        CREATE TABLE "pg_test_orders" (
            order_id SERIAL PRIMARY KEY,
            customer TEXT NOT NULL,
            total NUMERIC(10,2),
            status TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """, "PostgreSQL")
    test_query(cid, """
        INSERT INTO "pg_test_orders" (customer, total, status) VALUES
        ('Alice', 299.99, 'delivered'),
        ('Bob', 49.50, 'pending'),
        ('Carol', 899.00, 'delivered'),
        ('Dave', 124.75, 'shipped')
    """, "PostgreSQL")

    test_schema(cid, "PostgreSQL")
    test_query(cid, 'SELECT * FROM "pg_test_orders"', "PostgreSQL", "customer")
    test_query(cid, 'SELECT customer, total FROM "pg_test_orders" WHERE status=\'delivered\' ORDER BY total DESC', "PostgreSQL", "total")
    test_query(cid, 'SELECT COUNT(*) as cnt, SUM(total) as revenue FROM "pg_test_orders"', "PostgreSQL", "revenue")

    # PostgreSQL-specific: ILIKE, ::numeric cast, NOW()
    test_query(cid, 'SELECT * FROM "pg_test_orders" WHERE customer ILIKE \'a%\'', "PostgreSQL", "customer")
    test_query(cid, 'SELECT total::numeric * 1.1 AS with_tax FROM "pg_test_orders" LIMIT 2', "PostgreSQL", "with_tax")

    test_preview(cid, "pg_test_orders", "PostgreSQL")

    # Cleanup
    test_query(cid, 'DROP TABLE "pg_test_orders"', "PostgreSQL")
    rm_conn(cid)
    ok("PostgreSQL: connection deleted")

# ── MySQL ─────────────────────────────────────────────────────────────────────

def test_mysql():
    print("\n── MySQL ──────────────────────────────────────────────────────")
    my_host = os.environ.get("MY_HOST", "localhost")
    my_port = int(os.environ.get("MY_PORT", 3306))
    my_db   = os.environ.get("MY_DB", "mysql")
    my_user = os.environ.get("MY_USER", "root")
    my_pass = os.environ.get("MY_PASS", "")

    try:
        import pymysql
        conn = pymysql.connect(host=my_host, port=my_port, database=my_db,
                               user=my_user, password=my_pass, connect_timeout=5)
        conn.close()
    except Exception as e:
        print(f"  ⚠  MySQL not available ({e}) — skipping")
        return

    cid = add_conn({
        "name": "Test MySQL", "type": "mysql",
        "host": my_host, "port": my_port,
        "database": my_db, "user": my_user, "password": my_pass
    })
    ok("MySQL: connection created")

    test_query(cid, "DROP TABLE IF EXISTS `mysql_test_items`", "MySQL")
    test_query(cid, """
        CREATE TABLE `mysql_test_items` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `name` VARCHAR(100) NOT NULL,
            `category` VARCHAR(50),
            `price` DECIMAL(10,2),
            `created_at` DATETIME DEFAULT NOW()
        )
    """, "MySQL")
    test_query(cid, """
        INSERT INTO `mysql_test_items` (`name`, `category`, `price`) VALUES
        ('Laptop','Electronics',999.99),
        ('Mouse','Electronics',29.99),
        ('Desk','Furniture',349.00),
        ('Chair','Furniture',199.50),
        ('Monitor','Electronics',449.00)
    """, "MySQL")

    test_schema(cid, "MySQL")
    test_query(cid, "SELECT * FROM `mysql_test_items`", "MySQL", "name")
    test_query(cid, "SELECT `category`, COUNT(*) as cnt, SUM(`price`) as total FROM `mysql_test_items` GROUP BY `category`", "MySQL", "total")
    test_query(cid, "SELECT `name`, `price` FROM `mysql_test_items` WHERE `category`='Electronics' ORDER BY `price` DESC", "MySQL", "price")

    # MySQL-specific: CONCAT, IFNULL, DATE_FORMAT
    test_query(cid, "SELECT CONCAT(`name`, ' - ', `category`) as label FROM `mysql_test_items` LIMIT 3", "MySQL", "label")
    test_query(cid, "SELECT IFNULL(`category`, 'Unknown') as cat FROM `mysql_test_items` LIMIT 2", "MySQL", "cat")

    test_preview(cid, "mysql_test_items", "MySQL")

    # Cleanup
    test_query(cid, "DROP TABLE `mysql_test_items`", "MySQL")
    rm_conn(cid)
    ok("MySQL: connection deleted")

# ── Demo connections (auto-seeded) ────────────────────────────────────────────

def test_demo_connections():
    print("\n── Demo connections (auto-seeded) ─────────────────────────────")
    conns = get("/api/connections")
    if not conns:
        fail("Demo connections", "No connections returned")
        return

    demo_sqlite = next((c for c in conns if c["type"] == "sqlite" and "Demo" in c["name"]), None)
    demo_duck   = next((c for c in conns if c["type"] == "duckdb"  and "Demo" in c["name"]), None)

    if demo_sqlite:
        cid = demo_sqlite["id"]
        ok(f"Demo SQLite found: {demo_sqlite['name']}")
        test_schema(cid, "Demo SQLite")
        test_query(cid, "SELECT e.name, d.name as dept FROM employees e JOIN departments d ON e.department_id = d.id LIMIT 5", "Demo SQLite", "dept")
        test_query(cid, "SELECT d.name, SUM(s.amount) as total_sales FROM sales s JOIN employees e ON s.employee_id=e.id JOIN departments d ON e.department_id=d.id GROUP BY d.name ORDER BY total_sales DESC", "Demo SQLite", "total_sales")
    else:
        fail("Demo SQLite", "not found in connections list")

    if demo_duck:
        cid = demo_duck["id"]
        ok(f"Demo DuckDB found: {demo_duck['name']}")
        test_schema(cid, "Demo DuckDB")
        test_query(cid, 'SELECT p.name, SUM(o.quantity * o.unit_price) as revenue FROM orders o JOIN products p ON o.product_id=p.product_id GROUP BY p.name ORDER BY revenue DESC LIMIT 5', "Demo DuckDB", "revenue")
        test_query(cid, 'SELECT c.country, COUNT(*) as orders FROM orders o JOIN customers c ON o.customer_id=c.customer_id GROUP BY c.country ORDER BY orders DESC', "Demo DuckDB", "orders")
    else:
        fail("Demo DuckDB", "not found in connections list")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{'='*60}")
    print("SQL Agent — Full Database Test Suite")
    print(f"Target: {BASE}")
    print(f"{'='*60}")

    # Verify server reachable
    try:
        h = get("/health")
        print(f"\nServer: {h['status']} | Firebase: {h['firebase_enabled']} | SSH: {h['ssh_available']}")
    except Exception as e:
        print(f"ERROR: Cannot reach server at {BASE}: {e}")
        sys.exit(1)

    test_demo_connections()
    test_sqlite()
    test_duckdb()
    test_postgresql()
    test_mysql()

    # Summary
    total = len(PASS) + len(FAIL)
    print(f"\n{'='*60}")
    print(f"Results: {len(PASS)}/{total} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFailed tests:")
        for f in FAIL:
            print(f"  ✗ {f}")
    print(f"{'='*60}\n")
    sys.exit(0 if not FAIL else 1)
