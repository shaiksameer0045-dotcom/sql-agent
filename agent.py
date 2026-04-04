import os
import re
from groq import Groq

MAX_RETRIES = 3


def _extract_sql(text: str) -> str:
    """Pull the SQL out of the response (code block or raw statement)."""
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(
        r"(SELECT|INSERT|UPDATE|DELETE|WITH)\b.*",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if match:
        return match.group(0).strip().rstrip(";")
    return text.strip()


def _call_openai(system: str, user: str) -> str:
    """Stream a Groq response and return the full text."""
    client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
    full = ""
    stream = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=1024,
        stream=True,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    for chunk in stream:
        text = chunk.choices[0].delta.content or ""
        if text:
            print(text, end="", flush=True)
            full += text
    print()
    return full


def generate_sql(schema: str, question: str) -> str:
    """Ask Claude to write SQL for the question."""
    system = f"""You are an expert SQLite assistant.
Given the schema below and a natural language question, write a single correct SQL query.
Respond with ONLY the SQL inside a ```sql code block — no explanations.

{schema}"""

    raw = _call_openai(system, f"Question: {question}")
    return _extract_sql(raw)


def fix_sql(schema: str, question: str, bad_sql: str, error: str) -> str:
    """Ask Claude to correct a failing SQL query."""
    system = f"""You are an expert SQLite assistant.
A SQL query failed. Fix it so it runs correctly against the schema below.
Respond with ONLY the corrected SQL inside a ```sql code block — no explanations.

{schema}"""

    user = f"""Original question: {question}

Failing query:
```sql
{bad_sql}
```

Error message: {error}

Please provide the corrected query."""

    raw = _call_openai(system, user)
    return _extract_sql(raw)


def run(question: str, schema: str, execute_fn) -> dict:
    """
    Self-healing loop:
      1. Generate SQL.
      2. Execute it.
      3. On failure, ask Claude to fix it and retry — up to MAX_RETRIES times.

    Returns a dict with keys: sql, columns, rows, attempts, success[, error].
    """
    sql = None
    error = None

    for attempt in range(1, MAX_RETRIES + 1):
        divider = "─" * 52
        print(f"\n{divider}")

        if attempt == 1:
            print(f"[Attempt {attempt}/{MAX_RETRIES}] Generating SQL…")
            sql = generate_sql(schema, question)
        else:
            print(f"[Attempt {attempt}/{MAX_RETRIES}] Healing SQL…")
            sql = fix_sql(schema, question, sql, error)

        print(f"\nSQL:\n  {sql}\n")
        print("Executing…")

        try:
            columns, rows = execute_fn(sql)
            return {
                "sql": sql,
                "columns": columns,
                "rows": rows,
                "attempts": attempt,
                "success": True,
            }
        except Exception as exc:
            error = str(exc)
            print(f"  Error: {error}")

    return {
        "sql": sql,
        "columns": [],
        "rows": [],
        "attempts": MAX_RETRIES,
        "success": False,
        "error": error,
    }
