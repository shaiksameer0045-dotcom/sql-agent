"""
DBeaver-style SQL Agent — connect to any DB, query with natural language.
Supports: SQLite, PostgreSQL, MySQL/MariaDB
"""

import csv
import io
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

# ── Load .env ────────────────────────────────────────────────────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from groq import AsyncGroq
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ── Config ───────────────────────────────────────────────────────────────────
DATA_DIR   = Path(os.environ.get("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
MODEL      = "llama-3.3-70b-versatile"
MAX_TOKENS = 2048
MAX_RETRIES = 3

# ── Firebase Admin init ───────────────────────────────────────────────────────
import firebase_admin
from firebase_admin import auth as fb_auth, credentials as fb_creds

_FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
_FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
_FIREBASE_SERVICE_ACCOUNT_PATH = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", "")


def _firebase_enabled() -> bool:
    return bool(
        _FIREBASE_PROJECT_ID
        or _FIREBASE_SERVICE_ACCOUNT_JSON
        or _FIREBASE_SERVICE_ACCOUNT_PATH
    )


def _init_firebase():
    if firebase_admin._apps:
        return
    if _FIREBASE_SERVICE_ACCOUNT_JSON:
        cred = fb_creds.Certificate(json.loads(_FIREBASE_SERVICE_ACCOUNT_JSON))
        firebase_admin.initialize_app(cred)
    elif _FIREBASE_SERVICE_ACCOUNT_PATH:
        cred = fb_creds.Certificate(_FIREBASE_SERVICE_ACCOUNT_PATH)
        firebase_admin.initialize_app(cred)
    elif _FIREBASE_PROJECT_ID:
        # Minimal init — only needs project ID to verify tokens
        firebase_admin.initialize_app(options={"projectId": _FIREBASE_PROJECT_ID})
    # else: auth disabled (local dev without Firebase)

_init_firebase()


async def _get_uid(request: Request) -> str:
    """Extract and verify Firebase ID token → return uid. Raises 401 on failure."""
    if not _firebase_enabled():
        # Auth disabled (local dev) — use a fixed dev uid
        return "dev-user"
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = auth_header[7:]
    try:
        decoded = fb_auth.verify_id_token(token)
        return decoded["uid"]
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


# ── Per-user connection registry ──────────────────────────────────────────────
# _connections[uid][conn_id] = config dict
# _live[uid][conn_id]        = live DB connection object
_connections: dict[str, dict[str, dict]] = {}
_live:        dict[str, dict[str, Any]]  = {}


def _user_conns(uid: str) -> dict[str, dict]:
    if uid not in _connections:
        _connections[uid] = {}
        _load_connections(uid)
    return _connections[uid]


def _user_live(uid: str) -> dict[str, Any]:
    if uid not in _live:
        _live[uid] = {}
    return _live[uid]


def _connections_file(uid: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", uid)
    return DATA_DIR / f"connections_{safe}.json"


def _save_connections(uid: str):
    conns = _connections.get(uid, {})
    safe  = {cid: {k: v for k, v in cfg.items() if k != "password"}
             for cid, cfg in conns.items()}
    _connections_file(uid).write_text(json.dumps(safe, indent=2))


def _load_connections(uid: str):
    f = _connections_file(uid)
    if f.exists():
        data = json.loads(f.read_text())
        _connections.setdefault(uid, {}).update(data)


# ── Driver helpers ────────────────────────────────────────────────────────────

def _open_connection(cfg: dict):
    """Open and return a DB connection for the given config."""
    t = cfg["type"]
    if t == "sqlite":
        path = cfg.get("file", ":memory:")
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    elif t == "duckdb":
        import duckdb
        path = cfg.get("file", ":memory:")
        return duckdb.connect(path)
    elif t == "postgresql":
        import psycopg2
        return psycopg2.connect(
            host=cfg.get("host", "localhost"),
            port=int(cfg.get("port", 5432)),
            dbname=cfg.get("database", ""),
            user=cfg.get("user", ""),
            password=cfg.get("password", ""),
            connect_timeout=10,
        )
    elif t == "mysql":
        import pymysql
        return pymysql.connect(
            host=cfg.get("host", "localhost"),
            port=int(cfg.get("port", 3306)),
            database=cfg.get("database", ""),
            user=cfg.get("user", ""),
            password=cfg.get("password", ""),
            connect_timeout=10,
            cursorclass=pymysql.cursors.DictCursor,
        )
    else:
        raise ValueError(f"Unknown DB type: {t}")


def _get_live(conn_id: str, uid: str = "dev-user"):
    """Return live connection, reopening if closed/lost."""
    cfg = _user_conns(uid).get(conn_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Connection not found")

    live = _user_live(uid).get(conn_id)
    t = cfg["type"]

    # Test if still alive
    try:
        if live is not None:
            cur = live.cursor()
            if t == "sqlite":
                cur.execute("SELECT 1")
            elif t == "duckdb":
                live.execute("SELECT 1")
            elif t == "postgresql":
                cur.execute("SELECT 1")
                live.rollback()
            elif t == "mysql":
                cur.execute("SELECT 1")
            return live
    except Exception:
        pass

    # Reopen
    live = _open_connection(cfg)
    _user_live(uid)[conn_id] = live
    return live


def _get_schema(conn_id: str, uid: str = "dev-user") -> list[dict]:
    """Return [{table, columns:[{name,type}], row_count}] for a connection."""
    cfg = _user_conns(uid)[conn_id]
    live = _get_live(conn_id, uid)
    t = cfg["type"]
    cur = live.cursor()
    result = []

    if t == "sqlite":
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = [r[0] for r in cur.fetchall()]
        for tbl in tables:
            cur.execute(f"PRAGMA table_info([{tbl}])")
            cols = [{"name": c[1], "type": c[2] or "TEXT"} for c in cur.fetchall()]
            cur.execute(f"SELECT COUNT(*) FROM [{tbl}]")
            row_count = cur.fetchone()[0]
            result.append({"table": tbl, "columns": cols, "row_count": row_count})

    elif t == "duckdb":
        rows = live.execute("SHOW TABLES").fetchall()
        tables = [r[0] for r in rows]
        for tbl in tables:
            info = live.execute(f"PRAGMA table_info('{tbl}')").fetchall()
            cols = [{"name": r[1], "type": r[2] or "VARCHAR"} for r in info]
            row_count = live.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
            result.append({"table": tbl, "columns": cols, "row_count": row_count})

    elif t == "postgresql":
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        tables = [r[0] for r in cur.fetchall()]
        for tbl in tables:
            cur.execute("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position
            """, (tbl,))
            cols = [{"name": r[0], "type": r[1].upper()} for r in cur.fetchall()]
            cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
            row_count = cur.fetchone()[0]
            result.append({"table": tbl, "columns": cols, "row_count": row_count})
        live.rollback()

    elif t == "mysql":
        db = cfg.get("database", "")
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name
        """, (db,))
        rows = cur.fetchall()
        # MySQL 8 returns uppercase keys from information_schema
        tables = [list(r.values())[0] for r in rows]
        for tbl in tables:
            cur.execute("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position
            """, (db, tbl))
            col_rows = cur.fetchall()
            # Normalise keys to lowercase
            cols = [{"name": list(r.values())[0], "type": list(r.values())[1].upper()} for r in col_rows]
            cur.execute(f"SELECT COUNT(*) as cnt FROM `{tbl}`")
            row_count = list(cur.fetchone().values())[0]
            result.append({"table": tbl, "columns": cols, "row_count": row_count})

    return result


def _coerce(v):
    """Convert DB-driver-specific types to JSON-safe Python primitives."""
    if v is None:
        return None
    # Decimal → float
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except ImportError:
        pass
    # date / datetime / timedelta → str
    import datetime
    if isinstance(v, (datetime.date, datetime.datetime, datetime.timedelta, datetime.time)):
        return str(v)
    # bytes → hex string
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


def _execute_sql(conn_id: str, sql: str, uid: str = "dev-user") -> tuple[list[str], list[list]]:
    """Execute SQL on a connection. Returns (columns, rows)."""
    live = _get_live(conn_id, uid)
    cfg  = _user_conns(uid)[conn_id]
    t    = cfg["type"]

    # DuckDB has its own API (not DB-API2 cursor model)
    if t == "duckdb":
        rel = live.execute(sql)
        if rel.description:
            columns = [d[0] for d in rel.description]
            rows = [[_coerce(v) for v in row] for row in rel.fetchall()]
        else:
            columns = ["affected_rows"]
            rows = [[-1]]
        return columns, rows

    cur = live.cursor()
    cur.execute(sql)

    if cur.description:
        if t == "sqlite":
            columns = [d[0] for d in cur.description]
            rows = [[_coerce(v) for v in row] for row in cur.fetchall()]
        elif t == "postgresql":
            columns = [d[0] for d in cur.description]
            rows = [[_coerce(v) for v in r] for r in cur.fetchall()]
            live.rollback()
        elif t == "mysql":
            columns = [d[0] for d in cur.description]
            rows = [[_coerce(v) for v in r.values()] for r in cur.fetchall()]
    else:
        live.commit()
        columns = ["affected_rows"]
        rows = [[cur.rowcount]]

    return columns, rows


def _rich_schema_for_llm(conn_id: str, uid: str = "dev-user") -> str:
    """
    Build a rich schema description for the LLM prompt.
    Includes: column types, primary keys, foreign key hints (from naming),
    row counts, and 3 sample rows per table.
    """
    cfg  = _user_conns(uid)[conn_id]
    live = _get_live(conn_id, uid)
    t    = cfg["type"]
    cur  = live.cursor()
    parts = []

    # ── Fetch table list + columns + PKs ────────────────────────────────────
    if t == "duckdb":
        tbl_names = [r[0] for r in live.execute("SHOW TABLES").fetchall()]
        tables = []
        for tbl in tbl_names:
            info = live.execute(f"PRAGMA table_info('{tbl}')").fetchall()
            # (cid, name, type, notnull, dflt, pk)
            cols = [{"name": r[1], "type": r[2] or "VARCHAR", "pk": bool(r[5])} for r in info]
            count = live.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
            samples = [[_coerce(v) for v in row]
                       for row in live.execute(f'SELECT * FROM "{tbl}" LIMIT 3').fetchall()]
            tables.append({"table": tbl, "columns": cols, "row_count": count, "samples": samples})

    elif t == "sqlite":
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        table_names = [r[0] for r in cur.fetchall()]
        tables = []
        for tbl in table_names:
            cur.execute(f"PRAGMA table_info([{tbl}])")
            rows = cur.fetchall()
            # rows: (cid, name, type, notnull, dflt_value, pk)
            cols = [{"name": r[1], "type": r[2] or "TEXT", "pk": bool(r[5])} for r in rows]
            cur.execute(f"SELECT COUNT(*) FROM [{tbl}]")
            count = cur.fetchone()[0]
            # Sample rows
            cur.execute(f"SELECT * FROM [{tbl}] LIMIT 3")
            samples = [list(r) for r in cur.fetchall()]
            tables.append({"table": tbl, "columns": cols, "row_count": count, "samples": samples})

    elif t == "postgresql":
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name
        """)
        table_names = [r[0] for r in cur.fetchall()]
        tables = []
        for tbl in table_names:
            # Columns
            cur.execute("""
                SELECT c.column_name, c.data_type,
                       CASE WHEN kcu.column_name IS NOT NULL THEN true ELSE false END as is_pk
                FROM information_schema.columns c
                LEFT JOIN (
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                      AND tc.table_schema = kcu.table_schema
                    WHERE tc.table_schema='public' AND tc.table_name=%s
                      AND tc.constraint_type='PRIMARY KEY'
                ) kcu ON kcu.column_name = c.column_name
                WHERE c.table_schema='public' AND c.table_name=%s
                ORDER BY c.ordinal_position
            """, (tbl, tbl))
            cols = [{"name": r[0], "type": r[1].upper(), "pk": bool(r[2])} for r in cur.fetchall()]
            cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
            count = cur.fetchone()[0]
            cur.execute(f'SELECT * FROM "{tbl}" LIMIT 3')
            samples = [list(_coerce(v) for v in r) for r in cur.fetchall()]
            live.rollback()
            tables.append({"table": tbl, "columns": cols, "row_count": count, "samples": samples})

    elif t == "mysql":
        db = cfg.get("database", "")
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name
        """, (db,))
        table_names = [list(r.values())[0] for r in cur.fetchall()]
        tables = []
        for tbl in table_names:
            cur.execute("""
                SELECT c.column_name, c.data_type,
                       CASE WHEN c.column_key='PRI' THEN 1 ELSE 0 END as is_pk
                FROM information_schema.columns c
                WHERE c.table_schema=%s AND c.table_name=%s
                ORDER BY c.ordinal_position
            """, (db, tbl))
            col_rows = cur.fetchall()
            cols = [{"name": list(r.values())[0], "type": list(r.values())[1].upper(),
                     "pk": bool(list(r.values())[2])} for r in col_rows]
            cur.execute(f"SELECT COUNT(*) as cnt FROM `{tbl}`")
            count = list(cur.fetchone().values())[0]
            cur.execute(f"SELECT * FROM `{tbl}` LIMIT 3")
            samples = [list(_coerce(v) for v in r.values()) for r in cur.fetchall()]
            tables.append({"table": tbl, "columns": cols, "row_count": count, "samples": samples})
    else:
        return "No schema available."

    if not tables:
        return "No tables found in this database."

    # ── Detect FK relationships from naming conventions ──────────────────────
    all_table_names = {tbl["table"] for tbl in tables}
    def fk_hint(col_name: str) -> str:
        if col_name.endswith("_id"):
            ref = col_name[:-3]
            if ref in all_table_names:
                return f" → {ref}.id"
            # plural check
            if ref + "s" in all_table_names:
                return f" → {ref}s.id"
        return ""

    # ── Format each table ────────────────────────────────────────────────────
    for tbl in tables:
        col_lines = []
        for c in tbl["columns"]:
            pk_tag = " [PRIMARY KEY]" if c["pk"] else ""
            fk_tag = fk_hint(c["name"])
            col_lines.append(f"    {c['name']}  {c['type']}{pk_tag}{fk_tag}")

        block = f"TABLE {tbl['table']} ({tbl['row_count']:,} rows)\n"
        block += "\n".join(col_lines)

        if tbl["samples"]:
            col_names = [c["name"] for c in tbl["columns"]]
            block += "\n  -- sample rows:"
            for sample in tbl["samples"]:
                vals = ", ".join(
                    "NULL" if v is None else repr(v) if isinstance(v, str) else str(v)
                    for v in sample
                )
                block += f"\n  -- ({vals})"
        parts.append(block)

    return "\n\n".join(parts)


def _dialect_rules(db_type: str) -> str:
    if db_type == "duckdb":
        return """\
DUCKDB-SPECIFIC RULES (DuckDB is an analytical database with PostgreSQL-like SQL):
- Quote identifiers with double quotes: "column_name", "table_name"
- Rich type system: INTEGER, BIGINT, DOUBLE, VARCHAR, BOOLEAN, DATE, TIMESTAMP, LIST, STRUCT
- Excellent for analytics: window functions, QUALIFY, PIVOT, UNPIVOT fully supported
- Date functions: NOW(), CURRENT_DATE, DATE_TRUNC('month', col), date_diff('day', d1, d2)
- String: LOWER(), UPPER(), TRIM(), SPLIT_PART(), REGEXP_MATCHES()
- DuckDB can directly query CSV/Parquet: SELECT * FROM read_csv_auto('file.csv')
- Array functions: list_aggregate(), array_agg(), unnest()
- Sampling: SELECT * FROM tbl USING SAMPLE 10%
- Use LIMIT for top-N queries
- String concat: col1 || ' ' || col2
- SIMILAR TO or ~ for regex matching
- Do NOT use backtick quotes"""

    elif db_type == "sqlite":
        return """\
SQLITE-SPECIFIC RULES:
- Use single quotes for strings, double quotes for identifiers
- Date functions: date('now'), strftime('%Y-%m', date_col)
- No SHOW TABLES, no DESCRIBE, no information_schema
- Use sqlite_master for metadata: SELECT name FROM sqlite_master WHERE type='table'
- LIMIT comes after ORDER BY
- No BOOLEAN type — use INTEGER (0/1)
- String concat: col1 || ' ' || col2"""

    elif db_type == "postgresql":
        return """\
POSTGRESQL-SPECIFIC RULES:
- Quote mixed-case identifiers with double quotes: "ColumnName"
- Cast types explicitly when needed: value::numeric, value::text
- String functions: LOWER(), UPPER(), TRIM(), SUBSTRING()
- Date: NOW(), CURRENT_DATE, AGE(), DATE_TRUNC('month', col)
- Case-insensitive search: ILIKE '%pattern%'
- String concat: col1 || ' ' || col2
- Use COALESCE(col, default) for nulls
- Do NOT use backtick quotes"""

    elif db_type == "mysql":
        bt = "`"
        return (
            "MYSQL-SPECIFIC RULES:\n"
            f"- Always quote identifiers with backticks: {bt}column_name{bt}, {bt}table_name{bt}\n"
            "- String functions: LOWER(), UPPER(), TRIM(), SUBSTRING()\n"
            "- Date: NOW(), CURDATE(), DATE_FORMAT(col, '%Y-%m'), YEAR(col), MONTH(col)\n"
            "- Case-insensitive by default on most collations\n"
            "- String concat: CONCAT(col1, ' ', col2)\n"
            "- Use IFNULL(col, default) for nulls\n"
            "- Do NOT use double-quote identifiers\n"
            f"- CORRECT example: SELECT {bt}name{bt}, {bt}price{bt} FROM {bt}products{bt} WHERE {bt}stock{bt} < 50\n"
            f"- WRONG example:   SELECT \\`name\\` -- never use backslash before backtick"
        )
    return ""


def _extract_sql(text: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"(SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|DROP|ALTER)\b.*",
                  text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(0).strip()
    return text.strip()


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="SQL Agent — DBeaver Style")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)


def _seed_user_demos(uid: str):
    """Seed demo SQLite + DuckDB connections for a user if they have none."""
    _load_connections(uid)
    conns = _user_conns(uid)
    has_sqlite = any(c["type"] == "sqlite" and "Demo" in c["name"] for c in conns.values())
    has_duck   = any(c["type"] == "duckdb" and "Demo" in c["name"] for c in conns.values())

    if not has_sqlite:
        cid = str(uuid.uuid4())
        cfg = {"id": cid, "name": "Demo SQLite", "type": "sqlite",
               "file": str(DATA_DIR / f"demo_{uid[:8]}.db"), "color": "#7c5cfc"}
        conns[cid] = cfg
        _seed_demo(cid, uid)

    if not has_duck:
        did = str(uuid.uuid4())
        dcfg = {"id": did, "name": "Demo DuckDB", "type": "duckdb",
                "file": str(DATA_DIR / f"demo_{uid[:8]}.duckdb"), "color": "#f9e2af"}
        conns[did] = dcfg
        _seed_demo_duckdb(did, uid)

    _save_connections(uid)


@app.on_event("startup")
async def startup():
    Path("static").mkdir(exist_ok=True)
    # Pre-seed the dev-user demo connections (used when auth is disabled)
    _seed_user_demos("dev-user")


def _seed_demo(conn_id: str, uid: str = "dev-user"):
    cfg  = _user_conns(uid)[conn_id]
    live = _open_connection(cfg)
    _user_live(uid)[conn_id] = live
    live.executescript("""
    CREATE TABLE IF NOT EXISTS departments (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, budget REAL
    );
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL,
        department_id INTEGER REFERENCES departments(id),
        salary REAL, hire_date TEXT, role TEXT
    );
    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY, employee_id INTEGER REFERENCES employees(id),
        amount REAL, sale_date TEXT, product TEXT
    );
    INSERT OR IGNORE INTO departments VALUES
        (1,'Engineering',500000),(2,'Sales',300000),(3,'Marketing',200000),(4,'HR',150000);
    INSERT OR IGNORE INTO employees VALUES
        (1,'Alice Johnson',1,95000,'2020-03-15','Senior Engineer'),
        (2,'Bob Smith',1,85000,'2021-06-01','Engineer'),
        (3,'Carol White',2,75000,'2019-11-20','Sales Manager'),
        (4,'Dave Brown',2,65000,'2022-01-10','Sales Rep'),
        (5,'Eve Davis',3,70000,'2020-08-05','Marketing Lead'),
        (6,'Frank Miller',4,60000,'2021-03-22','HR Manager'),
        (7,'Grace Lee',1,90000,'2020-01-01','Tech Lead'),
        (8,'Henry Wilson',2,68000,'2022-07-15','Sales Rep');
    INSERT OR IGNORE INTO sales VALUES
        (1,3,15000,'2024-01-15','Enterprise License'),
        (2,4,8000,'2024-01-20','Pro License'),
        (3,3,22000,'2024-02-01','Enterprise License'),
        (4,8,9500,'2024-02-10','Pro License'),
        (5,4,12000,'2024-02-15','Enterprise License'),
        (6,3,18000,'2024-03-01','Enterprise License'),
        (7,8,7500,'2024-03-05','Pro License'),
        (8,4,11000,'2024-03-10','Enterprise License');
    """)


def _seed_demo_duckdb(conn_id: str, uid: str = "dev-user"):
    import duckdb
    path = _user_conns(uid)[conn_id].get("file", ":memory:")
    live = duckdb.connect(path)
    _user_live(uid)[conn_id] = live
    # Analytics-focused demo: e-commerce dataset
    live.execute("""
    CREATE TABLE IF NOT EXISTS products (
        product_id INTEGER PRIMARY KEY,
        name VARCHAR, category VARCHAR,
        price DOUBLE, cost DOUBLE, launch_date DATE
    )""")
    live.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        customer_id INTEGER PRIMARY KEY,
        name VARCHAR, country VARCHAR,
        segment VARCHAR, joined_date DATE
    )""")
    live.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        order_id INTEGER PRIMARY KEY,
        customer_id INTEGER, product_id INTEGER,
        quantity INTEGER, unit_price DOUBLE,
        order_date DATE, status VARCHAR
    )""")
    # Check if already seeded
    if live.execute("SELECT COUNT(*) FROM products").fetchone()[0] > 0:
        return
    live.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", [
        (1,'Laptop Pro 16','Electronics',1299.99,700.00,'2023-01-15'),
        (2,'Wireless Keyboard','Electronics',89.99,30.00,'2023-02-01'),
        (3,'Standing Desk','Furniture',649.00,280.00,'2023-01-20'),
        (4,'Ergonomic Chair','Furniture',449.00,190.00,'2022-11-10'),
        (5,'USB-C Hub','Electronics',59.99,18.00,'2023-03-05'),
        (6,'Monitor 4K 27"','Electronics',799.00,420.00,'2023-04-01'),
        (7,'Bookshelf','Furniture',199.00,80.00,'2023-02-15'),
        (8,'Webcam HD','Electronics',129.99,45.00,'2023-05-01'),
    ])
    live.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", [
        (1,'Alice Chen','USA','Enterprise','2022-03-10'),
        (2,'Bob Kumar','India','SMB','2022-07-22'),
        (3,'Carol Singh','UK','Enterprise','2021-11-05'),
        (4,'Dave Patel','Canada','Consumer','2023-01-18'),
        (5,'Eve Sharma','Australia','SMB','2022-09-30'),
        (6,'Frank Ali','Germany','Enterprise','2021-06-14'),
        (7,'Grace Lee','Singapore','Consumer','2023-02-28'),
        (8,'Henry Wu','Japan','SMB','2022-12-01'),
    ])
    live.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", [
        (1,1,1,2,1299.99,'2024-01-05','delivered'),
        (2,2,2,3,89.99,'2024-01-10','delivered'),
        (3,3,3,1,649.00,'2024-01-12','delivered'),
        (4,4,6,1,799.00,'2024-01-20','delivered'),
        (5,5,1,1,1299.99,'2024-02-01','delivered'),
        (6,6,4,2,449.00,'2024-02-03','delivered'),
        (7,1,5,5,59.99,'2024-02-10','shipped'),
        (8,7,8,1,129.99,'2024-02-15','shipped'),
        (9,2,6,1,799.00,'2024-02-20','processing'),
        (10,8,3,1,649.00,'2024-03-01','processing'),
        (11,3,1,1,1299.99,'2024-03-05','processing'),
        (12,4,2,2,89.99,'2024-03-10','processing'),
        (13,5,7,1,199.00,'2024-03-12','processing'),
        (14,6,8,2,129.99,'2024-03-15','processing'),
        (15,1,4,1,449.00,'2024-03-20','processing'),
    ])


# ── Connection endpoints ──────────────────────────────────────────────────────

class ConnectionCreate(BaseModel):
    name: str
    type: str                    # sqlite | postgresql | mysql
    file: Optional[str] = None   # sqlite only
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    color: Optional[str] = "#7c5cfc"


@app.get("/api/connections")
async def list_connections(request: Request):
    uid   = await _get_uid(request)
    conns = _user_conns(uid)
    live  = _user_live(uid)
    result = []
    for cid, cfg in conns.items():
        safe = {k: v for k, v in cfg.items() if k != "password"}
        safe["connected"] = cid in live
        result.append(safe)
    return result


@app.post("/api/connections")
async def add_connection(request: Request, req: ConnectionCreate):
    uid   = await _get_uid(request)
    conns = _user_conns(uid)
    cid   = str(uuid.uuid4())
    cfg   = {"id": cid, **req.dict()}
    try:
        conn = _open_connection(cfg)
        _user_live(uid)[cid] = conn
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {e}")
    conns[cid] = cfg
    _save_connections(uid)
    return {"id": cid, "name": cfg["name"], "connected": True}


@app.post("/api/connections/{conn_id}/test")
async def test_connection(request: Request, conn_id: str):
    uid = await _get_uid(request)
    cfg = _user_conns(uid).get(conn_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Connection not found")
    try:
        conn = _open_connection(cfg)
        _user_live(uid)[conn_id] = conn
        return {"ok": True, "message": "Connection successful"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.post("/api/connections/{conn_id}/disconnect")
async def disconnect(request: Request, conn_id: str):
    uid  = await _get_uid(request)
    live = _user_live(uid).pop(conn_id, None)
    if live:
        try: live.close()
        except: pass
    return {"disconnected": conn_id}


@app.delete("/api/connections/{conn_id}")
async def delete_connection(request: Request, conn_id: str):
    uid  = await _get_uid(request)
    live = _user_live(uid).pop(conn_id, None)
    if live:
        try: live.close()
        except: pass
    _user_conns(uid).pop(conn_id, None)
    _save_connections(uid)
    return {"deleted": conn_id}


@app.put("/api/connections/{conn_id}")
async def update_connection(request: Request, conn_id: str, req: ConnectionCreate):
    uid   = await _get_uid(request)
    conns = _user_conns(uid)
    if conn_id not in conns:
        raise HTTPException(status_code=404, detail="Not found")
    live = _user_live(uid).pop(conn_id, None)
    if live:
        try: live.close()
        except: pass
    cfg = {"id": conn_id, **req.dict()}
    conns[conn_id] = cfg
    _save_connections(uid)
    return {"updated": conn_id}


# ── Schema & query endpoints ──────────────────────────────────────────────────

@app.get("/api/connections/{conn_id}/schema")
async def get_schema(request: Request, conn_id: str):
    uid = await _get_uid(request)
    try:
        return _get_schema(conn_id, uid)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ExecuteRequest(BaseModel):
    sql: str

@app.post("/api/connections/{conn_id}/execute")
async def execute_sql(request: Request, conn_id: str, req: ExecuteRequest):
    uid = await _get_uid(request)
    try:
        columns, rows = _execute_sql(conn_id, req.sql, uid)
        return {"columns": columns, "rows": rows}
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/connections/{conn_id}/table/{table}/preview")
async def preview_table(request: Request, conn_id: str, table: str, limit: int = 100):
    uid = await _get_uid(request)
    try:
        cfg = _user_conns(uid).get(conn_id, {})
        t   = cfg.get("type", "sqlite")
        if t == "sqlite":
            sql = f"SELECT * FROM [{table}] LIMIT {limit}"
        elif t in ("postgresql", "duckdb"):
            sql = f'SELECT * FROM "{table}" LIMIT {limit}'
        else:
            sql = f"SELECT * FROM `{table}` LIMIT {limit}"
        columns, rows = _execute_sql(conn_id, sql, uid)
        return {"columns": columns, "rows": rows}
    except Exception as e:
        return {"error": str(e)}


# ── CSV/JSON upload ───────────────────────────────────────────────────────────

@app.post("/api/connections/{conn_id}/upload")
async def upload_file(request: Request, conn_id: str, file: UploadFile = File(...)):
    uid   = await _get_uid(request)
    fname = file.filename or "upload"
    try:
        contents = await file.read()
        raw_name = Path(fname).stem
        table_name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_") or "upload"

        if fname.lower().endswith(".json"):
            data = json.loads(contents.decode("utf-8"))
            if not isinstance(data, list) or not data:
                raise ValueError("JSON must be a non-empty array of objects")
            fieldnames = list({k for row in data for k in row})
            rows_data = [{f: row.get(f) for f in fieldnames} for row in data]
        else:
            text = contents.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            fieldnames = list(reader.fieldnames or [])
            rows_data = list(reader)

        if not fieldnames:
            raise ValueError("No columns found")

        def infer_type(vals):
            for v in vals:
                if v is None or v == "": continue
                try: int(str(v)); continue
                except: pass
                try: float(str(v)); continue
                except: return "TEXT"
            return "REAL"

        col_types = {c: infer_type([r.get(c) for r in rows_data]) for c in fieldnames}

        cfg  = _user_conns(uid).get(conn_id, {})
        t    = cfg.get("type", "sqlite")
        live = _get_live(conn_id, uid)
        cur  = live.cursor() if t != "duckdb" else None

        if t == "duckdb":
            live.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            col_defs = ", ".join(f'"{c}" VARCHAR' for c in fieldnames)
            live.execute(f'CREATE TABLE "{table_name}" ({col_defs})')
            for row in rows_data:
                vals = [row.get(c) for c in fieldnames]
                placeholders = ",".join("?" for _ in fieldnames)
                cols_q = ",".join(f'"{c}"' for c in fieldnames)
                live.execute(f'INSERT INTO "{table_name}" ({cols_q}) VALUES ({placeholders})', vals)
            live.commit()
            return {"table": table_name, "rows": len(rows_data), "columns": fieldnames}
        elif t == "sqlite":
            cur.execute(f"DROP TABLE IF EXISTS [{table_name}]")
            col_defs = ", ".join(f'[{c}] {col_types[c]}' for c in fieldnames)
            cur.execute(f"CREATE TABLE [{table_name}] ({col_defs})")
            for row in rows_data:
                vals = [row.get(c) for c in fieldnames]
                placeholders = ",".join("?" for _ in fieldnames)
                cur.execute(f"INSERT INTO [{table_name}] VALUES ({placeholders})", vals)
        elif t == "postgresql":
            cur.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            col_defs = ", ".join(f'"{c}" TEXT' for c in fieldnames)
            cur.execute(f'CREATE TABLE "{table_name}" ({col_defs})')
            for row in rows_data:
                vals = [row.get(c) for c in fieldnames]
                placeholders = ",".join("%s" for _ in fieldnames)
                cols_q = ",".join(f'"{c}"' for c in fieldnames)
                cur.execute(f'INSERT INTO "{table_name}" ({cols_q}) VALUES ({placeholders})', vals)
        elif t == "mysql":
            cur.execute(f"DROP TABLE IF EXISTS `{table_name}`")
            col_defs = ", ".join(f'`{c}` TEXT' for c in fieldnames)
            cur.execute(f"CREATE TABLE `{table_name}` ({col_defs})")
            for row in rows_data:
                vals = [row.get(c) for c in fieldnames]
                placeholders = ",".join("%s" for _ in fieldnames)
                cols_q = ",".join(f'`{c}`' for c in fieldnames)
                cur.execute(f"INSERT INTO `{table_name}` ({cols_q}) VALUES ({placeholders})", vals)

        live.commit()
        return {"table": table_name, "rows": len(rows_data), "columns": fieldnames}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── WebSocket — streaming self-healing NL query ───────────────────────────────

@app.websocket("/ws/query/{conn_id}")
async def ws_query(websocket: WebSocket, conn_id: str):
    await websocket.accept()

    async def send(obj: dict):
        await websocket.send_text(json.dumps(obj))

    try:
        raw     = await websocket.receive_text()
        payload = json.loads(raw)
        question = payload.get("question", "").strip()
        if not question:
            await send({"type": "failed", "message": "Empty question"})
            return

        # Auth: token in WS payload (browsers can't set WS headers)
        uid = "dev-user"
        if _firebase_enabled():
            token = payload.get("token", "")
            if not token:
                await send({"type": "failed", "message": "Unauthorized"})
                return
            try:
                decoded = fb_auth.verify_id_token(token)
                uid = decoded["uid"]
            except Exception as e:
                await send({"type": "failed", "message": f"Invalid token: {e}"})
                return

        cfg = _user_conns(uid).get(conn_id)
        if not cfg:
            await send({"type": "failed", "message": "Connection not found"})
            return

        # Seed demo connections for new users on first query
        if not _user_conns(uid):
            _seed_user_demos(uid)

        db_type     = cfg.get("type", "sqlite")
        dialect     = _dialect_rules(db_type)
        try:
            schema_text = _rich_schema_for_llm(conn_id, uid)
        except Exception:
            # Fallback to simple schema
            simple = _get_schema(conn_id, uid)
            schema_text = "\n".join(
                f"TABLE {t['table']} (" +
                ", ".join(f"{c['name']} {c['type']}" for c in t["columns"]) + ")"
                for t in simple
            )

        system_prompt = f"""\
