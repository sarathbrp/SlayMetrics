#!/usr/bin/env python3
"""Load knowledge documents from facts/ into TiDB for agent semantic search.

Reads .md files from the facts/ directory, chunks them by section headers,
embeds each chunk, and stores them in the facts table as type='knowledge'.

Usage:
    python tools/load_facts.py
    python tools/load_facts.py --dir /path/to/docs
    python tools/load_facts.py --clear   # remove all knowledge entries first
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from memory.embeddings import from_config as embedder_from_config


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def chunk_markdown(text: str, source_file: str) -> list[dict]:
    """Split markdown by ## headers into chunks. Each chunk gets the header as title."""
    chunks = []
    current_title = source_file
    current_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            # Save previous chunk
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    chunks.append({"title": current_title, "body": body})
            current_title = line.lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Save last chunk
    if current_lines:
        body = "\n".join(current_lines).strip()
        if body:
            chunks.append({"title": current_title, "body": body})

    # If no ## headers found, treat whole file as one chunk
    if not chunks:
        body = text.strip()
        if body:
            chunks.append({"title": source_file, "body": body})

    return chunks


def load_facts(facts_dir: str, cfg: dict, clear: bool = False) -> int:
    embedder = embedder_from_config(cfg)

    m = cfg["memory"]
    conn_kwargs = dict(
        host=m["host"],
        port=int(m.get("port", 4000)),
        user=m["user"],
        password=os.environ.get(m.get("password_env", ""), "") or "",
        database=m["database"],
        charset="utf8mb4",
        autocommit=True,
    )

    import pymysql
    import pymysql.cursors

    conn = pymysql.connect(**conn_kwargs, cursorclass=pymysql.cursors.DictCursor)

    if clear:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM knowledge WHERE type = 'knowledge'")
            print("Cleared existing knowledge entries.")

    # Find all .md files
    md_files = sorted(f for f in os.listdir(facts_dir) if f.endswith(".md"))

    if not md_files:
        print(f"No .md files found in {facts_dir}")
        return 0

    total = 0
    for filename in md_files:
        filepath = os.path.join(facts_dir, filename)
        with open(filepath) as f:
            text = f.read()

        chunks = chunk_markdown(text, filename)
        print(f"  {filename}: {len(chunks)} chunks")

        for chunk in chunks:
            fid = str(uuid.uuid4())
            embed_text = f"{chunk['title']} {chunk['body'][:2000]}"
            embedding = embedder.embed(embed_text)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO knowledge
                        (id, discovered_by, scope, type, parameter, reasoning, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        fid,
                        "__knowledge__",
                        "universal",
                        "knowledge",
                        chunk["title"],
                        chunk["body"][:10000],
                        json.dumps(embedding),
                    ),
                )
            total += 1

    conn.close()
    return total


def main():
    parser = argparse.ArgumentParser(description="Load knowledge docs from facts/ into TiDB")
    parser.add_argument("--dir", default="facts", help="Directory with .md files (default: facts/)")
    parser.add_argument(
        "--config", default="config.yaml", help="Config file (default: config.yaml)"
    )
    parser.add_argument(
        "--clear", action="store_true", help="Clear existing knowledge entries first"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    total = load_facts(args.dir, cfg, clear=args.clear)
    print(
        f"\nLoaded {total} chunks into TiDB knowledge table (type='knowledge', scope='universal')."
    )
    print("Agent can now query these via semantic search during diagnosis.")


if __name__ == "__main__":
    main()
