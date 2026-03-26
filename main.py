from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid

import yaml
from pathlib import Path

from agents import AgentDeps, TokenCounter
from adapters import load_adapter
from memory.embeddings import from_config as embedder_from_config
from memory.tidb_store import from_config as tidb_from_config
from tools.ssh import from_config as ssh_from_config
from core import log as logger


def load_dotenv():
    """Load .env file if present. Supports KEY=VALUE and KEY='VALUE' formats."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    logger.status("main", "Loading .env file")
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def get_model(cfg: dict):
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    profile_name = cfg["llm"]["active_profile"]
    profile = cfg["llm"]["profiles"][profile_name]
    logger.status("main", f"LLM profile: {profile_name} ({profile['backend']} / {profile['model']})")

    match profile["backend"]:
        case "claude":
            api_key = os.environ.get(profile.get("api_key_env", "ANTHROPIC_API_KEY"))
            if not api_key:
                logger.log("main", f"{profile.get('api_key_env','ANTHROPIC_API_KEY')} not set", "error")
                sys.exit(1)
            return AnthropicModel(profile["model"])
        case "vllm" | "ollama":
            provider = OpenAIProvider(
                base_url=profile["base_url"],
                api_key="ollama",
            )
            return OpenAIChatModel(
                profile["model"],
                provider=provider,
            )
        case _:
            logger.log("main", f"Unknown backend: {profile['backend']}", "error")
            sys.exit(1)


def load_knowledge(cfg: dict, embedder, memory) -> None:
    """Load facts/ knowledge docs into TiDB if they've changed since last load."""
    facts_dir = Path(__file__).parent / "facts"
    if not facts_dir.exists():
        return

    md_files = sorted(facts_dir.glob("*.md"))
    if not md_files:
        return

    # Hash all md files to detect changes
    hasher = hashlib.md5()
    for f in md_files:
        hasher.update(f.read_bytes())
    current_hash = hasher.hexdigest()

    # Check stored hash
    hash_file = facts_dir / ".loaded_hash"
    if hash_file.exists() and hash_file.read_text().strip() == current_hash:
        logger.status("knowledge", f"{len(md_files)} docs (unchanged, skipping load)")
        return

    # Load knowledge into TiDB
    logger.status("knowledge", f"Loading {len(md_files)} docs into TiDB...")
    import pymysql
    conn_kwargs = dict(
        host=cfg["memory"]["host"],
        port=int(cfg["memory"].get("port", 4000)),
        user=cfg["memory"]["user"],
        password=os.environ.get(cfg["memory"].get("password_env", ""), "") or "",
        database=cfg["memory"]["database"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
    conn = pymysql.connect(**conn_kwargs)

    # Clear old knowledge
    with conn.cursor() as cur:
        cur.execute("DELETE FROM facts WHERE type = 'knowledge'")

    total = 0
    for filepath in md_files:
        text = filepath.read_text()
        chunks = _chunk_markdown(text, filepath.name)
        for chunk in chunks:
            fid = str(uuid.uuid4())
            embed_text = f"{chunk['title']} {chunk['body'][:2000]}"
            embedding = embedder.embed(embed_text)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO facts (id, session_id, type, parameter, reasoning, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (fid, "__knowledge__", "knowledge", chunk["title"],
                      chunk["body"][:10000], json.dumps(embedding)))
            total += 1
        logger.status("knowledge", f"  {filepath.name}: {len(chunks)} chunks")

    conn.close()
    hash_file.write_text(current_hash)
    logger.status("knowledge", f"{total} chunks loaded")


def _chunk_markdown(text: str, source_file: str) -> list[dict]:
    """Split markdown by ## headers into chunks."""
    chunks = []
    current_title = source_file
    current_lines = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    chunks.append({"title": current_title, "body": body})
            current_title = line.lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        body = "\n".join(current_lines).strip()
        if body:
            chunks.append({"title": current_title, "body": body})

    if not chunks:
        body = text.strip()
        if body:
            chunks.append({"title": source_file, "body": body})

    return chunks


async def main(config_path: str, session_id: str | None, verbose: bool = False) -> None:
    cfg = load_config(config_path)

    # Session ID — generate or reuse
    if session_id is None:
        session_id = str(uuid.uuid4())[:8]

    # Init logger first so everything gets captured
    logger.init(session_id, verbose=verbose)
    logger.status("main", f"Session: {session_id}")

    # Load .env
    load_dotenv()

    # Wire up dependencies
    embedder = embedder_from_config(cfg)
    memory = tidb_from_config(cfg, embedder)
    memory.connect()
    logger.status("main", "TiDB connected")

    # Load knowledge base (facts/ folder) — skips if unchanged
    load_knowledge(cfg, embedder, memory)

    ssh = ssh_from_config(cfg)
    ssh.connect()
    host = cfg["target"]["host"]
    mode = "local (subprocess)" if host in ("localhost", "127.0.0.1", "::1") else f"SSH ({host})"
    logger.status("main", f"Target: {host} via {mode}")

    adapter = load_adapter(cfg, ssh)
    model = get_model(cfg)

    profile_name = cfg["llm"]["active_profile"]
    if not memory.session_exists(session_id):
        memory.create_session(
            session_id=session_id,
            service=cfg["service"]["name"],
            host=cfg["target"]["host"],
            llm_profile=profile_name,
        )

    token_counter = TokenCounter()
    deps = AgentDeps(
        adapter=adapter,
        memory=memory,
        ssh=ssh,
        session_id=session_id,
        config=cfg,
        token_counter=token_counter,
    )

    try:
        from core.orchestrator import run
        report_path = await run(model, deps)
        logger.status("main", f"Report: {report_path}")
    except KeyboardInterrupt:
        logger.log("main", "Interrupted by user (Ctrl+C)", "warn")
        logger.log("main", f"Session {session_id} can be resumed with: "
                   f"python3 main.py --session {session_id}", "warn")
        logger.log("main", f"Tokens used so far: {token_counter.summary()}", "warn")
    except Exception as e:
        logger.log("main", f"Error: {e}", "error")
        raise
    finally:
        # Silent cleanup
        logger.close()
        ssh.disconnect()
        memory.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SlayMetricsAgent")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config file (default: config.yaml)")
    parser.add_argument("--session", default=None,
                        help="Resume an existing session ID")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show all agent logs (not just actions/results)")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.config, args.session, args.verbose))
    except KeyboardInterrupt:
        print("\nAborted.")
