# Self-Healing SQL Agent

A natural language SQL interface powered by Claude. Ask questions in plain English — if the generated SQL fails, the agent automatically fixes it and retries.

---

## How It Works

```
User question
     │
     ▼
Claude generates SQL
     │
     ▼
Execute against SQLite
     │
   ┌─┴──────────────────┐
   │ Error?              │
   │ Yes → feed error    │
   │       back to Claude│
   │       (up to 3x)    │
   └─────────────────────┘
     │ Success
     ▼
Return results
```

Each healing attempt feeds the **original question + failing SQL + error message** back to Claude so it can self-correct.

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- An Anthropic API key → [console.anthropic.com](https://console.anthropic.com)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set your API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### 4. Run the web app

```bash
uvicorn server:app --reload
```

Open **http://localhost:8000** in your browser.

### 5. (Optional) Run the CLI instead

```bash
python main.py
```

---

## Project Structure

```
self-healing-sql-agent/
├── server.py          # FastAPI web server (WebSocket + REST API)
├── main.py            # Interactive CLI
├── agent.py           # Core self-healing loop (used by CLI)
├── database.py        # SQLite helpers (used by CLI)
├── requirements.txt   # Python dependencies
├── static/
│   └── index.html     # Single-page frontend (all CSS + JS inline)
├── data.db            # Web app database (auto-created)
└── sample.db          # CLI database (auto-created)
```

---

## Web UI Features

### Natural Language Queries
Type any question about your data. The agent streams the SQL generation live and shows each healing attempt in real time.

### Raw SQL Mode
Switch to the **Raw SQL** tab to run queries directly without AI — useful for verifying data or running custom queries.

### Schema Explorer (sidebar)
- Expand any table to see its columns and types
- Click a table name to preview the first 10 rows
- Delete tables with the trash icon

### CSV Upload
Drag and drop a `.csv` file onto the sidebar upload zone to create a new table instantly. Column types are inferred automatically.

### Query History
Your last 10 questions are saved in the browser (localStorage) and shown in the sidebar.

---

## REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/schema` | List all tables with columns and row counts |
| `POST` | `/api/execute` | Run raw SQL → `{columns, rows}` |
| `POST` | `/api/upload` | Upload a CSV file to create a table |
| `DELETE` | `/api/table/{name}` | Drop a table |
| `WS` | `/ws/query` | Streaming self-healing query |

### WebSocket Event Protocol

**Client sends:**
```json
{ "question": "Which department has the highest average salary?" }
```

**Server streams (in order):**
```json
{ "type": "attempt_start", "attempt": 1, "max_retries": 3, "healing": false }
{ "type": "sql_chunk", "text": "SELECT " }
{ "type": "sql_chunk", "text": "d.name, AVG(e.salary)..." }
{ "type": "sql_complete", "sql": "SELECT d.name, AVG(e.salary) as avg FROM ..." }
{ "type": "executing" }
{ "type": "result", "columns": ["name","avg"], "rows": [...], "attempts": 1 }
```

If SQL fails and healing kicks in:
```json
{ "type": "sql_error", "message": "no such table: employes", "attempt": 1 }
{ "type": "attempt_start", "attempt": 2, "max_retries": 3, "healing": true }
...
{ "type": "result", ..., "attempts": 2 }
```

On total failure (all retries exhausted):
```json
{ "type": "failed", "message": "Failed after 3 attempts. Last error: ..." }
```

### POST /api/execute

```bash
curl -X POST http://localhost:8000/api/execute \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT name, salary FROM employees ORDER BY salary DESC"}'
```

### POST /api/upload

```bash
curl -X POST http://localhost:8000/api/upload \
  -F "file=@mydata.csv"
```

---

## Demo Data

The app seeds a sample business database on startup:

**departments** (4 rows) — Engineering, Sales, Marketing, HR  
**employees** (8 rows) — names, salaries, hire dates, roles  
**sales** (8 rows) — amounts, dates, products per employee

### Example questions to try

- *Show all employees with their department names and salaries*
- *Which department has the highest average salary?*
- *What is the total sales revenue per employee?*
- *List employees hired after 2021, sorted by salary descending*
- *What is the monthly sales total for Q1 2024?*
- *Who are the top 3 salespeople by total revenue?*

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | **Required.** Your Anthropic API key |
| `DB_PATH` (in server.py) | `data.db` | SQLite database file path |
| `MAX_RETRIES` (in server.py) | `3` | Max healing attempts per query |
| `MODEL` (in server.py) | `claude-opus-4-6` | Claude model to use |

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| AI | Claude (`claude-opus-4-6`) via Anthropic Python SDK |
| Backend | FastAPI + Uvicorn |
| Database | SQLite (built into Python) |
| Frontend | Vanilla HTML/CSS/JS + highlight.js |
| Streaming | WebSockets (FastAPI + asyncio) |
