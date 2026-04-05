"""
DBeaver-style SQL Agent — connect to any DB, query with natural language.
Supports: SQLite, PostgreSQL, MySQL/MariaDB
"""

import base64
import collections
import csv
import datetime
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# Encryption for stored passwords
try:
    from cryptography.fernet import Fernet
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False

# SSH tunnel support (imported lazily to avoid hard crash if not installed)
try:
    from sshtunnel import SSHTunnelForwarder as _SSHTunnelForwarder
    import paramiko as _paramiko
    _SSH_AVAILABLE = True
except ImportError:
    _SSH_AVAILABLE = False

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
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# ── Config ───────────────────────────────────────────────────────────────────
DATA_DIR    = Path(os.environ.get("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
MODEL       = "llama-3.3-70b-versatile"
MAX_TOKENS  = 2048
MAX_RETRIES = 3
PAGE_SIZE   = 500   # rows per page in results

# ── Encryption key for stored passwords ──────────────────────────────────────
def _get_fernet():
    if not _CRYPTO_OK:
        return None
    key_file = DATA_DIR / ".enc_key"
    if key_file.exists():
        key = key_file.read_bytes()
    else:
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        key_file.chmod(0o600)
    return Fernet(key)

_fernet = _get_fernet()

def _encrypt(val: str) -> str:
    if _fernet and val:
        return "enc:" + _fernet.encrypt(val.encode()).decode()
    return val

def _decrypt(val: str) -> str:
    if _fernet and val and val.startswith("enc:"):
        try:
            return _fernet.decrypt(val[4:].encode()).decode()
        except Exception:
            return ""
    return val

# ── Rate limiter ──────────────────────────────────────────────────────────────
_rate_buckets: dict[str, collections.deque] = {}

def _check_rate(uid: str, key: str = "ai", limit: int = 30, window: int = 60) -> bool:
    """Return True if allowed, False if rate-limited. limit req per window seconds."""
    bucket_key = f"{uid}:{key}"
    now = time.time()
    if bucket_key not in _rate_buckets:
        _rate_buckets[bucket_key] = collections.deque()
    dq = _rate_buckets[bucket_key]
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True

# ── Firebase Admin init ───────────────────────────────────────────────────────
import firebase_admin
from firebase_admin import auth as fb_auth, credentials as fb_creds

_FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
_FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
_FIREBASE_SERVICE_ACCOUNT_PATH = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", "")
_FIREBASE_INIT_ERROR: Optional[str] = None
logger = logging.getLogger(__name__)


def _firebase_enabled() -> bool:
    return bool(
        _FIREBASE_PROJECT_ID
        or _FIREBASE_SERVICE_ACCOUNT_JSON
        or _FIREBASE_SERVICE_ACCOUNT_PATH
    )


def _init_firebase():
    global _FIREBASE_INIT_ERROR
    if firebase_admin._apps:
        return
    try:
        if _FIREBASE_SERVICE_ACCOUNT_JSON:
            cred = fb_creds.Certificate(json.loads(_FIREBASE_SERVICE_ACCOUNT_JSON))
            firebase_admin.initialize_app(cred)
        elif _FIREBASE_SERVICE_ACCOUNT_PATH:
            cred = fb_creds.Certificate(_FIREBASE_SERVICE_ACCOUNT_PATH)
            firebase_admin.initialize_app(cred)
        elif _FIREBASE_PROJECT_ID:
            # Minimal init — only needs project ID to verify tokens
            firebase_admin.initialize_app(options={"projectId": _FIREBASE_PROJECT_ID})
        _FIREBASE_INIT_ERROR = None
    except Exception as exc:
        _FIREBASE_INIT_ERROR = str(exc)
        logger.exception("Firebase initialization failed")

_init_firebase()


async def _get_uid(request: Request) -> str:
    """Extract and verify Firebase ID token → return uid. Raises 401 on failure."""
    if not _firebase_enabled():
        # Auth disabled (local dev) — use a fixed dev uid
        return "dev-user"
    if _FIREBASE_INIT_ERROR:
        raise HTTPException(status_code=503, detail="Firebase authentication is not configured correctly")
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
# _tunnels[uid][conn_id]     = SSHTunnelForwarder (when SSH tunnel is active)
_connections: dict[str, dict[str, dict]] = {}
_live:        dict[str, dict[str, Any]]  = {}
_tunnels:     dict[str, dict[str, Any]]  = {}


def _user_conns(uid: str) -> dict[str, dict]:
    if uid not in _connections:
        _connections[uid] = {}
        _load_connections(uid)
    return _connections[uid]


def _user_live(uid: str) -> dict[str, Any]:
    if uid not in _live:
        _live[uid] = {}
    return _live[uid]


def _user_tunnels(uid: str) -> dict[str, Any]:
    if uid not in _tunnels:
        _tunnels[uid] = {}
    return _tunnels[uid]


def _start_tunnel(cfg: dict):
    """Start an SSH tunnel and return the SSHTunnelForwarder. Raises on failure."""
    if not _SSH_AVAILABLE:
        raise RuntimeError("SSH tunnel requires 'sshtunnel' and 'paramiko' packages")
    ssh_host  = cfg["ssh_host"]
    ssh_port  = int(cfg.get("ssh_port", 22))
    ssh_user  = cfg.get("ssh_user", "")
    ssh_pass  = cfg.get("ssh_password")
    ssh_key   = cfg.get("ssh_private_key")   # PEM text

    db_host   = cfg.get("host", "localhost")
    db_type   = cfg.get("type", "postgresql")
    db_port   = int(cfg.get("port") or (5439 if db_type == "redshift" else 5432))

    kwargs: dict = {
        "ssh_username":        ssh_user,
        "remote_bind_address": (db_host, db_port),
    }
    if ssh_key:
        pkey = _paramiko.RSAKey.from_private_key(io.StringIO(ssh_key))
        kwargs["ssh_pkey"] = pkey
    elif ssh_pass:
        kwargs["ssh_password"] = ssh_pass

    tunnel = _SSHTunnelForwarder((ssh_host, ssh_port), **kwargs)
    tunnel.start()
    return tunnel


def _stop_tunnel(uid: str, conn_id: str):
    """Stop and remove an SSH tunnel for a connection if one exists."""
    tunnel = _user_tunnels(uid).pop(conn_id, None)
    if tunnel is not None:
        try:
            tunnel.stop()
        except Exception:
            pass


def _connections_file(uid: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", uid)
    return DATA_DIR / f"connections_{safe}.json"


def _save_connections(uid: str):
    """Save connections, encrypting sensitive fields."""
    _ENCRYPT_FIELDS = {"password", "ssh_password", "ssh_private_key"}
    conns = _connections.get(uid, {})
    out = {}
    for cid, cfg in conns.items():
        row = {}
        for k, v in cfg.items():
            if k in _ENCRYPT_FIELDS and v:
                row[k] = _encrypt(str(v))
            else:
                row[k] = v
        out[cid] = row
    _connections_file(uid).write_text(json.dumps(out, indent=2))


def _load_connections(uid: str):
    """Load connections, decrypting sensitive fields."""
    f = _connections_file(uid)
    if f.exists():
        try:
            data = json.loads(f.read_text())
        except Exception:
            return
        _DECRYPT_FIELDS = {"password", "ssh_password", "ssh_private_key"}
        for cid, cfg in data.items():
            for k in _DECRYPT_FIELDS:
                if k in cfg and cfg[k]:
                    cfg[k] = _decrypt(cfg[k])
        _connections.setdefault(uid, {}).update(data)


# ── Driver helpers ────────────────────────────────────────────────────────────

def _open_connection(cfg: dict, local_port: Optional[int] = None):
    """Open and return a DB connection for the given config.

    If local_port is given (SSH tunnel active), connect to 127.0.0.1:local_port
    instead of the configured host/port.
    """
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
    elif t in ("postgresql", "redshift"):
        import psycopg2
        default_port = 5439 if t == "redshift" else 5432
        host = "127.0.0.1" if local_port else cfg.get("host", "localhost")
        port = local_port if local_port else int(cfg.get("port", default_port))
        return psycopg2.connect(
            host=host,
            port=port,
            dbname=cfg.get("database", ""),
            user=cfg.get("user", ""),
            password=cfg.get("password", ""),
            connect_timeout=10,
            sslmode=cfg.get("sslmode", "prefer") if t == "redshift" else "prefer",
        )
    elif t == "mysql":
        import pymysql
        host = "127.0.0.1" if local_port else cfg.get("host", "localhost")
        port = local_port if local_port else int(cfg.get("port", 3306))
        return pymysql.connect(
            host=host,
            port=port,
            database=cfg.get("database", ""),
            user=cfg.get("user", ""),
            password=cfg.get("password", ""),
            connect_timeout=10,
            cursorclass=pymysql.cursors.DictCursor,
        )
    else:
        raise ValueError(f"Unknown DB type: {t}")


def _get_live(conn_id: str, uid: str = "dev-user"):
    """Return live connection, reopening if closed/lost.
    Ensures SSH tunnel is running first when ssh_enabled=True."""
    cfg = _user_conns(uid).get(conn_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Connection not found")

    t = cfg["type"]

    # ── Ensure SSH tunnel is alive ────────────────────────────────────────────
    local_port: Optional[int] = None
    if cfg.get("ssh_enabled"):
        tunnel = _user_tunnels(uid).get(conn_id)
        if tunnel is None or not tunnel.is_alive:
            # Stop stale tunnel if present
            if tunnel is not None:
                try:
                    tunnel.stop()
                except Exception:
                    pass
            tunnel = _start_tunnel(cfg)
            _user_tunnels(uid)[conn_id] = tunnel
        local_port = tunnel.local_bind_port

    live = _user_live(uid).get(conn_id)

    # Test if still alive
    try:
        if live is not None:
            if t == "sqlite":
                live.cursor().execute("SELECT 1")
            elif t == "duckdb":
                live.execute("SELECT 1")
            elif t in ("postgresql", "redshift"):
                live.cursor().execute("SELECT 1")
                live.rollback()
            elif t == "mysql":
                live.cursor().execute("SELECT 1")
            return live
    except Exception:
        pass

    # Reopen
    live = _open_connection(cfg, local_port=local_port)
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

    elif t in ("postgresql", "redshift"):
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
        elif t in ("postgresql", "redshift"):
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

    elif t in ("postgresql", "redshift"):
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name
        """)
        table_names = [r[0] for r in cur.fetchall()]
        tables = []
        for tbl in table_names:
            # Columns + PK info
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

    elif db_type == "redshift":
        return """\
AMAZON REDSHIFT-SPECIFIC RULES:
- Quote identifiers with double quotes: "column_name", "table_name"
- Default port is 5439 (not 5432)
- AUTO-INCREMENT: use IDENTITY(1,1) — NOT SERIAL or AUTO_INCREMENT
  e.g. id INTEGER IDENTITY(1,1)
- VARCHAR(MAX) is equivalent to VARCHAR(65535) in Redshift
- Date/time functions: GETDATE(), CURRENT_DATE, DATE_TRUNC('month', col),
  DATEDIFF('day', start_date, end_date), DATEADD('month', 1, date_col)
- String: LOWER(), UPPER(), TRIM(), LEN(), LEFT(), RIGHT(), SPLIT_PART()
- Case-insensitive search: ILIKE '%pattern%'
- String concat: col1 || ' ' || col2
- Use COALESCE(col, default) for nulls
- Window functions fully supported: ROW_NUMBER(), RANK(), LAG(), LEAD()
- Distribution: tables may have DISTKEY/SORTKEY but you do NOT add them in queries
- Do NOT use: CREATE INDEX CONCURRENTLY, SERIAL type, pg_catalog schema queries
- Do NOT use backtick quotes
- Avoid SELECT * on large tables — always specify columns
- Use LIMIT for top-N queries"""
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


# ── LLM helpers ──────────────────────────────────────────────────────────────

async def _llm(system: str, user: str, max_tokens: int = 1024) -> str:
    """Single non-streaming LLM call."""
    client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY", ""))
    resp = await client.chat.completions.create(
        model=MODEL, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system},
                  {"role": "user",   "content": user}],
    )
    return resp.choices[0].message.content or ""


async def _llm_sse(system: str, user: str, max_tokens: int = 1024):
    """Async generator yielding SSE-formatted chunks for StreamingResponse."""
    client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY", ""))
    stream = await client.chat.completions.create(
        model=MODEL, max_tokens=max_tokens, stream=True,
        messages=[{"role": "system", "content": system},
                  {"role": "user",   "content": user}],
    )
    async for chunk in stream:
        text = chunk.choices[0].delta.content or ""
        if text:
            yield f"data: {json.dumps({'text': text})}\n\n"
    yield "data: [DONE]\n\n"


# ── Query history (persisted to disk) ────────────────────────────────────────
_history: dict[str, dict[str, list]] = {}


def _history_file(uid: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", uid)
    return DATA_DIR / f"history_{safe}.json"


def _load_history(uid: str):
    f = _history_file(uid)
    if f.exists():
        try:
            _history[uid] = json.loads(f.read_text())
        except Exception:
            _history[uid] = {}


def _user_history(uid: str) -> dict[str, list]:
    if uid not in _history:
        _history[uid] = {}
        _load_history(uid)
    return _history[uid]


def _push_history(uid: str, conn_id: str, question: str, sql: str, row_count: int):
    h = _user_history(uid)
    if conn_id not in h:
        h[conn_id] = []
    h[conn_id].insert(0, {
        "id": str(uuid.uuid4()),
        "question": question,
        "sql": sql,
        "row_count": row_count,
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
    })
    h[conn_id] = h[conn_id][:100]
    try:
        _history_file(uid).write_text(json.dumps(h, indent=2))
    except Exception:
        pass


# ── Saved queries (persisted to disk) ────────────────────────────────────────
_saved: dict[str, dict[str, list]] = {}


def _saved_file(uid: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", uid)
    return DATA_DIR / f"saved_{safe}.json"


def _load_saved(uid: str):
    f = _saved_file(uid)
    if f.exists():
        try:
            _saved[uid] = json.loads(f.read_text())
        except Exception:
            pass


def _user_saved(uid: str) -> dict[str, list]:
    if uid not in _saved:
        _saved[uid] = {}
        _load_saved(uid)
    return _saved[uid]


def _persist_saved(uid: str):
    _saved_file(uid).write_text(json.dumps(_user_saved(uid), indent=2))


# ── Shared results (persisted to disk) ───────────────────────────────────────
_shares: dict[str, dict] = {}
_SHARES_FILE = DATA_DIR / "shares.json"


def _load_shares():
    if _SHARES_FILE.exists():
        try:
            _shares.update(json.loads(_SHARES_FILE.read_text()))
        except Exception:
            pass


def _save_shares():
    try:
        _SHARES_FILE.write_text(json.dumps(_shares, indent=2))
    except Exception:
        pass


def _cleanup_shares():
    now = datetime.datetime.utcnow()
    expired = [k for k, v in _shares.items()
               if (now - datetime.datetime.fromisoformat(v["created_at"])).days >= 7]
    for k in expired:
        del _shares[k]
    if expired:
        _save_shares()


# ── Admin stats (in-memory counters) ─────────────────────────────────────────
_admin_stats: dict = {
    "total_queries": 0,
    "total_nl_queries": 0,
    "total_users": set(),
    "queries_by_hour": collections.defaultdict(int),
    "errors": 0,
}

def _track(uid: str, kind: str = "query", error: bool = False):
    _admin_stats["total_queries"] += 1
    _admin_stats["total_users"].add(uid)
    if kind == "nl":
        _admin_stats["total_nl_queries"] += 1
    if error:
        _admin_stats["errors"] += 1
    hour = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:00")
    _admin_stats["queries_by_hour"][hour] += 1

# ── Scheduled alerts ──────────────────────────────────────────────────────────
_alerts: dict[str, dict[str, dict]] = {}   # uid -> alert_id -> alert config
_ALERTS_FILE = DATA_DIR / "alerts.json"


def _load_alerts():
    if _ALERTS_FILE.exists():
        try:
            _alerts.update(json.loads(_ALERTS_FILE.read_text()))
        except Exception:
            pass


def _save_alerts():
    try:
        _ALERTS_FILE.write_text(json.dumps(_alerts, indent=2))
    except Exception:
        pass


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
    _load_shares()
    _load_alerts()
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
    type: str                       # sqlite | duckdb | postgresql | mysql | redshift
    file: Optional[str] = None      # sqlite / duckdb only
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    color: Optional[str] = "#7c5cfc"
    # SSH / VPC tunnel fields
    ssh_enabled: bool = False
    ssh_host: Optional[str] = None
    ssh_port: int = 22
    ssh_user: Optional[str] = None
    ssh_password: Optional[str] = None
    ssh_private_key: Optional[str] = None   # PEM text


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
    _stop_tunnel(uid, conn_id)
    return {"disconnected": conn_id}


@app.delete("/api/connections/{conn_id}")
async def delete_connection(request: Request, conn_id: str):
    uid  = await _get_uid(request)
    live = _user_live(uid).pop(conn_id, None)
    if live:
        try: live.close()
        except: pass
    _stop_tunnel(uid, conn_id)
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
    _stop_tunnel(uid, conn_id)
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
    page: int = 1

@app.post("/api/connections/{conn_id}/execute")
async def execute_sql(request: Request, conn_id: str, req: ExecuteRequest):
    uid = await _get_uid(request)
    try:
        columns, rows = _execute_sql(conn_id, req.sql, uid)
        _track(uid, "query")
        total = len(rows)
        page  = max(1, req.page)
        start = (page - 1) * PAGE_SIZE
        page_rows = rows[start:start + PAGE_SIZE]
        return {
            "columns": columns, "rows": page_rows,
            "total": total, "page": page,
            "page_size": PAGE_SIZE,
            "total_pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        }
    except HTTPException:
        raise
    except Exception as e:
        _track(uid, "query", error=True)
        return {"error": str(e)}


@app.get("/api/connections/{conn_id}/table/{table}/preview")
async def preview_table(request: Request, conn_id: str, table: str, limit: int = 100):
    uid = await _get_uid(request)
    try:
        cfg = _user_conns(uid).get(conn_id, {})
        t   = cfg.get("type", "sqlite")
        if t == "sqlite":
            sql = f"SELECT * FROM [{table}] LIMIT {limit}"
        elif t in ("postgresql", "redshift", "duckdb"):
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
        elif t in ("postgresql", "redshift"):
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
            if _FIREBASE_INIT_ERROR:
                await send({"type": "failed", "message": "Firebase authentication is not configured correctly"})
                return
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

        if not _check_rate(uid, "ai"):
            await send({"type": "failed", "message": "Rate limit: 30 AI requests per minute"})
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
                _push_history(uid, conn_id, question, sql, len(rows))
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


# ── Explain table (streaming) ────────────────────────────────────────────────

@app.get("/api/connections/{conn_id}/explain/{table}")
async def explain_table(request: Request, conn_id: str, table: str):
    uid = await _get_uid(request)
    if not _check_rate(uid, "ai"):
        raise HTTPException(status_code=429, detail="Rate limit: 30 AI requests per minute")
    cfg = _user_conns(uid).get(conn_id, {})
    db_type = cfg.get("type", "sqlite")
    try:
        schema = _rich_schema_for_llm(conn_id, uid)
    except Exception:
        schema = "Schema unavailable"
    tbl_list = _get_schema(conn_id, uid)
    tbl_info = next((t for t in tbl_list if t["table"] == table), {})
    cols = ", ".join(f"{c['name']} ({c['type']})" for c in tbl_info.get("columns", []))
    row_count = tbl_info.get("row_count", "?")
    system = """\
You are a database expert explaining tables to a business analyst.
Use markdown with these sections:
## Purpose
## Key Columns
## Relationships
## Common Use Cases
## Data Insights
Be concise but insightful. Focus on business meaning, not just technical details."""
    user = f"""Database: {db_type.upper()}
Table: {table} ({row_count:,} rows)
Columns: {cols}

Full schema context:
{schema}

Explain this table clearly."""
    return StreamingResponse(_llm_sse(system, user, max_tokens=800),
                             media_type="text/event-stream")


# ── Suggest starter queries ───────────────────────────────────────────────────

@app.get("/api/connections/{conn_id}/suggest")
async def suggest_queries(request: Request, conn_id: str):
    uid = await _get_uid(request)
    if not _check_rate(uid, "ai"):
        raise HTTPException(status_code=429, detail="Rate limit: 30 AI requests per minute")
    cfg = _user_conns(uid).get(conn_id, {})
    db_type = cfg.get("type", "sqlite")
    try:
        schema = _rich_schema_for_llm(conn_id, uid)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read schema")
    system = ("You are a data analyst. Generate useful natural-language questions "
              "answerable by querying this database. Output ONLY a JSON array of strings.")
    user = (f"Database: {db_type.upper()}\n\nSchema:\n{schema}\n\n"
            "Generate exactly 8 useful, varied questions covering: aggregations, trends, "
            "rankings, comparisons, lookups. Return JSON array only.")
    text = await _llm(system, user, max_tokens=600)
    try:
        m = re.search(r'\[.*?\]', text, re.DOTALL)
        questions = json.loads(m.group(0)) if m else []
    except Exception:
        questions = []
    return {"questions": questions[:8]}


# ── Data quality scan ─────────────────────────────────────────────────────────

@app.get("/api/connections/{conn_id}/quality/{table}")
async def data_quality(request: Request, conn_id: str, table: str):
    uid = await _get_uid(request)
    cfg = _user_conns(uid).get(conn_id, {})
    t   = cfg.get("type", "sqlite")
    schema = _get_schema(conn_id, uid)
    tbl_info = next((s for s in schema if s["table"] == table), None)
    if not tbl_info:
        raise HTTPException(status_code=404, detail="Table not found")
    columns   = [c["name"] for c in tbl_info["columns"]]
    row_count = tbl_info["row_count"]
    stats, issues = [], []

    def q(col: str, template: str) -> str:
        if t == "sqlite":
            return template.format(t=f"[{table}]", c=f"[{col}]")
        elif t in ("postgresql", "redshift", "duckdb"):
            return template.format(t=f'"{table}"', c=f'"{col}"')
        else:
            return template.format(t=f"`{table}`", c=f"`{col}`")

    for col in columns:
        try:
            _, nr = _execute_sql(conn_id, q(col, "SELECT COUNT(*) FROM {t} WHERE {c} IS NULL"), uid)
            null_count = nr[0][0] if nr else 0
            null_pct   = round(100 * null_count / row_count, 1) if row_count else 0
            _, dr = _execute_sql(conn_id, q(col, "SELECT COUNT(*) - COUNT(DISTINCT {c}) FROM {t}"), uid)
            dup_count = dr[0][0] if dr else 0
            stats.append({"column": col, "null_count": null_count,
                          "null_pct": null_pct, "duplicate_values": dup_count})
            if null_pct > 50:
                issues.append({"severity": "high", "column": col,
                               "issue": f"{null_pct}% NULL — column may be unused or data missing"})
            elif null_pct > 10:
                issues.append({"severity": "medium", "column": col,
                               "issue": f"{null_pct}% NULL — verify if expected"})
            if dup_count > 0 and any(col.lower().endswith(s)
                                     for s in ("id", "uuid", "email", "code", "key")):
                issues.append({"severity": "high", "column": col,
                               "issue": f"{dup_count} duplicates in what looks like a unique field"})
        except Exception:
            pass

    if row_count == 0:
        issues.append({"severity": "high", "column": None, "issue": "Table is empty"})

    return {
        "table": table, "row_count": row_count,
        "column_count": len(columns), "stats": stats, "issues": issues,
        "score": max(0, 100 - len([i for i in issues if i["severity"] == "high"]) * 15
                     - len([i for i in issues if i["severity"] == "medium"]) * 5),
    }


# ── AI Data Story (streaming) ─────────────────────────────────────────────────

class StoryRequest(BaseModel):
    question: str
    sql: str
    columns: list
    rows: list


@app.post("/api/connections/{conn_id}/story")
async def ai_story(request: Request, conn_id: str, req: StoryRequest):
    uid = await _get_uid(request)
    if not _check_rate(uid, "ai"):
        raise HTTPException(status_code=429, detail="Rate limit: 30 AI requests per minute")
    header   = " | ".join(req.columns)
    data_str = "\n".join(" | ".join(str(v) for v in row) for row in req.rows[:25])
    system   = """\
You are a senior data analyst writing a business intelligence insight report.
Given query results, write a concise markdown narrative.
## Key Findings  (3-5 bullet points with specific numbers)
## Trend or Pattern  (what stands out)
## Recommendation  (one actionable next step)
Max 250 words. Be specific, use the actual data values."""
    user = (f"Question: {req.question}\nSQL: {req.sql}\n"
            f"Total rows: {len(req.rows)}\n\nSample data:\n{header}\n{data_str}\n\n"
            "Write the business narrative.")
    return StreamingResponse(_llm_sse(system, user, max_tokens=600),
                             media_type="text/event-stream")


# ── Optimize SQL ──────────────────────────────────────────────────────────────

class OptimizeRequest(BaseModel):
    sql: str


@app.post("/api/connections/{conn_id}/optimize")
async def optimize_query(request: Request, conn_id: str, req: OptimizeRequest):
    uid     = await _get_uid(request)
    if not _check_rate(uid, "ai"):
        raise HTTPException(status_code=429, detail="Rate limit: 30 AI requests per minute")
    cfg     = _user_conns(uid).get(conn_id, {})
    db_type = cfg.get("type", "sqlite")
    try:
        schema = _rich_schema_for_llm(conn_id, uid)
    except Exception:
        schema = ""
    system = (f"You are a SQL performance expert for {db_type.upper()}. "
              "Respond ONLY with JSON: "
              '{"issues":[],"suggestions":[],"optimized_sql":"","explanation":""}')
    user   = f"DB: {db_type.upper()}\nSchema:\n{schema}\n\nQuery:\n{req.sql}"
    text   = await _llm(system, user, max_tokens=1000)
    try:
        m      = re.search(r'\{.*\}', text, re.DOTALL)
        result = json.loads(m.group(0)) if m else {}
    except Exception:
        result = {}
    result.setdefault("issues", [])
    result.setdefault("suggestions", [])
    result.setdefault("optimized_sql", req.sql)
    result.setdefault("explanation", text)
    return result


# ── Schema Doctor (streaming) ─────────────────────────────────────────────────

@app.get("/api/connections/{conn_id}/schema-doctor")
async def schema_doctor(request: Request, conn_id: str):
    uid = await _get_uid(request)
    if not _check_rate(uid, "ai"):
        raise HTTPException(status_code=429, detail="Rate limit: 30 AI requests per minute")
    cfg = _user_conns(uid).get(conn_id, {})
    db_type = cfg.get("type", "sqlite")
    try:
        schema = _rich_schema_for_llm(conn_id, uid)
    except Exception:
        raise HTTPException(status_code=500, detail="Schema unavailable")
    system = """\
You are a senior database architect. Analyze a schema and give a health report in markdown.
Use EXACTLY these sections:
## Schema Health Score: X/100
## ✅ What's Good
## ⚠️ Issues Found
## 🔧 Recommendations
## 🔑 Missing Indexes Suggested
## 📊 Normalization Notes
Be specific: name the tables and columns. Focus on actionable improvements."""
    user   = f"Database: {db_type.upper()}\n\nFull Schema:\n{schema}\n\nProvide health report."
    return StreamingResponse(_llm_sse(system, user, max_tokens=1200),
                             media_type="text/event-stream")


# ── Generate API endpoint code ────────────────────────────────────────────────

class GenerateAPIRequest(BaseModel):
    sql: str
    framework: str = "fastapi"


@app.post("/api/connections/{conn_id}/generate-api")
async def generate_api(request: Request, conn_id: str, req: GenerateAPIRequest):
    uid     = await _get_uid(request)
    if not _check_rate(uid, "ai"):
        raise HTTPException(status_code=429, detail="Rate limit: 30 AI requests per minute")
    cfg     = _user_conns(uid).get(conn_id, {})
    db_type = cfg.get("type", "sqlite")
    fws = {"fastapi": "FastAPI (Python) with psycopg2/sqlite3",
           "express": "Express.js (Node.js) with pg/mysql2",
           "flask":   "Flask (Python)"}
    fw = fws.get(req.framework, "FastAPI (Python)")
    system = (f"Generate a complete, production-ready {fw} REST API endpoint for this SQL query. "
              "Include error handling, input validation for any parameters, and connection setup. "
              "Return ONLY the code block, no prose.")
    user   = f"Database: {db_type.upper()}\nSQL: {req.sql}\nGenerate the {fw} endpoint."
    code   = await _llm(system, user, max_tokens=1000)
    return {"code": code, "framework": req.framework}


# ── Auto chart suggestion ─────────────────────────────────────────────────────

@app.post("/api/connections/{conn_id}/chart-suggest")
async def chart_suggest(request: Request, conn_id: str, body: dict = None):
    await _get_uid(request)
    if body is None:
        body = await request.json()
    columns = body.get("columns", [])
    rows    = body.get("rows", [])
    if not columns or not rows:
        return {"chart_type": "bar", "x": 0, "y": 1, "reason": "Default"}

    # Detect column types from sample data
    def is_numeric(col_idx):
        vals = [r[col_idx] for r in rows[:20] if r[col_idx] is not None]
        return vals and all(isinstance(v, (int, float)) for v in vals)

    def is_date(col_name):
        n = col_name.lower()
        return any(k in n for k in ("date", "time", "month", "year", "day", "week"))

    numeric_cols = [i for i in range(len(columns)) if is_numeric(i)]
    text_cols    = [i for i in range(len(columns)) if i not in numeric_cols]
    date_cols    = [i for i in text_cols if is_date(columns[i])]

    if date_cols and numeric_cols:
        return {"chart_type": "line", "x": date_cols[0], "y": numeric_cols[0],
                "reason": "Date + numeric → time series line chart"}
    if text_cols and numeric_cols:
        if len(rows) <= 6:
            return {"chart_type": "pie", "x": text_cols[0], "y": numeric_cols[0],
                    "reason": "Few categories → pie chart"}
        return {"chart_type": "bar", "x": text_cols[0], "y": numeric_cols[0],
                "reason": "Category + numeric → bar chart"}
    if len(numeric_cols) >= 2:
        return {"chart_type": "bar", "x": numeric_cols[0], "y": numeric_cols[1],
                "reason": "Two numeric columns → bar chart"}
    return {"chart_type": "bar", "x": 0, "y": min(1, len(columns) - 1), "reason": "Default"}


# ── Query history endpoints ───────────────────────────────────────────────────

@app.get("/api/connections/{conn_id}/history")
async def get_history(request: Request, conn_id: str):
    uid = await _get_uid(request)
    return _user_history(uid).get(conn_id, [])


@app.delete("/api/connections/{conn_id}/history")
async def clear_history(request: Request, conn_id: str):
    uid = await _get_uid(request)
    _user_history(uid)[conn_id] = []
    return {"cleared": True}


# ── Saved queries endpoints ───────────────────────────────────────────────────

class SaveQueryRequest(BaseModel):
    name: str
    sql: str
    question: str = ""


@app.get("/api/connections/{conn_id}/saved")
async def get_saved(request: Request, conn_id: str):
    uid = await _get_uid(request)
    return _user_saved(uid).get(conn_id, [])


@app.post("/api/connections/{conn_id}/saved")
async def save_query(request: Request, conn_id: str, req: SaveQueryRequest):
    uid = await _get_uid(request)
    s   = _user_saved(uid)
    s.setdefault(conn_id, [])
    entry = {
        "id": str(uuid.uuid4()),
        "name": req.name.strip() or "Untitled",
        "sql": req.sql,
        "question": req.question,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    s[conn_id].insert(0, entry)
    _persist_saved(uid)
    return entry


@app.delete("/api/connections/{conn_id}/saved/{query_id}")
async def delete_saved(request: Request, conn_id: str, query_id: str):
    uid = await _get_uid(request)
    s   = _user_saved(uid)
    if conn_id in s:
        s[conn_id] = [q for q in s[conn_id] if q["id"] != query_id]
        _persist_saved(uid)
    return {"deleted": query_id}


# ── Share results ─────────────────────────────────────────────────────────────

class ShareRequest(BaseModel):
    sql: str
    question: str
    columns: list
    rows: list
    conn_name: str


@app.post("/api/share")
async def create_share(request: Request, req: ShareRequest):
    uid = await _get_uid(request)
    _cleanup_shares()
    share_id = str(uuid.uuid4())[:12]
    _shares[share_id] = {
        "sql": req.sql, "question": req.question,
        "columns": req.columns, "rows": req.rows[:500],
        "conn_name": req.conn_name,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "uid": uid,
    }
    return {"share_id": share_id}


@app.get("/api/share/{share_id}")
async def get_share(share_id: str):
    share = _shares.get(share_id)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found or expired (7-day limit)")
    return share


@app.get("/share/{share_id}")
async def serve_share_page(share_id: str):
    return FileResponse("static/index.html")


# ── Admin stats ──────────────────────────────────────────────────────────────

@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    uid = await _get_uid(request)
    stats = _admin_stats.copy()
    stats["total_users"] = len(stats["total_users"])
    # Keep only last 24 hours of hourly data
    now = datetime.datetime.utcnow()
    cutoff = (now - datetime.timedelta(hours=23)).strftime("%Y-%m-%d %H:00")
    stats["queries_by_hour"] = {
        k: v for k, v in stats["queries_by_hour"].items() if k >= cutoff
    }
    stats["uid"] = uid
    return stats


# ── Multi-turn AI Chat WebSocket ──────────────────────────────────────────────

@app.websocket("/ws/chat/{conn_id}")
async def ws_chat(websocket: WebSocket, conn_id: str):
    await websocket.accept()

    async def send(obj: dict):
        await websocket.send_text(json.dumps(obj))

    uid = "dev-user"
    if _firebase_enabled():
        try:
            init_msg = json.loads(await websocket.receive_text())
            token = init_msg.get("token", "")
            if not token:
                await send({"type": "error", "message": "Unauthorized"})
                return
            decoded = fb_auth.verify_id_token(token)
            uid = decoded["uid"]
        except Exception as e:
            await send({"type": "error", "message": f"Auth failed: {e}"})
            return
    else:
        # dev mode: skip auth handshake
        await websocket.receive_text()  # consume init message

    cfg = _user_conns(uid).get(conn_id, {})
    db_type = cfg.get("type", "sqlite")
    try:
        schema = _rich_schema_for_llm(conn_id, uid)
    except Exception:
        schema = "Schema unavailable"

    system = f"""\
You are a helpful data analyst with access to a {db_type.upper()} database.
You help users explore and understand their data through conversation.
When asked to write SQL, wrap it in ```sql code blocks.
Be concise, friendly, and data-driven. Reference actual schema details.

Schema:
{schema}"""

    conversation: list[dict] = []
    await send({"type": "ready", "message": "Chat ready"})

    try:
        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            user_msg = payload.get("message", "").strip()
            if not user_msg:
                continue

            if not _check_rate(uid, "ai"):
                await send({"type": "error", "message": "Rate limit: 30 AI requests per minute"})
                continue

            conversation.append({"role": "user", "content": user_msg})
            # Keep last 20 turns (10 exchanges)
            if len(conversation) > 20:
                conversation = conversation[-20:]

            _track(uid, "nl")

            client = groq.AsyncGroq(api_key=GROQ_KEY)
            messages = [{"role": "system", "content": system}] + conversation
            full_reply = ""
            try:
                stream = await client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=messages,
                    max_tokens=800,
                    stream=True,
                )
                async for chunk in stream:
                    delta = chunk.choices[0].delta.content or ""
                    if delta:
                        full_reply += delta
                        await send({"type": "delta", "content": delta})

                conversation.append({"role": "assistant", "content": full_reply})
                await send({"type": "done"})
            except Exception as e:
                await send({"type": "error", "message": str(e)})
    except Exception:
        pass


# ── Scheduled Alerts endpoints ────────────────────────────────────────────────

class AlertRequest(BaseModel):
    name: str
    conn_id: str
    sql: str
    condition: str = "always"   # always | gt | lt | eq | not_empty | empty
    threshold: float = 0
    schedule: str = "hourly"    # hourly | daily | weekly
    notify_email: str = ""


@app.get("/api/alerts")
async def get_alerts(request: Request):
    uid = await _get_uid(request)
    return list(_alerts.get(uid, {}).values())


@app.post("/api/alerts")
async def create_alert(request: Request, req: AlertRequest):
    uid = await _get_uid(request)
    alert_id = str(uuid.uuid4())
    alert = {
        "id": alert_id,
        "name": req.name,
        "conn_id": req.conn_id,
        "sql": req.sql,
        "condition": req.condition,
        "threshold": req.threshold,
        "schedule": req.schedule,
        "notify_email": req.notify_email,
        "enabled": True,
        "last_run": None,
        "last_result": None,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    _alerts.setdefault(uid, {})[alert_id] = alert
    _save_alerts()
    return alert


@app.delete("/api/alerts/{alert_id}")
async def delete_alert(request: Request, alert_id: str):
    uid = await _get_uid(request)
    if uid in _alerts and alert_id in _alerts[uid]:
        del _alerts[uid][alert_id]
        _save_alerts()
    return {"deleted": alert_id}


@app.post("/api/alerts/{alert_id}/toggle")
async def toggle_alert(request: Request, alert_id: str):
    uid = await _get_uid(request)
    alert = _alerts.get(uid, {}).get(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert["enabled"] = not alert["enabled"]
    _save_alerts()
    return alert


@app.post("/api/alerts/{alert_id}/run")
async def run_alert_now(request: Request, alert_id: str):
    uid = await _get_uid(request)
    alert = _alerts.get(uid, {}).get(alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    try:
        cols, rows = _execute_sql(alert["conn_id"], alert["sql"], uid)
        value = rows[0][0] if rows and rows[0] else None
        alert["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"
        alert["last_result"] = {"value": value, "columns": cols, "rows": rows[:5]}
        _save_alerts()
        return {"value": value, "columns": cols, "rows": rows[:5], "triggered": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Schema autocomplete endpoint ──────────────────────────────────────────────

@app.get("/api/connections/{conn_id}/autocomplete")
async def autocomplete(request: Request, conn_id: str):
    uid = await _get_uid(request)
    try:
        schema = _get_schema(conn_id, uid)
    except Exception:
        return {"tables": [], "columns": []}
    tables = [t["table"] for t in schema]
    columns = []
    for t in schema:
        for c in t.get("columns", []):
            columns.append({"table": t["table"], "column": c["name"], "type": c.get("type", "")})
    return {"tables": tables, "columns": columns}


# ── ER Diagram endpoint ───────────────────────────────────────────────────────

@app.get("/api/connections/{conn_id}/er-diagram")
async def er_diagram(request: Request, conn_id: str):
    uid = await _get_uid(request)
    try:
        schema = _get_schema(conn_id, uid)
    except Exception:
        raise HTTPException(status_code=500, detail="Could not read schema")

    tables = []
    for t in schema:
        tables.append({
            "name": t["table"],
            "columns": t.get("columns", []),
            "row_count": t.get("row_count", 0),
        })

    # Detect FK relationships by column name heuristics
    table_names = {t["name"] for t in tables}
    relationships = []
    for t in tables:
        for c in t["columns"]:
            col = c["name"].lower()
            # Match patterns: other_table_id, other_table_fk, fk_other_table
            for candidate in table_names:
                cand_lower = candidate.lower()
                if candidate == t["name"]:
                    continue
                if col == f"{cand_lower}_id" or col == f"{cand_lower}_fk" or col == f"fk_{cand_lower}":
                    relationships.append({
                        "from_table": t["name"],
                        "from_col": c["name"],
                        "to_table": candidate,
                        "to_col": "id",
                    })

    return {"tables": tables, "relationships": relationships}


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
    active_tunnels = sum(len(v) for v in _tunnels.values())
    return {
        "status": "ok",
        "users": len(_connections),
        "active_tunnels": active_tunnels,
        "ssh_available": _SSH_AVAILABLE,
        "firebase_enabled": _firebase_enabled(),
        "firebase_ready": _FIREBASE_INIT_ERROR is None,
        "firebase_error": _FIREBASE_INIT_ERROR,
    }


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


@app.get("/firebase-config.js")
async def serve_firebase_config():
    return FileResponse("static/firebase-config.js", media_type="application/javascript")


@app.get("/manifest.json")
async def serve_manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def serve_sw():
    # Service workers must be served from root scope with correct content-type
    return FileResponse("static/sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})


@app.get("/icons/{filename}")
async def serve_icon(filename: str):
    path = Path(f"static/icons/{filename}")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Icon not found")
    suffix = path.suffix.lower()
    media = {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml",
             "webp": "image/webp"}.get(suffix.lstrip("."), "application/octet-stream")
    return FileResponse(str(path), media_type=media, headers={"Cache-Control": "public, max-age=86400"})
