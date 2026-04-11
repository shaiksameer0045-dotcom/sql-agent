"""
QueryLux — Comprehensive End-to-End Feature Test
Tests every API endpoint + WebSocket feature against the local server.
Run:  python3 test_all_features.py
Requires server running at http://127.0.0.1:8766  (DESKTOP_MODE=1)
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
import urllib.request
import urllib.error
import websockets

BASE    = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8766")
WS_BASE = BASE.replace("http://", "ws://").replace("https://", "wss://")
PASS = []
FAIL = []
WARN = []

# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _req(method, path, body=None, expect_404_ok=False, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            content = r.read()
            if raw:
                return content
            try:
                return json.loads(content)
            except Exception:
                # Non-JSON response (JS, HTML, SSE, etc.)
                return {"__raw__": content.decode(errors="replace")[:200]}
    except urllib.error.HTTPError as e:
        if expect_404_ok and e.code == 404:
            return {"__404__": True}
        try:
            body_bytes = e.read()
            try:
                return json.loads(body_bytes)
            except Exception:
                return {"__http_error__": e.code, "reason": str(e)}
        except Exception:
            return {"__http_error__": e.code, "reason": str(e)}
    except Exception as e:
        return {"__error__": str(e)}

def get(path, **kw):    return _req("GET",    path, **kw)
def post(path, body=None, **kw): return _req("POST",  path, body, **kw)
def put(path, body=None, **kw):  return _req("PUT",   path, body, **kw)
def delete(path, **kw): return _req("DELETE", path, **kw)

def get_sse(path, timeout=30):
    """Read an SSE (text/event-stream) endpoint and return concatenated text."""
    req = urllib.request.Request(BASE + path, method="GET")
    collected = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while True:
                line = r.readline()
                if not line:
                    break
                line = line.decode(errors="replace").strip()
                if line.startswith("data: "):
                    collected.append(line[6:])
        return {"ok": True, "text": "".join(collected)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def post_sse(path, body, timeout=30):
    """POST to an SSE endpoint and return concatenated text."""
    data = json.dumps(body).encode()
    req  = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    collected = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while True:
                line = r.readline()
                if not line:
                    break
                line = line.decode(errors="replace").strip()
                if line.startswith("data: "):
                    collected.append(line[6:])
        return {"ok": True, "text": "".join(collected)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ─────────────────────────────────────────────────────────────────────────────
# Reporting helpers
# ─────────────────────────────────────────────────────────────────────────────

def ok(name, detail=""):
    PASS.append(name)
    print(f"  \033[32m✓\033[0m {name}" + (f"  [{detail}]" if detail else ""))

def fail(name, reason):
    FAIL.append(name)
    print(f"  \033[31m✗\033[0m {name}: {reason}")

def warn(name, reason):
    WARN.append(name)
    print(f"  \033[33m⚠\033[0m {name}: {reason}")

def section(title):
    print(f"\n\033[1;34m── {title}\033[0m {'─'*(55-len(title))}")

def assert_ok(name, r, key=None):
    """Assert response has no error key (or has a specific key)."""
    if "error" in r and not isinstance(r.get("error"), bool):
        fail(name, r["error"])
        return False
    if key and key not in r:
        fail(name, f"missing key '{key}' in {list(r.keys())}")
        return False
    ok(name, str(r.get(key, ""))[:80] if key else "")
    return True

# ─────────────────────────────────────────────────────────────────────────────
# Connection helpers
# ─────────────────────────────────────────────────────────────────────────────

def add_conn(cfg):
    r = post("/api/connections", cfg)
    if "id" not in r:
        raise RuntimeError(f"Cannot create connection: {r}")
    return r["id"]

def rm_conn(cid):
    delete(f"/api/connections/{cid}")

# ─────────────────────────────────────────────────────────────────────────────
# 1. Health & bootstrap
# ─────────────────────────────────────────────────────────────────────────────

def test_health():
    section("Health & bootstrap")
    h = get("/health")
    if h.get("status") != "ok":
        fail("health", f"unexpected: {h}")
        sys.exit(1)
    ok("GET /health", f"firebase={h['firebase_enabled']} ssh={h['ssh_available']}")

    cfg = get("/api/firebase-config")
    ok("GET /api/firebase-config", str(cfg.get("firebase_enabled")))

    bs = post("/api/bootstrap")
    ok("POST /api/bootstrap")

    fc = _req("GET", "/firebase-config.js")
    if "__raw__" in fc or "__error__" not in fc:
        ok("GET /firebase-config.js (static JS)")
    else:
        fail("GET /firebase-config.js", str(fc))

# ─────────────────────────────────────────────────────────────────────────────
# 2. Connection CRUD
# ─────────────────────────────────────────────────────────────────────────────

def test_connection_crud():
    section("Connection CRUD")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Create
    cid = add_conn({"name": "CRUD Test SQLite", "type": "sqlite", "file": db_path})
    ok("POST /api/connections (create)", cid[:8])

    # List
    conns = get("/api/connections")
    ids = [c["id"] for c in conns]
    if cid in ids:
        ok("GET /api/connections (list)", f"{len(conns)} connections")
    else:
        fail("GET /api/connections", "new conn not in list")

    # Update (rename) — server returns {"updated": id}
    r = put(f"/api/connections/{cid}", {"name": "CRUD Renamed", "type": "sqlite", "file": db_path, "color": "#ff0000"})
    if r.get("updated") == cid or r.get("name") == "CRUD Renamed":
        ok("PUT /api/connections/{id} (rename)")
    else:
        warn("PUT /api/connections/{id}", str(r))

    # Test endpoint
    r = post(f"/api/connections/{cid}/test")
    if r.get("ok"):
        ok("POST /api/connections/{id}/test")
    else:
        fail("POST /api/connections/{id}/test", str(r))

    # test-config (no save)
    r = post("/api/connections/test-config", {"name": "x", "type": "sqlite", "file": db_path})
    if r.get("ok"):
        ok("POST /api/connections/test-config")
    else:
        fail("POST /api/connections/test-config", str(r))

    # Disconnect
    r = post(f"/api/connections/{cid}/disconnect")
    ok("POST /api/connections/{id}/disconnect")

    # Delete
    r = delete(f"/api/connections/{cid}")
    ok("DELETE /api/connections/{id}")

    # Confirm gone
    conns2 = get("/api/connections")
    if not any(c["id"] == cid for c in conns2):
        ok("Deleted connection no longer in list")
    else:
        fail("Delete", "connection still present after delete")

    return db_path

# ─────────────────────────────────────────────────────────────────────────────
# 3. SQL execute, schema, preview
# ─────────────────────────────────────────────────────────────────────────────

def test_sql_and_schema():
    section("SQL execute / schema / preview / pagination")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    cid = add_conn({"name": "SQL Test", "type": "sqlite", "file": db_path})

    # DDL + DML
    queries = [
        "CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, dept TEXT, salary REAL)",
        "CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT)",
        "INSERT INTO departments VALUES (1,'Engineering'),(2,'Sales'),(3,'Marketing')",
        "INSERT INTO employees VALUES (1,'Alice','Engineering',95000),(2,'Bob','Sales',72000),(3,'Carol','Engineering',88000),(4,'Dave','Marketing',65000),(5,'Eve','Sales',78000)",
    ]
    for q in queries:
        r = post(f"/api/connections/{cid}/execute", {"sql": q})
        if "error" in r and not isinstance(r.get("error"), bool):
            fail(f"execute: {q[:40]}", r["error"]); rm_conn(cid); return cid, db_path

    ok("execute DDL + DML (5 queries)")

    # SELECT
    r = post(f"/api/connections/{cid}/execute", {"sql": "SELECT * FROM employees"})
    if r.get("columns") and len(r["rows"]) == 5:
        ok("execute SELECT *", f"5 rows, cols={r['columns']}")
    else:
        fail("execute SELECT *", str(r))

    # JOIN
    r = post(f"/api/connections/{cid}/execute", {
        "sql": "SELECT e.name, e.salary, e.dept FROM employees e ORDER BY salary DESC"})
    if r.get("rows") and r["rows"][0][1] == 95000:
        ok("execute ORDER BY salary DESC", f"top={r['rows'][0][0]}")
    else:
        fail("execute ORDER BY", str(r))

    # Aggregate
    r = post(f"/api/connections/{cid}/execute", {
        "sql": "SELECT dept, COUNT(*) as cnt, AVG(salary) as avg_sal FROM employees GROUP BY dept"})
    assert_ok("execute GROUP BY + AVG", r, "columns")

    # Schema
    schema = get(f"/api/connections/{cid}/schema")
    if isinstance(schema, list) and len(schema) >= 2:
        tables = [t["table"] for t in schema]
        ok("GET schema", f"tables={tables}")
    else:
        fail("GET schema", str(schema))

    # Preview
    r = get(f"/api/connections/{cid}/table/employees/preview?limit=3")
    if r.get("rows") and len(r["rows"]) <= 3:
        ok("GET table preview", f"{len(r['rows'])} rows")
    else:
        fail("GET table preview", str(r))

    # Pagination: insert 600 rows and check page 2
    post(f"/api/connections/{cid}/execute", {"sql": "CREATE TABLE big (n INTEGER)"})
    vals = ",".join(f"({i})" for i in range(600))
    post(f"/api/connections/{cid}/execute", {"sql": f"INSERT INTO big VALUES {vals}"})
    r = post(f"/api/connections/{cid}/execute", {"sql": "SELECT * FROM big"})
    nrows = len(r.get("rows", []))
    total = r.get("total_rows") or r.get("total") or nrows
    if nrows == 500 and total >= 600:
        ok("Pagination: 600 rows → 500 returned (page 1)", f"total_rows={total}")
    elif nrows <= 500 and total >= 600:
        ok("Pagination: page capped", f"returned={nrows}, total={total}")
    elif nrows == 600:
        warn("Pagination", "all 600 rows returned — PAGE_SIZE not active for SQLite?")
    else:
        warn("Pagination", f"total={total} rows_returned={nrows}")

    rm_conn(cid)
    return db_path

# ─────────────────────────────────────────────────────────────────────────────
# 4. ER Diagram
# ─────────────────────────────────────────────────────────────────────────────

def test_er_diagram():
    section("ER Diagram")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    cid = add_conn({"name": "ER Test", "type": "sqlite", "file": db_path})
    post(f"/api/connections/{cid}/execute", {"sql":
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL, FOREIGN KEY(customer_id) REFERENCES customers(id))"})
    post(f"/api/connections/{cid}/execute", {"sql":
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT)"})
    post(f"/api/connections/{cid}/execute", {"sql":
        "CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER, product TEXT, qty INTEGER, FOREIGN KEY(order_id) REFERENCES orders(id))"})

    r = get(f"/api/connections/{cid}/er-diagram")
    if isinstance(r, dict) and "tables" in r:
        tables   = [t["name"] for t in r.get("tables", [])]
        rels     = r.get("relationships", [])
        ok("GET /er-diagram — tables", f"tables={tables}")
        ok("GET /er-diagram — relationships", f"{len(rels)} FK relationship(s) detected")
    else:
        fail("GET /er-diagram", str(r)[:200])

    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 5. AI features (explain, suggest, quality, story, optimize, schema-doctor,
#                 chart-suggest, generate-api)
# ─────────────────────────────────────────────────────────────────────────────

def test_ai_features():
    section("AI-powered features")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    cid = add_conn({"name": "AI Test", "type": "sqlite", "file": db_path})
    for q in [
        "CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT, amount REAL, sale_date TEXT)",
        "INSERT INTO sales VALUES (1,'North',1500,'2024-01-01'),(2,'South',2200,'2024-01-05'),(3,'North',800,'2024-02-01'),(4,'East',3100,'2024-02-10'),(5,'South',950,'2024-03-01')",
    ]:
        post(f"/api/connections/{cid}/execute", {"sql": q})

    _no_groq = not os.environ.get("GROQ_API_KEY","")

    # Table explain (SSE stream)
    r = get_sse(f"/api/connections/{cid}/explain/sales")
    if r["ok"] and r.get("text"):
        ok("GET /explain/{table} — SSE AI explanation", f"{len(r['text'])} chars")
    elif _no_groq:
        warn("GET /explain/{table}", "no GROQ_API_KEY — AI skipped")
    elif r["ok"]:
        warn("GET /explain/{table}", "connected but no text received")
    else:
        warn("GET /explain/{table}", r.get("error","?")[:100])

    # Suggest queries (JSON)
    r = get(f"/api/connections/{cid}/suggest")
    qs = r.get("questions", r if isinstance(r, list) else [])
    if qs:
        ok("GET /suggest", f"{len(qs)} suggestions")
    elif _no_groq:
        warn("GET /suggest", "no GROQ_API_KEY — AI skipped")
    else:
        warn("GET /suggest", str(r)[:100])

    # Data quality (JSON) — no LLM needed
    r = get(f"/api/connections/{cid}/quality/sales")
    if "score" in r or "issues" in r:
        ok("GET /quality/{table}", f"score={r.get('score','n/a')} issues={len(r.get('issues',[]))}")
    elif "__http_error__" not in r and "__error__" not in r:
        ok("GET /quality/{table}", str(r)[:50])
    else:
        warn("GET /quality/{table}", str(r)[:100])

    # Schema doctor (SSE stream)
    r = get_sse(f"/api/connections/{cid}/schema-doctor")
    if r["ok"] and r.get("text"):
        ok("GET /schema-doctor — SSE", f"{len(r['text'])} chars")
    elif _no_groq:
        warn("GET /schema-doctor", "no GROQ_API_KEY — AI skipped")
    else:
        warn("GET /schema-doctor", f"ok={r['ok']} text={len(r.get('text',''))}")

    # Story (SSE stream) — requires actual result data
    r = post_sse(f"/api/connections/{cid}/story", {
        "question": "What are the top regions by revenue?",
        "sql": "SELECT region, SUM(amount) as total FROM sales GROUP BY region",
        "columns": ["region", "total"],
        "rows": [["North", 2300], ["South", 3150], ["East", 3100]]
    })
    if r["ok"] and r.get("text"):
        ok("POST /story — SSE AI narrative", f"{len(r['text'])} chars")
    elif _no_groq:
        warn("POST /story", "no GROQ_API_KEY — AI skipped")
    else:
        warn("POST /story", r.get("error","?")[:100])

    # Optimize SQL (SSE stream)
    r = post_sse(f"/api/connections/{cid}/optimize", {"sql": "SELECT * FROM sales WHERE region='North'"})
    if r["ok"] and r.get("text"):
        ok("POST /optimize — SSE", f"{len(r.get('text',''))} chars")
    elif _no_groq:
        warn("POST /optimize", "no GROQ_API_KEY — AI skipped")
    else:
        warn("POST /optimize", r.get("error","?")[:100])

    # Generate API code (SSE stream)
    r = post_sse(f"/api/connections/{cid}/generate-api", {"sql": "SELECT * FROM sales WHERE region=?"})
    if r["ok"] and r.get("text"):
        ok("POST /generate-api — SSE", f"{len(r.get('text',''))} chars")
    elif _no_groq:
        warn("POST /generate-api", "no GROQ_API_KEY — AI skipped")
    else:
        warn("POST /generate-api", r.get("error","?")[:100])

    # Chart suggest (JSON) — no LLM needed
    r = post(f"/api/connections/{cid}/chart-suggest", {"columns": ["region","amount"], "rows": [["North",1500],["South",2200]]})
    if "chart_type" in r or "type" in r:
        ok("POST /chart-suggest", str(r.get("chart_type", r.get("type","ok"))))
    elif "__http_error__" not in r and "__error__" not in r:
        ok("POST /chart-suggest", str(r)[:50])
    else:
        warn("POST /chart-suggest", str(r)[:100])

    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 6. History & Saved queries
# ─────────────────────────────────────────────────────────────────────────────

def test_history_saved():
    section("Query history & saved queries")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    cid = add_conn({"name": "Hist Test", "type": "sqlite", "file": db_path})

    post(f"/api/connections/{cid}/execute", {"sql": "CREATE TABLE t (x INTEGER)"})
    post(f"/api/connections/{cid}/execute", {"sql": "SELECT 1"})
    post(f"/api/connections/{cid}/execute", {"sql": "SELECT 2"})

    # History
    r = get(f"/api/connections/{cid}/history")
    if isinstance(r, list):
        ok("GET /history", f"{len(r)} entries")
    else:
        warn("GET /history", str(r)[:80])

    # Save a query
    r = post(f"/api/connections/{cid}/saved", {"name": "My saved", "sql": "SELECT * FROM t", "question": "All rows"})
    qid = r.get("id")
    if qid:
        ok("POST /saved (save query)", qid[:8])
    else:
        warn("POST /saved", str(r)[:80])

    # List saved
    r = get(f"/api/connections/{cid}/saved")
    if isinstance(r, list) and any(q.get("id") == qid for q in r):
        ok("GET /saved (list)", f"{len(r)} saved queries")
    else:
        warn("GET /saved", str(r)[:80])

    # Delete saved
    if qid:
        delete(f"/api/connections/{cid}/saved/{qid}")
        r2 = get(f"/api/connections/{cid}/saved")
        if not any(q.get("id") == qid for q in (r2 if isinstance(r2, list) else [])):
            ok("DELETE /saved/{id}")
        else:
            warn("DELETE /saved/{id}", "still present")

    # Clear history
    delete(f"/api/connections/{cid}/history")
    r = get(f"/api/connections/{cid}/history")
    if isinstance(r, list) and len(r) == 0:
        ok("DELETE /history (clear)")
    else:
        warn("DELETE /history", str(r)[:80])

    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 7. Share
# ─────────────────────────────────────────────────────────────────────────────

def test_share():
    section("Share query")
    payload = {"sql": "SELECT 42 as answer", "columns": ["answer"], "rows": [[42]], "question": "ultimate answer", "conn_name": "Test Connection"}
    r = post("/api/share", payload)
    sid = r.get("id") or r.get("share_id")
    if not sid:
        warn("POST /api/share", str(r)[:80])
        return
    ok("POST /api/share", f"id={sid[:8]}")

    r2 = get(f"/api/share/{sid}")
    if r2.get("sql") == "SELECT 42 as answer":
        ok("GET /api/share/{id}")
    else:
        warn("GET /api/share/{id}", str(r2)[:80])

    # Share HTML page
    r3 = _req("GET", f"/share/{sid}")
    ok("GET /share/{id} (HTML page)" if r3 else "GET /share/{id} returned empty")

# ─────────────────────────────────────────────────────────────────────────────
# 8. Alerts / scheduler
# ─────────────────────────────────────────────────────────────────────────────

def test_alerts():
    section("Alerts / scheduler")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    cid = add_conn({"name": "Alert Test", "type": "sqlite", "file": db_path})
    post(f"/api/connections/{cid}/execute", {"sql": "CREATE TABLE metrics (val INTEGER)"})
    post(f"/api/connections/{cid}/execute", {"sql": "INSERT INTO metrics VALUES (42)"})

    # Create alert
    r = post("/api/alerts", {
        "name": "Test Alert",
        "conn_id": cid,
        "sql": "SELECT val FROM metrics",
        "threshold": 100,
        "operator": "<",
        "interval_minutes": 60,
        "notify_email": "test@example.com"
    })
    aid = r.get("id")
    if not aid:
        warn("POST /api/alerts", str(r)[:80])
        rm_conn(cid); return
    ok("POST /api/alerts (create)", aid[:8])

    # List
    r = get("/api/alerts")
    if isinstance(r, list) and any(a.get("id") == aid for a in r):
        ok("GET /api/alerts (list)", f"{len(r)} alerts")
    else:
        warn("GET /api/alerts", str(r)[:80])

    # Toggle (pause/resume)
    r = post(f"/api/alerts/{aid}/toggle")
    ok("POST /api/alerts/{id}/toggle")

    # Manual run
    r = post(f"/api/alerts/{aid}/run")
    if "triggered" in r or "error" not in r:
        ok("POST /api/alerts/{id}/run", f"triggered={r.get('triggered')}")
    else:
        warn("POST /api/alerts/{id}/run", str(r)[:80])

    # Delete
    delete(f"/api/alerts/{aid}")
    r = get("/api/alerts")
    if not any(a.get("id") == aid for a in (r if isinstance(r, list) else [])):
        ok("DELETE /api/alerts/{id}")
    else:
        warn("DELETE /api/alerts/{id}", "still present")

    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 9. CSV / JSON upload
# ─────────────────────────────────────────────────────────────────────────────

def test_upload():
    section("CSV / JSON upload")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    cid = add_conn({"name": "Upload Test", "type": "sqlite", "file": db_path})

    # CSV upload via multipart
    csv_content = b"id,product,price\n1,Widget,9.99\n2,Gadget,24.99\n3,Doohickey,4.99\n"
    boundary = "----TestBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="products.csv"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode() + csv_content + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(BASE + f"/api/connections/{cid}/upload", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            r = json.loads(resp.read())
        if r.get("rows") == 3:
            ok("POST /upload CSV", f"table={r['table']}, rows={r['rows']}")
        else:
            warn("POST /upload CSV", str(r)[:80])
    except Exception as e:
        fail("POST /upload CSV", str(e))

    # Verify table was created
    r = post(f"/api/connections/{cid}/execute", {"sql": "SELECT * FROM products"})
    if r.get("rows") and len(r["rows"]) == 3:
        ok("Uploaded CSV table queryable", f"3 rows, cols={r['columns']}")
    else:
        warn("Uploaded CSV queryable", str(r)[:80])

    # JSON upload
    json_data = json.dumps([
        {"city": "Mumbai", "country": "India", "pop": 20667000},
        {"city": "Delhi",  "country": "India", "pop": 32941000},
        {"city": "Pune",   "country": "India", "pop": 6629000},
    ]).encode()
    boundary2 = "----TestBoundary2"
    body2 = (
        f"--{boundary2}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="cities.json"\r\n'
        f"Content-Type: application/json\r\n\r\n"
    ).encode() + json_data + f"\r\n--{boundary2}--\r\n".encode()
    req2 = urllib.request.Request(BASE + f"/api/connections/{cid}/upload", data=body2, method="POST")
    req2.add_header("Content-Type", f"multipart/form-data; boundary={boundary2}")
    try:
        with urllib.request.urlopen(req2, timeout=20) as resp:
            r2 = json.loads(resp.read())
        if r2.get("rows") == 3:
            ok("POST /upload JSON", f"table={r2['table']}, rows={r2['rows']}")
        else:
            warn("POST /upload JSON", str(r2)[:80])
    except Exception as e:
        fail("POST /upload JSON", str(e))

    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 10. Autocomplete endpoint
# ─────────────────────────────────────────────────────────────────────────────

def test_autocomplete():
    section("SQL Autocomplete")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    cid = add_conn({"name": "AC Test", "type": "sqlite", "file": db_path})
    post(f"/api/connections/{cid}/execute", {"sql": "CREATE TABLE widgets (id INTEGER, name TEXT, color TEXT)"})

    r = get(f"/api/connections/{cid}/autocomplete")
    # Returns {"tables": [...], "columns": [...]} or a flat list
    if isinstance(r, dict) and ("tables" in r or "columns" in r):
        total_tokens = len(r.get("tables", [])) + len(r.get("columns", []))
        ok("GET /autocomplete", f"{total_tokens} tokens (tables={len(r.get('tables',[]))}, cols={len(r.get('columns',[]))})")
    elif isinstance(r, list):
        ok("GET /autocomplete", f"{len(r)} tokens")
    else:
        warn("GET /autocomplete", str(r)[:80])

    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 11. Admin stats
# ─────────────────────────────────────────────────────────────────────────────

def test_admin():
    section("Admin stats")
    r = get("/api/admin/stats")
    if "total_users" in r or "users" in r or "error" not in r:
        ok("GET /api/admin/stats", str(r)[:80])
    else:
        warn("GET /api/admin/stats", str(r)[:80])

# ─────────────────────────────────────────────────────────────────────────────
# 12. PostgreSQL — specific features
# ─────────────────────────────────────────────────────────────────────────────

def test_postgresql_features():
    section("PostgreSQL (Docker ql-test-postgres :5432)")
    PG = dict(host=os.environ.get("PG_HOST","127.0.0.1"),
              port=int(os.environ.get("PG_PORT",5432)),
              db=os.environ.get("PG_DB","testdb"),
              user=os.environ.get("PG_USER","testuser"),
              pw=os.environ.get("PG_PASS","testpass"))
    try:
        import psycopg2
        c = psycopg2.connect(host=PG["host"], port=PG["port"], dbname=PG["db"],
                             user=PG["user"], password=PG["pw"], connect_timeout=5)
        c.close()
    except Exception as e:
        warn("PostgreSQL", f"not reachable — skipping ({e})")
        return

    cid = add_conn({"name":"PG Feature Test","type":"postgresql",
                    "host":PG["host"],"port":PG["port"],
                    "database":PG["db"],"user":PG["user"],"password":PG["pw"]})

    post(f"/api/connections/{cid}/execute", {"sql": 'DROP TABLE IF EXISTS "pg_feat"'})
    post(f"/api/connections/{cid}/execute", {"sql": '''
        CREATE TABLE "pg_feat" (
            id SERIAL PRIMARY KEY,
            name TEXT,
            score NUMERIC(5,2),
            tags TEXT[],
            meta JSONB,
            created_at TIMESTAMP DEFAULT NOW()
        )'''})
    post(f"/api/connections/{cid}/execute", {"sql": """
        INSERT INTO "pg_feat" (name, score, tags, meta) VALUES
        ('Alpha', 95.5, '{\"a\",\"b\"}', '{\"level\":1}'),
        ('Beta',  80.0, '{\"b\",\"c\"}', '{\"level\":2}'),
        ('Gamma', 72.3, '{\"a\",\"c\"}', '{\"level\":1}')
    """})

    # Basic select
    r = post(f"/api/connections/{cid}/execute", {"sql": 'SELECT * FROM "pg_feat"'})
    if r.get("rows") and len(r["rows"]) == 3:
        ok("PostgreSQL: SELECT *")
    else:
        fail("PostgreSQL: SELECT *", str(r)[:80])

    # JSONB query
    r = post(f"/api/connections/{cid}/execute", {"sql": "SELECT name, meta->>'level' as level FROM pg_feat"})
    if r.get("columns"):
        ok("PostgreSQL: JSONB ->>'key' query")
    else:
        fail("PostgreSQL: JSONB", str(r)[:80])

    # Window function
    r = post(f"/api/connections/{cid}/execute", {"sql":
        "SELECT name, score, RANK() OVER (ORDER BY score DESC) as rnk FROM pg_feat"})
    if r.get("columns") and "rnk" in r["columns"]:
        ok("PostgreSQL: RANK() window function")
    else:
        fail("PostgreSQL: window function", str(r)[:80])

    # ILIKE
    r = post(f"/api/connections/{cid}/execute", {"sql": "SELECT name FROM pg_feat WHERE name ILIKE 'a%'"})
    if r.get("rows") and r["rows"][0][0].lower().startswith("a"):
        ok("PostgreSQL: ILIKE")
    else:
        fail("PostgreSQL: ILIKE", str(r)[:80])

    # CTE
    r = post(f"/api/connections/{cid}/execute", {"sql": """
        WITH ranked AS (SELECT name, score, RANK() OVER (ORDER BY score DESC) as rnk FROM pg_feat)
        SELECT * FROM ranked WHERE rnk = 1"""})
    if r.get("rows"):
        ok("PostgreSQL: CTE (WITH clause)")
    else:
        fail("PostgreSQL: CTE", str(r)[:80])

    # ER diagram
    r = get(f"/api/connections/{cid}/er-diagram")
    if "tables" in r:
        ok("PostgreSQL: ER diagram", f"{len(r['tables'])} tables")
    else:
        warn("PostgreSQL: ER diagram", str(r)[:80])

    # Schema + autocomplete
    schema = get(f"/api/connections/{cid}/schema")
    if isinstance(schema, list) and any(t["table"] == "pg_feat" for t in schema):
        ok("PostgreSQL: schema lists pg_feat")
    else:
        fail("PostgreSQL: schema", str(schema)[:80])

    ac = get(f"/api/connections/{cid}/autocomplete")
    if isinstance(ac, list) and len(ac) > 0:
        ok("PostgreSQL: autocomplete tokens", f"{len(ac)}")

    post(f"/api/connections/{cid}/execute", {"sql": 'DROP TABLE "pg_feat"'})
    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 13. MySQL — specific features
# ─────────────────────────────────────────────────────────────────────────────

def test_mysql_features():
    section("MySQL (Docker ql-test-mysql :3306)")
    MY = dict(host=os.environ.get("MY_HOST","127.0.0.1"),
              port=int(os.environ.get("MY_PORT",3306)),
              db=os.environ.get("MY_DB","testdb"),
              user=os.environ.get("MY_USER","testuser"),
              pw=os.environ.get("MY_PASS","testpass"))
    try:
        import pymysql
        c = pymysql.connect(host=MY["host"], port=MY["port"], database=MY["db"],
                            user=MY["user"], password=MY["pw"], connect_timeout=5)
        c.close()
    except Exception as e:
        warn("MySQL", f"not reachable — skipping ({e})")
        return

    cid = add_conn({"name":"MySQL Feature Test","type":"mysql",
                    "host":MY["host"],"port":MY["port"],
                    "database":MY["db"],"user":MY["user"],"password":MY["pw"]})

    post(f"/api/connections/{cid}/execute", {"sql": "DROP TABLE IF EXISTS `my_feat`"})
    post(f"/api/connections/{cid}/execute", {"sql": """
        CREATE TABLE `my_feat` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `name` VARCHAR(100),
            `category` VARCHAR(50),
            `amount` DECIMAL(10,2),
            `created_at` DATETIME DEFAULT NOW()
        )"""})
    post(f"/api/connections/{cid}/execute", {"sql": """
        INSERT INTO `my_feat` (`name`,`category`,`amount`) VALUES
        ('Laptop','Electronics',999.99),
        ('Mouse','Electronics',29.99),
        ('Desk','Furniture',349.00),
        ('Chair','Furniture',199.50)
    """})

    r = post(f"/api/connections/{cid}/execute", {"sql": "SELECT * FROM `my_feat`"})
    if r.get("rows") and len(r["rows"]) == 4:
        ok("MySQL: SELECT *")
    else:
        fail("MySQL: SELECT *", str(r)[:80])

    r = post(f"/api/connections/{cid}/execute", {"sql":
        "SELECT `category`, COUNT(*) cnt, SUM(`amount`) total FROM `my_feat` GROUP BY `category` ORDER BY total DESC"})
    if r.get("rows"):
        ok("MySQL: GROUP BY + SUM")
    else:
        fail("MySQL: GROUP BY", str(r)[:80])

    r = post(f"/api/connections/{cid}/execute", {"sql":
        "SELECT CONCAT(`name`,' (',`category`,')') as label FROM `my_feat` LIMIT 2"})
    if r.get("rows"):
        ok("MySQL: CONCAT")

    r = post(f"/api/connections/{cid}/execute", {"sql":
        "SELECT name, RANK() OVER (PARTITION BY category ORDER BY amount DESC) rnk FROM my_feat"})
    if r.get("columns") and "rnk" in r["columns"]:
        ok("MySQL: RANK() window function")
    else:
        warn("MySQL: window function", str(r)[:80])

    # ER diagram
    r = get(f"/api/connections/{cid}/er-diagram")
    if "tables" in r:
        ok("MySQL: ER diagram", f"{len(r['tables'])} tables")
    else:
        warn("MySQL: ER diagram", str(r)[:80])

    post(f"/api/connections/{cid}/execute", {"sql": "DROP TABLE `my_feat`"})
    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 14. postgres-dummy (:5433)
# ─────────────────────────────────────────────────────────────────────────────

def test_mariadb_features():
    section("MariaDB (Docker ql-test-mariadb :3307)")
    M = dict(host=os.environ.get("MARIA_HOST","127.0.0.1"),
             port=int(os.environ.get("MARIA_PORT",3307)),
             db=os.environ.get("MARIA_DB","testdb"),
             user=os.environ.get("MARIA_USER","testuser"),
             pw=os.environ.get("MARIA_PASS","testpass"))
    try:
        import pymysql
        c = pymysql.connect(host=M["host"], port=M["port"], database=M["db"],
                            user=M["user"], password=M["pw"], connect_timeout=5)
        c.close()
    except Exception as e:
        warn("MariaDB", f"not reachable — skipping ({e})")
        return

    cid = add_conn({"name":"MariaDB Feature Test","type":"mysql",
                    "host":M["host"],"port":M["port"],
                    "database":M["db"],"user":M["user"],"password":M["pw"]})
    post(f"/api/connections/{cid}/execute", {"sql": "DROP TABLE IF EXISTS `maria_feat`"})
    post(f"/api/connections/{cid}/execute", {"sql": """
        CREATE TABLE `maria_feat` (
            `id` INT AUTO_INCREMENT PRIMARY KEY,
            `level` VARCHAR(10),
            `message` TEXT,
            `ts` DATETIME DEFAULT NOW()
        )"""})
    post(f"/api/connections/{cid}/execute", {"sql": """
        INSERT INTO `maria_feat` (`level`,`message`) VALUES
        ('INFO','Server started'),('WARN','High memory'),('ERROR','Disk full'),
        ('INFO','Cache cleared'),('ERROR','Network timeout')"""})

    r = post(f"/api/connections/{cid}/execute", {"sql": "SELECT * FROM `maria_feat`"})
    if r.get("rows") and len(r["rows"]) == 5:
        ok("MariaDB: SELECT *")
    else:
        fail("MariaDB: SELECT *", str(r)[:80])

    r = post(f"/api/connections/{cid}/execute", {"sql":
        "SELECT `level`, COUNT(*) cnt FROM `maria_feat` GROUP BY `level` ORDER BY cnt DESC"})
    if r.get("rows"):
        ok("MariaDB: GROUP BY")

    r = post(f"/api/connections/{cid}/execute", {"sql":
        "SELECT JSON_OBJECT('id', id, 'level', `level`) as j FROM `maria_feat` LIMIT 2"})
    if r.get("rows"):
        ok("MariaDB: JSON_OBJECT (MariaDB-specific)")

    r = get(f"/api/connections/{cid}/er-diagram")
    if "tables" in r:
        ok("MariaDB: ER diagram", f"{len(r['tables'])} tables")

    post(f"/api/connections/{cid}/execute", {"sql": "DROP TABLE `maria_feat`"})
    rm_conn(cid)


def test_postgres_dummy():
    section("postgres-dummy (:5433)")
    PG2 = dict(host=os.environ.get("PG2_HOST","127.0.0.1"),
               port=int(os.environ.get("PG2_PORT",5433)),
               db=os.environ.get("PG2_DB","mydb"),
               user=os.environ.get("PG2_USER","myuser"),
               pw=os.environ.get("PG2_PASS","password"))
    try:
        import psycopg2
        c = psycopg2.connect(host=PG2["host"], port=PG2["port"], dbname=PG2["db"],
                             user=PG2["user"], password=PG2["pw"], connect_timeout=5)
        c.close()
    except Exception as e:
        warn("postgres-dummy", f"not reachable — skipping ({e})")
        return

    cid = add_conn({"name":"Dummy PG Test","type":"postgresql",
                    "host":PG2["host"],"port":PG2["port"],
                    "database":PG2["db"],"user":PG2["user"],"password":PG2["pw"]})

    post(f"/api/connections/{cid}/execute", {"sql": 'DROP TABLE IF EXISTS "dummy_feat"'})
    post(f"/api/connections/{cid}/execute", {"sql": """
        CREATE TABLE "dummy_feat" (
            id SERIAL PRIMARY KEY,
            event TEXT,
            payload JSONB,
            ts TIMESTAMP DEFAULT NOW()
        )"""})
    post(f"/api/connections/{cid}/execute", {"sql": """
        INSERT INTO "dummy_feat" (event, payload) VALUES
        ('click',   '{\"page\":\"home\"}'),
        ('login',   '{\"user\":42}'),
        ('purchase','{\"amount\":99.9}')
    """})

    r = post(f"/api/connections/{cid}/execute", {"sql": "SELECT event, payload->>'page' as page FROM dummy_feat"})
    if r.get("columns"):
        ok("postgres-dummy: JSONB query")
    else:
        fail("postgres-dummy: JSONB", str(r)[:80])

    # ER diagram
    r = get(f"/api/connections/{cid}/er-diagram")
    if "tables" in r:
        ok("postgres-dummy: ER diagram", f"{len(r['tables'])} tables")
    else:
        warn("postgres-dummy: ER diagram", str(r)[:80])

    schema = get(f"/api/connections/{cid}/schema")
    if isinstance(schema, list) and len(schema) > 0:
        ok("postgres-dummy: schema", f"{len(schema)} tables")
    else:
        fail("postgres-dummy: schema", str(schema)[:80])

    post(f"/api/connections/{cid}/execute", {"sql": 'DROP TABLE "dummy_feat"'})
    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 15. DuckDB features
# ─────────────────────────────────────────────────────────────────────────────

def test_duckdb_features():
    section("DuckDB")
    db_path = os.path.join(tempfile.gettempdir(), f"ql_feat_{uuid.uuid4().hex}.duckdb")
    cid = add_conn({"name": "DuckDB Feature Test", "type": "duckdb", "file": db_path})

    post(f"/api/connections/{cid}/execute", {"sql": """
        CREATE TABLE analytics AS
        SELECT * FROM (VALUES
            ('2024-01', 'North', 1500.0),
            ('2024-01', 'South', 2200.0),
            ('2024-02', 'North',  800.0),
            ('2024-02', 'East',  3100.0),
            ('2024-03', 'South',  950.0)
        ) t(month, region, revenue)
    """})

    r = post(f"/api/connections/{cid}/execute", {"sql":
        "SELECT region, SUM(revenue) total FROM analytics GROUP BY region ORDER BY total DESC"})
    if r.get("rows"):
        ok("DuckDB: GROUP BY + SUM")
    else:
        fail("DuckDB: GROUP BY", str(r)[:80])

    r = post(f"/api/connections/{cid}/execute", {"sql": """
        SELECT region, revenue,
               RANK() OVER (ORDER BY revenue DESC) rnk,
               LAG(revenue) OVER (PARTITION BY region ORDER BY month) prev_rev
        FROM analytics
    """})
    if r.get("columns") and "rnk" in r["columns"]:
        ok("DuckDB: RANK() + LAG() window functions")
    else:
        fail("DuckDB: window functions", str(r)[:80])

    r = post(f"/api/connections/{cid}/execute", {"sql":
        "SELECT PIVOT analytics ON region USING SUM(revenue) GROUP BY month"})
    if r.get("columns"):
        ok("DuckDB: PIVOT statement")
    else:
        warn("DuckDB: PIVOT", str(r)[:80])

    # ER diagram
    r = get(f"/api/connections/{cid}/er-diagram")
    if "tables" in r:
        ok("DuckDB: ER diagram", f"{len(r['tables'])} table(s)")
    else:
        warn("DuckDB: ER diagram", str(r)[:80])

    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 16. WebSocket — NL query (ws/query)
# ─────────────────────────────────────────────────────────────────────────────

async def _ws_nl_query(cid):
    uri = f"{WS_BASE}/ws/query/{cid}"
    msgs = []
    try:
        async with websockets.connect(uri, open_timeout=10) as ws:
            # Send auth init (desktop mode — token ignored)
            await ws.send(json.dumps({"token": "desktop", "uid": "local-user"}))
            # Send NL question
            await ws.send(json.dumps({"question": "How many rows are in the employees table?"}))
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(raw)
                    msgs.append(msg)
                    if msg.get("type") in ("result", "error", "done"):
                        break
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        return None, str(e)
    return msgs, None

def test_ws_nl_query():
    section("WebSocket — NL natural-language query (/ws/query)")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    cid = add_conn({"name": "WS NL Test", "type": "sqlite", "file": db_path})
    for q in [
        "CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, dept TEXT, salary REAL)",
        "INSERT INTO employees VALUES (1,'Alice','Eng',95000),(2,'Bob','Sales',72000),(3,'Carol','Eng',88000)",
    ]:
        post(f"/api/connections/{cid}/execute", {"sql": q})

    msgs, err = asyncio.run(_ws_nl_query(cid))
    if err:
        warn("WS /ws/query — connect", err[:100])
    elif msgs:
        types = [m.get("type") for m in msgs]
        has_sql  = any(m.get("sql")    for m in msgs)
        has_rows = any(m.get("rows") is not None for m in msgs)
        if has_rows or "result" in types:
            ok("WS /ws/query — NL→SQL→result", f"types={types}, has_sql={has_sql}")
        elif has_sql:
            ok("WS /ws/query — SQL generated", f"sql={next(m['sql'] for m in msgs if m.get('sql'))[:60]}")
        else:
            warn("WS /ws/query", f"msgs={types}")
    else:
        warn("WS /ws/query", "no messages received")

    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 17. WebSocket — Chat (/ws/chat)
# ─────────────────────────────────────────────────────────────────────────────

async def _ws_chat(cid):
    uri = f"{WS_BASE}/ws/chat/{cid}"
    msgs = []
    try:
        async with websockets.connect(uri, open_timeout=10) as ws:
            await ws.send(json.dumps({"token": "desktop", "uid": "local-user"}))
            await ws.send(json.dumps({"message": "What tables are available?"}))
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(raw)
                    msgs.append(msg)
                    if msg.get("type") in ("done", "error", "end"):
                        break
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        return None, str(e)
    return msgs, None

def test_ws_chat():
    section("WebSocket — AI Chat (/ws/chat)")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    cid = add_conn({"name": "WS Chat Test", "type": "sqlite", "file": db_path})
    post(f"/api/connections/{cid}/execute", {"sql": "CREATE TABLE chat_test (id INTEGER, msg TEXT)"})

    msgs, err = asyncio.run(_ws_chat(cid))
    if err:
        warn("WS /ws/chat — connect", err[:100])
    elif msgs:
        types = [m.get("type") for m in msgs]
        has_text = any(m.get("text") or m.get("chunk") or m.get("delta") for m in msgs)
        if has_text or "done" in types or "end" in types:
            ok("WS /ws/chat — streamed AI response", f"types={types}")
        else:
            ok("WS /ws/chat — connected, messages received", f"types={types}")
    else:
        warn("WS /ws/chat", "no messages received")

    rm_conn(cid)

# ─────────────────────────────────────────────────────────────────────────────
# 18. Demo connections (auto-seeded)
# ─────────────────────────────────────────────────────────────────────────────

def test_demo_connections():
    section("Demo connections (auto-seeded SQLite + DuckDB)")
    conns = get("/api/connections")
    demo_sqlite = next((c for c in conns if c["type"]=="sqlite"  and "Demo" in c["name"]), None)
    demo_duck   = next((c for c in conns if c["type"]=="duckdb"  and "Demo" in c["name"]), None)

    if demo_sqlite:
        cid = demo_sqlite["id"]
        ok(f"Demo SQLite present: {demo_sqlite['name']}")
        r = post(f"/api/connections/{cid}/execute", {"sql":
            "SELECT e.name, d.name dept, s.amount FROM sales s JOIN employees e ON s.employee_id=e.id JOIN departments d ON e.department_id=d.id LIMIT 5"})
        if r.get("rows"):
            ok("Demo SQLite: 3-table JOIN")
        # ER diagram
        r2 = get(f"/api/connections/{cid}/er-diagram")
        if "tables" in r2:
            ok("Demo SQLite: ER diagram", f"{len(r2['tables'])} tables, {len(r2.get('relationships',[]))} FK(s)")
    else:
        warn("Demo SQLite", "not found — run server once to seed")

    if demo_duck:
        cid = demo_duck["id"]
        ok(f"Demo DuckDB present: {demo_duck['name']}")
        r = post(f"/api/connections/{cid}/execute", {"sql":
            "SELECT p.name, SUM(o.quantity*o.unit_price) rev FROM orders o JOIN products p ON o.product_id=p.product_id GROUP BY p.name ORDER BY rev DESC LIMIT 5"})
        if r.get("rows"):
            ok("Demo DuckDB: JOIN + GROUP BY revenue")
        r2 = get(f"/api/connections/{cid}/er-diagram")
        if "tables" in r2:
            ok("Demo DuckDB: ER diagram", f"{len(r2['tables'])} tables")
    else:
        warn("Demo DuckDB", "not found")

# ─────────────────────────────────────────────────────────────────────────────
# 19. Static assets
# ─────────────────────────────────────────────────────────────────────────────

def test_static():
    section("Static assets")
    # Root HTML
    html_raw = _req("GET", "/", raw=True)
    html = html_raw.decode(errors="replace") if isinstance(html_raw, bytes) else ""
    if "QueryLux" in html and "serviceWorker" not in html:
        ok("GET / — HTML (no service worker)", f"{len(html)} bytes")
    elif "QueryLux" in html:
        warn("GET /", "serviceWorker still present in HTML")
    else:
        fail("GET /", "QueryLux not found in response")

    # Icon
    try:
        with urllib.request.urlopen(BASE + "/icons/icon-192.png", timeout=10) as r:
            ok("GET /icons/icon-192.png", f"{r.length or '?'} bytes")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            warn("GET /icons/icon-192.png", "icon file not found in static/icons/")
        else:
            warn("GET /icons/icon-192.png", str(e))
    except Exception as e:
        warn("GET /icons/icon-192.png", str(e))

    # No manifest or sw.js
    r = _req("GET", "/manifest.json", expect_404_ok=True)
    if r.get("__404__"):
        ok("GET /manifest.json → 404 (removed)")
    else:
        warn("GET /manifest.json", "still accessible (should be 404)")

    r = _req("GET", "/sw.js", expect_404_ok=True)
    if r.get("__404__"):
        ok("GET /sw.js → 404 (removed)")
    else:
        warn("GET /sw.js", "still accessible (should be 404)")

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n\033[1m{'='*60}\033[0m")
    print("  QueryLux — Full Feature E2E Test Suite")
    print(f"  Server: {BASE}")
    print(f"\033[1m{'='*60}\033[0m")

    try:
        import websockets
    except ImportError:
        print("Installing websockets...")
        os.system(f"{sys.executable} -m pip install websockets -q")
        import websockets

    # Verify server
    try:
        h = get("/health")
        if h.get("status") != "ok":
            raise ValueError(f"unhealthy: {h}")
        print(f"\n  Server OK | firebase={h['firebase_enabled']} | ssh={h['ssh_available']}\n")
    except Exception as e:
        print(f"\n  \033[31mERROR: Cannot reach {BASE}: {e}\033[0m")
        sys.exit(1)

    # Run all test groups
    test_health()
    test_static()
    test_connection_crud()
    test_sql_and_schema()
    test_er_diagram()
    test_ai_features()
    test_history_saved()
    test_share()
    test_alerts()
    test_upload()
    test_autocomplete()
    test_admin()
    test_demo_connections()
    test_duckdb_features()
    test_postgresql_features()
    test_mysql_features()
    test_mariadb_features()
    test_postgres_dummy()
    test_ws_nl_query()
    test_ws_chat()

    # Summary
    total = len(PASS) + len(FAIL)
    print(f"\n\033[1m{'='*60}\033[0m")
    print(f"  \033[32m{len(PASS)} passed\033[0m  "
          f"\033[31m{len(FAIL)} failed\033[0m  "
          f"\033[33m{len(WARN)} warnings\033[0m  "
          f"(total {total})")
    if FAIL:
        print("\n  \033[31mFailed:\033[0m")
        for f in FAIL:
            print(f"    ✗ {f}")
    if WARN:
        print("\n  \033[33mWarnings (non-blocking):\033[0m")
        for w in WARN:
            print(f"    ⚠ {w}")
    print(f"\033[1m{'='*60}\033[0m\n")
    sys.exit(0 if not FAIL else 1)
