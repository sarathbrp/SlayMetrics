from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path

import yaml

from adapters import load_adapter
from agents import AgentDeps, TokenCounter
from services import load_profile
from core import log as logger
from memory.embeddings import from_config as embedder_from_config
from memory.tidb_store import from_config as tidb_from_config
from models import create_model
from telemetry import LangfuseClient
from tools.ssh import from_config as ssh_from_config


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
        raw = f.read()
    # Resolve ${VAR:-default} patterns from environment
    import re

    def _resolve(m):
        var = m.group(1)
        default = m.group(2) if m.group(2) is not None else ""
        return os.environ.get(var, default)

    raw = re.sub(r"\$\{(\w+):-([^}]*)\}", _resolve, raw)
    raw = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), raw)
    return yaml.safe_load(raw)


def get_model(cfg: dict):
    return create_model(cfg)


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
        cur.execute("DELETE FROM knowledge WHERE type = 'knowledge'")

    total = 0
    for filepath in md_files:
        text = filepath.read_text()
        chunks = _chunk_markdown(text, filepath.name)
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
        logger.status("knowledge", f"  {filepath.name}: {len(chunks)} chunks")

    conn.close()
    hash_file.write_text(current_hash)
    logger.status("knowledge", f"{total} chunks loaded")


def _chunk_markdown(text: str, source_file: str) -> list[dict]:
    """Split markdown by ## headers into chunks."""
    chunks = []
    current_title = source_file
    current_lines: list[str] = []

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