You are an expert {db_type.upper()} SQL query writer. Your ONLY job is to write correct SQL.

OUTPUT FORMAT — CRITICAL:
- Respond with ONLY a ```sql code block containing one SQL statement
- No explanations, no comments, no prose — just the SQL inside the code block
- Never output multiple queries

{dialect}

DATABASE SCHEMA:
{schema_text}

QUERY WRITING GUIDELINES:
- Use EXACT column and table names as shown in the schema above
- When joining tables, use the [PRIMARY KEY] and → FK hints shown in the schema
- For "top N" requests: use ORDER BY + LIMIT
- For aggregations: use GROUP BY with the correct columns
- For counting: SELECT COUNT(*) or COUNT(DISTINCT col)
- For averages/sums: AVG(), SUM() with proper GROUP BY
- Always alias aggregation columns for clarity: SUM(amount) AS total_amount
- Sample rows are shown to help you understand data formats and values
"""

        client     = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY", ""))
        last_sql   = None
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            healing = attempt > 1
            await send({
                "type": "attempt_start", "attempt": attempt,
                "max_retries": MAX_RETRIES, "healing": healing,
            })

            if not healing:
                user_msg = f"Question: {question}\n\nWrite the SQL query:"
            else:
                user_msg = (
                    f"The following SQL query failed. Fix it.\n\n"
                    f"Original question: {question}\n\n"
                    f"Failing query:\n```sql\n{last_sql}\n```\n\n"
                    f"Error message: {last_error}\n\n"
                    f"Common fixes:\n"
                    f"- Check column/table names match the schema exactly\n"
                    f"- Check for type mismatches\n"
                    f"- Ensure GROUP BY includes all non-aggregated SELECT columns\n\n"
                    f"Write the corrected SQL query:"
                )

            full_text = ""
            stream = await client.chat.completions.create(
                model=MODEL, max_tokens=MAX_TOKENS, stream=True,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_msg},
                ],
            )
            async for chunk in stream:
                text = chunk.choices[0].delta.content or ""
                if text:
                    full_text += text
                    await send({"type": "sql_chunk", "text": text})

            sql      = _extract_sql(full_text)
            last_sql = sql
            await send({"type": "sql_complete", "sql": sql})
            await send({"type": "executing"})

            try:
                columns, rows = _execute_sql(conn_id, sql, uid)
                await send({
                    "type": "result", "sql": sql,
                    "columns": columns, "rows": rows, "attempts": attempt,
                })
                return
            except Exception as exc:
                last_error = str(exc)
                await send({"type": "sql_error", "message": last_error, "attempt": attempt})

        await send({"type": "failed",
                    "message": f"Failed after {MAX_RETRIES} attempts. Last error: {last_error}"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await send({"type": "failed", "message": str(e)})
        except Exception:
            pass


# ── Firebase public config (safe to expose) ───────────────────────────────────

@app.get("/api/firebase-config")
async def firebase_config():
    """Return public Firebase config for the frontend."""
    cfg = {
        "apiKey":            os.environ.get("FIREBASE_API_KEY", ""),
        "authDomain":        os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
        "projectId":         os.environ.get("FIREBASE_PROJECT_ID", ""),
        "storageBucket":     os.environ.get("FIREBASE_STORAGE_BUCKET", ""),
        "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID", ""),
        "appId":             os.environ.get("FIREBASE_APP_ID", ""),
    }
    # Only return config if Firebase is actually configured
    if not cfg["projectId"]:
        return {}
    return cfg


# ── First-login bootstrap ─────────────────────────────────────────────────────

@app.post("/api/bootstrap")
async def bootstrap(request: Request):
    """Called once after login — seeds demo connections for new users."""
    uid = await _get_uid(request)
    _seed_user_demos(uid)
    return {"uid": uid, "connections": len(_user_conns(uid))}


# ── Health check (for deployment platforms) ───────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "users": len(_connections)}


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


@app.get("/firebase-config.js")
async def serve_firebase_config():
    return FileResponse("static/firebase-config.js", media_type="application/javascript")
