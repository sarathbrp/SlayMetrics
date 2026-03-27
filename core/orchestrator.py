from __future__ import annotations

import json
import os
import statistics

import agents.agent as diagnosis_agent
import core.reporter as reporter
import rhel.system_checks as system_checks
from agents import AgentDeps
from core import log as logger

PAYLOAD_SIZES = ["small", "medium", "large"]


async def run(model, deps: AgentDeps) -> str:
    """Main orchestration loop. Returns path to final report."""
    cfg = deps.config
    session_id = deps.session_id
    memory = deps.memory

    logger.panel(
        "SlayMetricsAgent",
        f"Session: {session_id}\nService: {cfg['service']['name']} on {cfg['target']['host']}",
    )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1: RHEL system checks (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 1: Running RHEL system checks...")
    checks = system_checks.run_all(deps.ssh, cfg["rhel"]["checks"])
    checks_summary = []
    for chk in checks:
        logger.check(chk.name, chk.value, chk.status, chk.recommendation)
        memory.save_context(session_id, "system_check", chk.name, chk.value, chk.recommendation)
        checks_summary.append(
            f"- {chk.name}: {chk.value[:100]} [{chk.status}] {chk.recommendation[:100]}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1.5: System fingerprint (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 1.5: Collecting system fingerprint...")
    ssh = deps.ssh
    rhel_ver = ssh.execute("cat /etc/redhat-release 2>/dev/null || echo unknown").stdout.strip()
    kernel_ver = ssh.execute("uname -r").stdout.strip()
    cpu_cores = int(ssh.execute("nproc").stdout.strip() or "0")
    ram_kb = ssh.execute("grep MemTotal /proc/meminfo | awk '{print $2}'").stdout.strip()
    ram_gb = int(ram_kb) // (1024 * 1024) if ram_kb.isdigit() else 0

    memory.update_profile(
        session_id,
        rhel_version=rhel_ver[:64],
        kernel_version=kernel_ver[:64],
        cpu_cores=cpu_cores,
        ram_gb=ram_gb,
    )
    logger.status(
        "system",
        f"RHEL: {rhel_ver}, Kernel: {kernel_ver}, CPU: {cpu_cores} cores, RAM: {ram_gb} GB",
    )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2: Baseline benchmarks (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    bench_cfg = cfg["service"]["benchmark"]
    bench_tool = bench_cfg.get("tool", "wrk2")

    if bench_tool == "hackathon":
        logger.step("Step 2: Running hackathon baseline benchmark...")
        baselines = _run_hackathon_benchmark(deps, cfg, "baseline", session_id)
    else:
        logger.step("Step 2: Running baseline benchmarks (small/medium/large)...")
        baselines = _run_wrk2_benchmarks(deps, cfg, "Baseline", session_id)

    baseline_rps = baselines.get("small", {}).get("rps",
                   baselines.get("homepage", {}).get("rps", 0))
    memory.update_profile(session_id, baseline_rps=baseline_rps, best_rps=baseline_rps)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 3: Collect service config (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 3: Collecting service configuration...")
    config_data = deps.adapter.get_config()
    nginx_config = config_data.get("raw", "")
    memory.save_context(
        session_id, "command_output", "service_config", nginx_config, "current service config"
    )
    logger.status(
        "collector", f"Config: {config_data.get('path', 'unknown')} ({len(nginx_config)} chars)"
    )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 4a: Check context — previously applied fixes (TiDB — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 4a: Checking for previously applied fixes...")
    past_fixes = list(memory.get_facts(session_id, type="fix") or [])
    # Also check ALL sessions for this host — cross-session learning
    all_fixes = list(memory.get_all_fixes_for_host(cfg["target"]["host"]) or [])

    prior_fixes = []
    seen_params = set()
    for f in all_fixes + past_fixes:
        param = f.get("parameter", "")
        if param and param not in seen_params:
            seen_params.add(param)
            prior_fixes.append(
                {
                    "parameter": param,
                    "value": f.get("after_value", ""),
                    "impact": f.get("impact_pct", 0),
                }
            )

    if prior_fixes:
        for pf in prior_fixes:
            logger.status(
                "context", f"  Prior fix: {pf['parameter']} = {pf['value']} ({pf['impact']:+.1f}%)"
            )
    else:
        logger.status("context", "No prior fixes found — fresh diagnosis")

    # Step 4b removed — RAG knowledge is in system prompt (proven fixes list).
    # Knowledge base stays in TiDB for query_memory tool if agent needs it.

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 5: LLM diagnosis + remediation (ONE agent, ONE context)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 5: Running AI diagnosis and remediation...")

    context_prompt = _build_context_prompt(
        rhel_ver=rhel_ver,
        kernel_ver=kernel_ver,
        cpu_cores=cpu_cores,
        ram_gb=ram_gb,
        checks_summary=checks_summary,
        baselines=baselines,
        prior_fixes=prior_fixes,
    )

    logger.log("orchestrator", f"Context prompt: {len(context_prompt)} chars", "info")

    diagnosis = await diagnosis_agent.run(model, deps, context_prompt)

    notes = getattr(diagnosis, "notes", getattr(diagnosis, "summary", ""))
    logger.log("agent", f"Summary: {notes}", "result")

    # Update best RPS from agent's result
    best_rps = baseline_rps
    diagnosis_after_rps = getattr(diagnosis, "after_rps", None)
    if diagnosis_after_rps is None:
        fixes_applied = getattr(diagnosis, "fixes_applied", []) or []
        diagnosis_after_rps = max((fix.get("after_rps", 0) for fix in fixes_applied), default=0)
    if diagnosis_after_rps > best_rps:
        best_rps = diagnosis_after_rps
    memory.update_profile(session_id, best_rps=best_rps)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 6: Final benchmarks (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    if bench_tool == "hackathon":
        logger.step("Step 6: Running hackathon final benchmark...")
        finals = _run_hackathon_benchmark(deps, cfg, "tuned", session_id)
    else:
        logger.step("Step 6: Running final benchmarks (small/medium/large)...")
        finals = _run_wrk2_benchmarks(deps, cfg, "Final", session_id)

    # Update best_rps from actual final benchmarks
    final_small_rps = finals.get("small", {}).get("rps",
                      finals.get("homepage", {}).get("rps", 0))
    if final_small_rps > best_rps:
        best_rps = final_small_rps
    memory.update_profile(session_id, best_rps=best_rps)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 6.5: Capture system throughput limits (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    # Capture system throughput limits silently (for report only)
    throughput_info = {}
    nic_result = ssh.execute(
        "ethtool ens3 2>/dev/null | grep Speed || "
        "cat /sys/class/net/$(ip route | awk '/default/{print $5}')/speed 2>/dev/null"
    )
    throughput_info["nic_speed"] = nic_result.stdout.strip()[:50]
    disk_result = ssh.execute(
        "dd if=/dev/zero of=/tmp/disktest bs=1M count=256 oflag=direct 2>&1 | tail -1"
    )
    throughput_info["disk_write"] = disk_result.stdout.strip()[:80]
    ssh.execute("rm -f /tmp/disktest")
    for size in PAYLOAD_SIZES:
        data = finals.get(size, {})
        rps = data.get("rps", 0)
        file_sizes = {"small": 1, "medium": 100, "large": 1024}
        throughput_info[f"{size}_throughput_mb_s"] = round(
            (rps * file_sizes.get(size, 1)) / 1024, 1
        )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 7: Stability test (direct wrk2 loop — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    stability_cfg = cfg["agent"].get("stability", {})
    stability_data = None

    if stability_cfg.get("enabled", False):
        total_duration = stability_cfg.get("duration_sec", 1800)
        interval = stability_cfg.get("sample_interval_sec", 60)
        url_key = stability_cfg.get("url_key", "small_file_url")
        stability_url = bench_cfg.get(url_key, "http://localhost/")
        num_samples = total_duration // interval

        logger.step(
            f"Step 7: Running {total_duration // 60}-minute stability test "
            f"({num_samples} samples)..."
        )
        samples = []

        for i in range(num_samples):
            result = deps.adapter.benchmark(duration=interval, url=stability_url)
            samples.append(result.requests_per_sec)
            logger.status(
                "stability", f"Sample {i + 1}/{num_samples}: {result.requests_per_sec:.1f} RPS"
            )

        mean_rps = statistics.mean(samples)
        stdev_rps = statistics.stdev(samples) if len(samples) > 1 else 0.0
        cv = (stdev_rps / mean_rps * 100) if mean_rps else 0.0

        stability_data = {
            "samples": samples,
            "mean_rps": round(mean_rps, 1),
            "stdev_rps": round(stdev_rps, 1),
            "cv_pct": round(cv, 1),
            "duration_sec": total_duration,
            "sample_count": num_samples,
        }
        memory.save_context(
            session_id,
            "benchmark",
            "stability_test",
            json.dumps(stability_data),
            f"Stability: mean={mean_rps:.1f} stdev={stdev_rps:.1f} CV={cv:.1f}%",
        )
        logger.status(
            "stability", f"Result: mean={mean_rps:.1f}  stdev={stdev_rps:.1f}  CV={cv:.1f}%"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 8: Generate report (template — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 8: Generating report...")

    # Save token usage to context for cross-session tracking
    tc = deps.token_counter
    memory.save_context(
        session_id,
        "metric",
        "token_usage",
        json.dumps(
            {
                "input_tokens": tc.input_tokens,
                "output_tokens": tc.output_tokens,
                "total_tokens": tc.total,
                "tool_calls": tc.tool_calls,
                "session_id": session_id,
            }
        ),
        f"Tokens: in={tc.input_tokens:,} out={tc.output_tokens:,} "
        f"total={tc.total:,} calls={tc.tool_calls}",
    )

    memory.update_profile(session_id, status="completed")

    token_history = memory.get_token_history()

    report_path = reporter.generate(
        session_id,
        memory,
        deps.token_counter,
        baselines=baselines,
        finals=finals,
        stability=stability_data,
        throughput=throughput_info,
        token_history=token_history,
    )

    total_improvement = ((best_rps - baseline_rps) / baseline_rps * 100) if baseline_rps else 0.0
    nginx_applied = getattr(diagnosis, "nginx_applied", "unknown")
    system_applied = getattr(diagnosis, "system_applied", "unknown")
    logger.panel(
        "SlayMetricsAgent Complete",
        f"Baseline (small): {baseline_rps:.1f} req/sec\n"
        f"Best (small):     {best_rps:.1f} req/sec\n"
        f"Improvement: {total_improvement:+.1f}%\n"
        f"Nginx applied: {nginx_applied}, System applied: {system_applied}\n"
        f"Tokens used: {deps.token_counter.summary()}\n"
        f"Report: {report_path}\n"
        f"Log: report/log_*_{session_id}.md",
    )
    return report_path


def _build_context_prompt(
    rhel_ver,
    kernel_ver,
    cpu_cores,
    ram_gb,
    checks_summary,
    baselines,
    prior_fixes=None,
) -> str:
    checks_text = "\n".join(checks_summary)

    baselines_text = ""
    for size, data in baselines.items():
        baselines_text += f"  {size}: {data['rps']:.1f} RPS, p99={data.get('p99', 0):.1f}ms\n"

    prior_text = ""
    if prior_fixes:
        prior_text = "Already applied (skip): "
        prior_text += ", ".join(f"{pf['parameter']}" for pf in prior_fixes)
        prior_text += "\n"

    return f"""{rhel_ver} | {kernel_ver} | {cpu_cores} CPU | {ram_gb}GB
Checks: {checks_text}
Baselines:
{baselines_text}
{prior_text}
Inspect, apply proven fixes, benchmark after, save_findings.
"""


HACKATHON_WORKLOADS = ["homepage", "small", "medium", "large", "mixed"]


def _parse_latency(val: str) -> float:
    """Parse wrk latency string like '2.43ms', '496.00us', '1.53s' to milliseconds."""
    if not val:
        return 0.0
    val = val.strip()
    try:
        if val.endswith("us"):
            return float(val[:-2]) / 1000
        elif val.endswith("ms"):
            return float(val[:-2])
        elif val.endswith("s"):
            return float(val[:-1]) * 1000
        else:
            return float(val)
    except ValueError:
        return 0.0


def _run_hackathon_benchmark(deps, cfg, label, session_id):
    """Run the hackathon's official benchmark.sh and parse results."""
    bench_cfg = cfg["service"]["benchmark"]
    script = bench_cfg.get("script", "/root/hackathon-tools/benchmark.sh")
    name = bench_cfg.get("contestant_name", "slaymetrics")
    target_env = bench_cfg.get("target_host_env", "DUT_HOST")
    target_host = os.environ.get(target_env, cfg["target"]["host"])

    contestant = f"{name}-{label}"
    cmd = f"TARGET_HOST={target_host} {script} {contestant}"

    logger.status("benchmark", f"Running: {cmd}")
    logger.status("benchmark", "This takes ~5 minutes (5 workloads)...")

    result = deps.bench.execute(cmd, timeout=600)
    logger.log("benchmark", f"Exit code: {result.exit_code}", "info")

    # Parse results from JSON files
    results = {}
    results_dir = "/root/hackathon-results"
    for workload in HACKATHON_WORKLOADS:
        json_path = f"{results_dir}/{contestant}_{workload}.json"
        r = deps.bench.execute(f"cat {json_path} 2>/dev/null")
        if r.ok and r.stdout.strip():
            try:
                data = json.loads(r.stdout)
                res = data.get("results", {})
                rps = res.get("requests", {}).get("per_sec", 0)
                lat = res.get("latency", {})
                p99 = _parse_latency(lat.get("percentiles", {}).get("p99", "0ms"))
                p50 = _parse_latency(lat.get("percentiles", {}).get("p50", "0ms"))
                results[workload] = {
                    "rps": rps,
                    "p50": p50,
                    "p99": p99,
                    "cpu_pct": 0,
                    "mem_mb": 0,
                    "error_rate": 0,
                }
                logger.benchmark(f"{label} ({workload})", rps, p99, 0, 0)
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.log("benchmark", f"Failed to parse {workload}: {e}", "warn")

    # Save to context
    for workload, data in results.items():
        deps.memory.save_context(
            session_id, "benchmark", f"{label}_{workload}",
            json.dumps(data),
            f"{label} {workload}: {data['rps']:.1f} RPS",
        )

    return results


def _run_wrk2_benchmarks(deps, cfg, label, session_id):
    """Run wrk2 benchmarks for small/medium/large — fallback mode."""
    bench_cfg = cfg["service"]["benchmark"]
    results = {}

    for size in PAYLOAD_SIZES:
        url = bench_cfg.get(f"{size}_file_url")
        if not url:
            continue
        result = deps.adapter.benchmark(
            duration=bench_cfg.get("duration", 30), url=url,
        )
        results[size] = {
            "rps": result.requests_per_sec,
            "p50": result.latency_p50_ms,
            "p99": result.latency_p99_ms,
            "cpu_pct": result.cpu_pct,
            "mem_mb": result.mem_mb,
            "error_rate": result.error_rate,
            "url": url,
        }
        logger.benchmark(
            f"{label} ({size})", result.requests_per_sec,
            result.latency_p99_ms, result.cpu_pct, result.mem_mb,
        )
        deps.memory.save_context(
            session_id, "benchmark", f"{label.lower()}_{size}",
            json.dumps(results[size]),
            f"{label} {size}: {result.requests_per_sec:.1f} RPS",
        )

    return results
