from database import init_db, get_schema, execute_query
from agent import run

DEMO_QUESTIONS = [
    "Show all employees with their department names and salaries",
    "Which department has the highest average salary?",
    "What is the total sales amount per employee?",
    "List employees hired after 2021, sorted by salary descending",
    "What is the monthly sales total for Q1 2024?",
    "Who are the top 3 salespeople by total revenue?",
]


def _print_table(columns: list[str], rows: list[tuple]):
    if not rows:
        print("  (no rows returned)")
        return

    widths = [len(c) for c in columns]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val) if val is not None else "NULL"))

    header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    sep = "-+-".join("-" * w for w in widths)
    print(f"  {header}")
    print(f"  {sep}")
    for row in rows:
        line = " | ".join(
            (str(v) if v is not None else "NULL").ljust(widths[i])
            for i, v in enumerate(row)
        )
        print(f"  {line}")


def _print_result(result: dict):
    if not result["success"]:
        print(f"\nFailed after {result['attempts']} attempt(s).")
        print(f"Final error: {result['error']}")
        return

    n = len(result["rows"])
    attempts = result["attempts"]
    healed = " (self-healed)" if attempts > 1 else ""
    print(f"\nSuccess{healed} — {n} row{'s' if n != 1 else ''} ({attempts} attempt(s))")
    _print_table(result["columns"], result["rows"])


def main():
    print("Initializing database…")
    init_db()
    schema = get_schema()

    print("\n╔══════════════════════════════════════╗")
    print("║      Self-Healing SQL Agent          ║")
    print("╚══════════════════════════════════════╝")

    print(f"\n{schema}")

    print("\nDemo questions (enter a number, or type your own):")
    for i, q in enumerate(DEMO_QUESTIONS, 1):
        print(f"  {i}. {q}")
    print("\nType 'q' to quit.\n")

    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not raw:
            continue
        if raw.lower() == "q":
            print("Goodbye!")
            break

        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(DEMO_QUESTIONS):
                question = DEMO_QUESTIONS[idx]
                print(f"Question: {question}")
            else:
                print(f"Enter 1–{len(DEMO_QUESTIONS)}.")
                continue
        else:
            question = raw

        result = run(question, schema, execute_query)
        _print_result(result)
        print()


if __name__ == "__main__":
    main()
