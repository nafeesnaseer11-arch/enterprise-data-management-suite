"""Command-line entry point.

    python -m scripts.run_pipeline                       # full end-to-end (ETL + governance + RAG index)
    python -m scripts.run_pipeline --layers bronze silver
    python -m scripts.run_pipeline --ask "Which region had the highest revenue?" --role executive
"""
from __future__ import annotations

import argparse

from edms.suite import Suite


def main():
    ap = argparse.ArgumentParser(description="Enterprise Data Management Suite")
    ap.add_argument("--layers", nargs="*", choices=["bronze", "silver", "gold"])
    ap.add_argument("--index-only", action="store_true")
    ap.add_argument("--ask")
    ap.add_argument("--role", default="data_admin")
    a = ap.parse_args()
    suite = Suite()
    if a.ask:
        r = suite.chatbot.ask(a.ask, a.role)
        print(r["answer"])
        for q in r["queries"]:
            print("\nSQL:", q["sql"])
            print(q.get("error") or q["result"].head(20).to_string())
        return
    if a.index_only:
        suite.build_index()
    elif a.layers:
        suite.run_pipeline(tuple(a.layers))
    else:
        suite.run_end_to_end()


if __name__ == "__main__":
    main()