async def main(
    config_path: str,
    session_id: str | None,
    verbose: bool = False,
    max_phase: int | None = None,
    planner_mode: str | None = None,
    baseline_mode: str | None = None,
    approval_mode: str | None = None,
) -> None:
    # Load .env FIRST so ${DUT_HOST} etc. resolve in config.yaml
    load_dotenv()

    cfg = load_config(config_path)
    cfg.setdefault("agent", {})
    if max_phase is not None:
        cfg["agent"]["max_phase"] = max_phase
    if planner_mode is not None:
        normalized_planner_mode = planner_mode.strip().lower()
        if normalized_planner_mode == "single":
            normalized_planner_mode = "deterministic"
        cfg["agent"]["planner_mode"] = normalized_planner_mode
    if baseline_mode is not None:
        cfg["agent"]["baseline_mode"] = baseline_mode
    if approval_mode is not None:
        cfg.setdefault("tools", {})["approval_mode"] = approval_mode.replace("-", "_")

    resolved_max_phase = int((cfg.get("agent") or {}).get("max_phase", 4))
    resolved_planner_mode = str((cfg.get("agent") or {}).get("planner_mode", "debate")).strip()
    resolved_baseline_mode = str((cfg.get("agent") or {}).get("baseline_mode", "fresh")).strip()

    # Session ID — generate or reuse
    if session_id is None:
        session_id = str(uuid.uuid4())[:8]

    # Init logger first so everything gets captured
    logger.init(session_id, verbose=verbose)
    logger.status("main", f"Session: {session_id}")

    # Wire up dependencies
    embedder = embedder_from_config(cfg)
    memory = tidb_from_config(cfg, embedder)
    memory.connect()
    logger.status("main", "TiDB connected")

    # Load knowledge base (facts/ folder) — skips if unchanged
    load_knowledge(cfg, embedder, memory)

    ssh = ssh_from_config(cfg, section="target")
    ssh.connect()
    host = cfg["target"]["host"]
    mode = "local (subprocess)" if host in ("localhost", "127.0.0.1", "::1") else f"SSH ({host})"
    logger.status("main", f"DUT: {host} via {mode}")

    # Bench executor — where wrk2 runs (may be same machine or separate)
    bench = ssh_from_config(cfg, section="bench") if "bench" in cfg else ssh
    bench.connect()
    bench_host = cfg.get("bench", {}).get("host", host)
    bench_mode = (
        "local (subprocess)"
        if bench_host in ("localhost", "127.0.0.1", "::1")
        else f"SSH ({bench_host})"
    )
    logger.status("main", f"Bench: {bench_host} via {bench_mode}")

    adapter = load_adapter(cfg, ssh, bench=bench)
    service_profile = load_profile(cfg["service"]["name"])
    logger.status("main", f"Service profile: {service_profile.name} ({service_profile.type})")
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
    tracing_cfg = cfg.get("telemetry", {}).get("langfuse", {}) or {}
    langfuse_enabled = bool(tracing_cfg.get("enabled", False))
    langfuse = LangfuseClient.from_env(
        {
            "session_id": session_id,
            "service": cfg["service"]["name"],
            "planner_mode": resolved_planner_mode,
            "max_phase": resolved_max_phase,
            "baseline_mode": resolved_baseline_mode,
            "llm_profile": profile_name,
            "dut_host": host,
            "bench_host": bench_host,
        },
        enabled=langfuse_enabled,
    )
    if langfuse.enabled:
        if langfuse.auth_check():
            logger.status("langfuse", "Tracing enabled (auth OK)")
        else:
            logger.log("langfuse", "Tracing disabled: auth check failed", "warn")
            langfuse = LangfuseClient.from_env(enabled=False)
    deps = AgentDeps(
        adapter=adapter,
        memory=memory,
        ssh=ssh,
        bench=bench,
        session_id=session_id,
        config=cfg,
        token_counter=token_counter,
        langfuse=langfuse,
        service_profile=service_profile,
    )

    try:
        from core.orchestrator import run

        with langfuse.trace(
            "slaymetrics_run",
            input={"config_path": config_path},
            metadata={
                "session_id": session_id,
                "planner_mode": planner_mode,
                "max_phase": max_phase,
                "baseline_mode": baseline_mode,
                "llm_profile": profile_name,
            },
        ):
            report_path = await run(model, deps)
        logger.status("main", f"Report: {report_path}")
        if langfuse.last_trace_url:
            logger.status("langfuse", f"Trace: {langfuse.last_trace_url}")
    except KeyboardInterrupt:
        logger.log("main", "Interrupted by user (Ctrl+C)", "warn")
        logger.log(
            "main",
            f"Session {session_id} can be resumed with: python3 main.py --session {session_id}",
            "warn",
        )
        logger.log("main", f"Tokens used so far: {token_counter.summary()}", "warn")
    except Exception as e:
        logger.log("main", f"Error: {e}", "error")
        try:
            from core.slack_notifier import SlackNotifier

            SlackNotifier(cfg).notify_error(
                session_id=session_id,
                error=str(e),
                context=f"LLM: {profile_name} | Tokens: {token_counter.total:,}",
            )
        except Exception:
            pass
        raise
    finally:
        # Silent cleanup
        langfuse.flush()
        langfuse.shutdown()
        logger.close()
        ssh.disconnect()
        if bench is not ssh:
            bench.disconnect()
        memory.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SlayMetricsAgent")
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config file (default: config.yaml)"
    )
    parser.add_argument("--session", default=None, help="Resume an existing session ID")
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show all agent logs (not just actions/results)",
    )
    parser.add_argument(
        "--max-phase",
        type=int,
        choices=[3, 4],
        default=None,
        help="Maximum phase to run: 3 stops after RCA/recommendations, 4 runs remediation",
    )
    parser.add_argument(
        "--planner-mode",
        choices=["single", "deterministic", "hybrid", "debate"],
        default=None,
        help="Planning path override: deterministic, hybrid, debate, or legacy single alias",
    )
    parser.add_argument(
        "--baseline-mode",
        choices=["fresh", "reuse"],
        default=None,
        help=(
            "Baseline acquisition: run a fresh baseline or reuse "
            "the latest stored one for this host"
        ),
    )
    parser.add_argument(
        "--approval-mode",
        choices=["auto", "interactive", "dry-run"],
        default=None,
        help="Tool approval mode: auto (default), interactive (prompt), dry-run (plan only)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(
            main(
                args.config,
                args.session,
                args.verbose,
                args.max_phase,
                args.planner_mode,
                args.baseline_mode,
                args.approval_mode,
            )
        )
    except KeyboardInterrupt:
        print("\nAborted.")
