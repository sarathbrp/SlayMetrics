from __future__ import annotations

import csv
import io
import json
import shlex
from pathlib import Path
from typing import Any

from core import log as logger

SNAPSHOT_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "benchmark" / "phase1-script.py"
SAMPLER_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "benchmark" / "phase1-sampler.py"


def collect_snapshot(client, *, scope: str, host: str, source: str) -> dict[str, Any]:
    script_source = SNAPSHOT_SCRIPT_PATH.read_text()
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


def start_sampler(client, *, scope: str, host: str, interval_sec: int = 1) -> dict[str, Any]:
    script_source = SAMPLER_SCRIPT_PATH.read_text()
    remote_script = f"/tmp/slaymetrics_{scope}_sampler.py"
    csv_path = f"/tmp/slaymetrics_{scope}_telemetry.csv"
    pid_path = f"/tmp/slaymetrics_{scope}_telemetry.pid"
    log_path = f"/tmp/slaymetrics_{scope}_telemetry.log"
    command = (
        f"cat > {shlex.quote(remote_script)} <<'PY'\n{script_source}\nPY\n"
        f"chmod +x {shlex.quote(remote_script)}\n"
        f"rm -f {shlex.quote(csv_path)} {shlex.quote(pid_path)} {shlex.quote(log_path)}\n"
        f"nohup python3 {shlex.quote(remote_script)} "
        f"--output {shlex.quote(csv_path)} --interval {int(interval_sec)} "
        f"> {shlex.quote(log_path)} 2>&1 < /dev/null & echo $! > {shlex.quote(pid_path)}\n"
    )
    result = client.execute(command, timeout=60)
    if not result.ok:
        output = result.stderr.strip() or result.stdout.strip()
        return {
            "scope": scope,
            "host": host,
            "csv_path": csv_path,
            "pid_path": pid_path,
            "log_path": log_path,
            "ok": False,
            "error": output[:12000],
        }
    logger.status("telemetry", f"{scope} sampler started: {csv_path}")
    return {
        "scope": scope,
        "host": host,
        "csv_path": csv_path,
        "pid_path": pid_path,
        "log_path": log_path,
        "ok": True,
    }


def stop_sampler(client, *, scope: str, host: str) -> dict[str, Any]:
    csv_path = f"/tmp/slaymetrics_{scope}_telemetry.csv"
    pid_path = f"/tmp/slaymetrics_{scope}_telemetry.pid"
    log_path = f"/tmp/slaymetrics_{scope}_telemetry.log"
    command = (
        f"if [ -f {shlex.quote(pid_path)} ]; then "
        f"kill $(cat {shlex.quote(pid_path)}) 2>/dev/null || true; "
        f"rm -f {shlex.quote(pid_path)}; "
        "fi\n"
        "sleep 1\n"
        f"cat {shlex.quote(csv_path)} 2>/dev/null\n"
    )
    result = client.execute(command, timeout=60)
    csv_content = result.stdout
    summary = summarize_csv(csv_content)
    if not result.ok and not csv_content.strip():
        output = result.stderr.strip() or result.stdout.strip()
        return {
            "scope": scope,
            "host": host,
            "csv_path": csv_path,
            "log_path": log_path,
            "ok": False,
            "error": output[:12000],
            "summary": {"error": output[:200]},
        }
    logger.status(
        "telemetry",
        (
            f"{scope} post: workers={summary.get('last_worker_count', 0)} "
            f"somaxconn={summary.get('somaxconn', 'unknown')} "
            f"drops(rx/tx)={summary.get('last_rx_drop_total', 'unknown')}/"
            f"{summary.get('last_tx_drop_total', 'unknown')}"
        ),
    )
    return {
        "scope": scope,
        "host": host,
        "csv_path": csv_path,
        "log_path": log_path,
        "ok": True,
        "csv_content": csv_content,
        "summary": summary,
    }


