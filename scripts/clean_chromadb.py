"""
Clean ChromaDB semantic memory store.

Usage:
  python scripts/clean_chromadb.py              # show stats only
  python scripts/clean_chromadb.py --reset      # delete all entries
  python scripts/clean_chromadb.py --before 2026-04-09  # delete entries before date
"""

import argparse
import sys
from pathlib import Path

# Patch sqlite3 before importing chromadb (same as memory.py)
try:
    __import__("pysqlite3")
    sys.modules["sqlite3"] = sys.modules["pysqlite3"]
except ImportError:
    pass

import chromadb

CHROMA_DIR  = Path(__file__).parent.parent / "dspy_data" / "chroma"
COLLECTION  = "rca_cases"


def get_collection() -> chromadb.Collection:
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(COLLECTION)


def show_stats(col: chromadb.Collection) -> None:
    count = col.count()
    print(f"ChromaDB store: {CHROMA_DIR}")
    print(f"Collection    : {COLLECTION}")
    print(f"Total entries : {count}")
    if count == 0:
        return
    results = col.get(include=["metadatas"])
    print(f"\n{'Session':<10} {'Timestamp':<25} {'Improvement':>12} {'Applied':>8} {'Rejected':>9}")
    print("-" * 68)
    for meta in results["metadatas"]:
        session   = meta.get("session_id", "?")[:8]
        ts        = meta.get("timestamp", "?")[:19]
        pct       = meta.get("max_improvement_pct", 0.0)
        applied   = meta.get("applied_fixes", "[]")
        rejected  = meta.get("rejected_fixes", "[]")
        n_applied  = applied.count("description") if applied else 0
        n_rejected = rejected.count("description") if rejected else 0
        print(f"{session:<10} {ts:<25} {pct:>+11.1f}% {n_applied:>8} {n_rejected:>9}")


def reset_all(col: chromadb.Collection) -> None:
    count = col.count()
    if count == 0:
        print("Nothing to delete.")
        return
    confirm = input(f"Delete all {count} entries? [y/N] ")
    if confirm.strip().lower() != "y":
        print("Aborted.")
        return
    ids = col.get()["ids"]
    col.delete(ids=ids)
    print(f"Deleted {len(ids)} entries.")


def delete_before(col: chromadb.Collection, before_date: str) -> None:
    results = col.get(include=["metadatas"])
    to_delete = [
        id_ for id_, meta in zip(results["ids"], results["metadatas"])
        if meta.get("timestamp", "") < before_date
    ]
    if not to_delete:
        print(f"No entries before {before_date}.")
        return
    confirm = input(f"Delete {len(to_delete)} entries before {before_date}? [y/N] ")
    if confirm.strip().lower() != "y":
        print("Aborted.")
        return
    col.delete(ids=to_delete)
    print(f"Deleted {len(to_delete)} entries.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage ChromaDB semantic memory")
    parser.add_argument("--reset",  action="store_true", help="Delete all entries")
    parser.add_argument("--before", metavar="DATE",      help="Delete entries before DATE (YYYY-MM-DD)")
    args = parser.parse_args()

    if not CHROMA_DIR.exists():
        print(f"ChromaDB directory not found: {CHROMA_DIR}")
        sys.exit(1)

    col = get_collection()

    if args.reset:
        reset_all(col)
    elif args.before:
        delete_before(col, args.before)
    else:
        show_stats(col)


if __name__ == "__main__":
    main()
