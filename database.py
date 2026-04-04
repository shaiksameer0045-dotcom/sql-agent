import sqlite3

DB_PATH = "sample.db"


def init_db():
    """Create sample database with demo data."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS departments (
        id      INTEGER PRIMARY KEY,
        name    TEXT    NOT NULL,
        budget  REAL
    );

    CREATE TABLE IF NOT EXISTS employees (
        id            INTEGER PRIMARY KEY,
        name          TEXT    NOT NULL,
        department_id INTEGER REFERENCES departments(id),
        salary        REAL,
        hire_date     TEXT,
        role          TEXT
    );

    CREATE TABLE IF NOT EXISTS sales (
        id          INTEGER PRIMARY KEY,
        employee_id INTEGER REFERENCES employees(id),
        amount      REAL,
        sale_date   TEXT,
        product     TEXT
    );

    INSERT OR IGNORE INTO departments VALUES
        (1, 'Engineering', 500000),
        (2, 'Sales',       300000),
        (3, 'Marketing',   200000),
        (4, 'HR',          150000);

    INSERT OR IGNORE INTO employees VALUES
        (1, 'Alice Johnson', 1, 95000, '2020-03-15', 'Senior Engineer'),
        (2, 'Bob Smith',     1, 85000, '2021-06-01', 'Engineer'),
        (3, 'Carol White',   2, 75000, '2019-11-20', 'Sales Manager'),
        (4, 'Dave Brown',    2, 65000, '2022-01-10', 'Sales Rep'),
        (5, 'Eve Davis',     3, 70000, '2020-08-05', 'Marketing Lead'),
        (6, 'Frank Miller',  4, 60000, '2021-03-22', 'HR Manager'),
        (7, 'Grace Lee',     1, 90000, '2020-01-01', 'Tech Lead'),
        (8, 'Henry Wilson',  2, 68000, '2022-07-15', 'Sales Rep');

    INSERT OR IGNORE INTO sales VALUES
        (1, 3, 15000, '2024-01-15', 'Enterprise License'),
        (2, 4,  8000, '2024-01-20', 'Pro License'),
        (3, 3, 22000, '2024-02-01', 'Enterprise License'),
        (4, 8,  9500, '2024-02-10', 'Pro License'),
        (5, 4, 12000, '2024-02-15', 'Enterprise License'),
        (6, 3, 18000, '2024-03-01', 'Enterprise License'),
        (7, 8,  7500, '2024-03-05', 'Pro License'),
        (8, 4, 11000, '2024-03-10', 'Enterprise License');
    """)

    conn.commit()
    conn.close()


def get_schema() -> str:
    """Return the database schema as a readable string."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cur.fetchall()]

    parts = []
    for table in tables:
        cur.execute(f"PRAGMA table_info({table})")
        cols = cur.fetchall()
        col_defs = ", ".join(f"{c[1]} {c[2]}" for c in cols)
        parts.append(f"  {table}({col_defs})")

    conn.close()
    return "Tables:\n" + "\n".join(parts)


def execute_query(sql: str) -> tuple[list[str], list[tuple]]:
    """Execute a SQL statement. Returns (column_names, rows). Raises on error."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute(sql)
        if cur.description:
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
        else:
            conn.commit()
            columns = ["affected_rows"]
            rows = [(cur.rowcount,)]
        return columns, rows
    finally:
        conn.close()