def summarize_csv(csv_content: str) -> dict[str, Any]:
    if not csv_content.strip():
        return {"sample_count": 0}
    rows = list(csv.DictReader(io.StringIO(csv_content)))
    if not rows:
        return {"sample_count": 0}
    samples = [_normalize_row(row) for row in rows]
    first = samples[0]
    last = samples[-1]
    run_queue_values = [sample["vmstat_run_queue"] for sample in samples]
    blocked_values = [sample["vmstat_blocked"] for sample in samples]
    established_values = [sample["tcp_established"] for sample in samples]
    worker_spreads = [len(sample["nginx_worker_cores"]) for sample in samples]
    duration_sec = max(last["timestamp"] - first["timestamp"], 0)
    rx_drop_delta = max(last["rx_drop_total"] - first["rx_drop_total"], 0)
    tx_drop_delta = max(last["tx_drop_total"] - first["tx_drop_total"], 0)
    return {
        "sample_count": len(samples),
        "duration_sec": duration_sec,
        "first_worker_count": first["nginx_worker_count"],
        "last_worker_count": last["nginx_worker_count"],
        "somaxconn": last["somaxconn"],
        "tcp_max_syn_backlog": last["tcp_max_syn_backlog"],
        "ip_local_port_range": last["ip_local_port_range"],
        "first_rx_drop_total": first["rx_drop_total"],
        "last_rx_drop_total": last["rx_drop_total"],
        "first_tx_drop_total": first["tx_drop_total"],
        "last_tx_drop_total": last["tx_drop_total"],
        "rx_drop_delta": rx_drop_delta,
        "tx_drop_delta": tx_drop_delta,
        "rx_drop_rate_per_sec": round(rx_drop_delta / duration_sec, 2) if duration_sec else 0.0,
        "tx_drop_rate_per_sec": round(tx_drop_delta / duration_sec, 2) if duration_sec else 0.0,
        "run_queue_avg": round(sum(run_queue_values) / len(run_queue_values), 2),
        "run_queue_max": max(run_queue_values),
        "blocked_avg": round(sum(blocked_values) / len(blocked_values), 2),
        "blocked_max": max(blocked_values),
        "tcp_established_avg": round(sum(established_values) / len(established_values), 2),
        "tcp_established_max": max(established_values),
        "worker_core_spread_max": max(worker_spreads),
        "first_sample": _sample_summary(first),
        "last_sample": _sample_summary(last),
    }


def persist_sampler_result(memory, session_id: str, sampler_result: dict[str, Any]) -> None:
    scope = sampler_result["scope"]
    summary = sampler_result.get("summary", {})
    csv_content = sampler_result.get("csv_content", "")
    if csv_content:
        memory.save_context(
            session_id,
            "command_output",
            f"telemetry_csv_{scope}",
            csv_content,
            f"{scope} telemetry csv ({summary.get('sample_count', 0)} samples)",
        )
    memory.save_context(
        session_id,
        "telemetry",
        f"{scope}:series",
        json.dumps(
            {
                "summary": summary,
                "first_sample": summary.get("first_sample", {}),
                "last_sample": summary.get("last_sample", {}),
                "csv_path": sampler_result.get("csv_path", ""),
            }
        ),
        (
            f"{scope} series | samples={summary.get('sample_count', 0)} "
            f"duration={summary.get('duration_sec', 0)}s "
            f"rx_drop_delta={summary.get('rx_drop_delta', 0)} "
            f"runq_max={summary.get('run_queue_max', 0)}"
        ),
    )


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


def _normalize_row(row: dict[str, str]) -> dict[str, Any]:
    return {
        "timestamp": _to_int(row.get("timestamp")),
        "nginx_worker_count": _to_int(row.get("nginx_worker_count")),
        "nginx_worker_cores": _parse_cores(row.get("nginx_worker_cores", "")),
        "somaxconn": row.get("somaxconn", "unknown"),
        "tcp_max_syn_backlog": row.get("tcp_max_syn_backlog", "unknown"),
        "ip_local_port_range": row.get("ip_local_port_range", "unknown"),
        "rx_drop_total": _to_int(row.get("rx_drop_total")),
        "tx_drop_total": _to_int(row.get("tx_drop_total")),
        "tcp_established": _to_int(row.get("tcp_established")),
        "mem_used_mb": _to_int(row.get("mem_used_mb")),
        "vmstat_run_queue": _to_int(row.get("vmstat_run_queue")),
        "vmstat_blocked": _to_int(row.get("vmstat_blocked")),
    }


def _sample_summary(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "nginx_worker_count": sample["nginx_worker_count"],
        "nginx_worker_cores": sample["nginx_worker_cores"],
        "somaxconn": sample["somaxconn"],
        "tcp_max_syn_backlog": sample["tcp_max_syn_backlog"],
        "ip_local_port_range": sample["ip_local_port_range"],
        "rx_drop_total": sample["rx_drop_total"],
        "tx_drop_total": sample["tx_drop_total"],
        "tcp_established": sample["tcp_established"],
        "mem_used_mb": sample["mem_used_mb"],
        "vmstat_run_queue": sample["vmstat_run_queue"],
        "vmstat_blocked": sample["vmstat_blocked"],
    }


def _parse_cores(value: str) -> list[int]:
    cores: list[int] = []
    for item in str(value).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            cores.append(int(item))
        except ValueError:
            continue
    return cores


def _to_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return 0
