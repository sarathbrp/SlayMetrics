from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core import log as logger

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "benchmark" / "phase1-script.py"


def collect_snapshot(client, *, scope: str, host: str, source: str) -> dict[str, Any]:
    script_source = SCRIPT_PATH.read_text()
    command = f"python3 - <<'PY'\n{script_source}\nPY"
    result = client.execute(command, timeout=60)
    if not result.ok:
        output = result.stderr.strip() or result.stdout.strip()
        return {
            "scope": scope,
            "source": source,
            "host": host,
            "summary": {"error": output[:200]},
            "sections": {},
            "error": output[:12000],
        }

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        output = result.stdout.strip() or result.stderr.strip()
        return {
            "scope": scope,
            "source": source,
            "host": host,
            "summary": {"error": f"telemetry JSON parse failed: {exc.msg}"},
            "sections": {},
            "error": output[:12000],
        }

    return {
        "scope": scope,
        "source": source,
        "host": host,
        "summary": payload.get("summary", {}),
        "sections": payload.get("sections", {}),
    }


def persist_snapshot(memory, session_id: str, snapshot: dict[str, Any]) -> None:
    scope = snapshot["scope"]
    source = snapshot["source"]
    summary = snapshot["summary"]
    memory.save_context(
        session_id,
        "telemetry",
        f"{scope}:{source}",
        json.dumps(snapshot),
        (
            f"{source} telemetry | workers={summary.get('nginx_worker_count', 0)} "
            f"cores={summary.get('nginx_worker_cores', [])} "
            f"somaxconn={summary.get('somaxconn', 'unknown')} "
            f"syn_backlog={summary.get('tcp_max_syn_backlog', 'unknown')} "
            f"ports={summary.get('ip_local_port_range', 'unknown')} "
            f"rx_drop={summary.get('rx_drop_total', 'unknown')} "
            f"tx_drop={summary.get('tx_drop_total', 'unknown')}"
        ),
    )
    logger.status(
        "telemetry",
        (
            f"{scope} {source}: workers={summary.get('nginx_worker_count', 0)} "
            f"somaxconn={summary.get('somaxconn', 'unknown')} "
            f"drops(rx/tx)={summary.get('rx_drop_total', 'unknown')}/"
            f"{summary.get('tx_drop_total', 'unknown')}"
        ),
    )
