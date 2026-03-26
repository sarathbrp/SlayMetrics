from __future__ import annotations

import json
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

    logger.panel("SlayMetricsAgent",
                 f"Session: {session_id}\n"
                 f"Service: {cfg['service']['name']} on {cfg['target']['host']}")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1: RHEL system checks (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 1: Running RHEL system checks...")
    checks = system_checks.run_all(deps.ssh, cfg["rhel"]["checks"])
    checks_summary = []
    for chk in checks:
        logger.check(chk.name, chk.value, chk.status, chk.recommendation)
        memory.save_context(session_id, "system_check", chk.name,
                            chk.value, chk.recommendation)
        checks_summary.append(f"- {chk.name}: {chk.value[:100]} "
                              f"[{chk.status}] {chk.recommendation[:100]}")

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

    memory.update_profile(session_id,
                          rhel_version=rhel_ver[:64],
                          kernel_version=kernel_ver[:64],
                          cpu_cores=cpu_cores,
                          ram_gb=ram_gb)
    logger.status("system", f"RHEL: {rhel_ver}, Kernel: {kernel_ver}, "
                  f"CPU: {cpu_cores} cores, RAM: {ram_gb} GB")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2: Baseline benchmarks — all 3 sizes (direct wrk2 — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 2: Running baseline benchmarks (small/medium/large)...")
    bench_cfg = cfg["service"]["benchmark"]
    baselines = {}

    for size in PAYLOAD_SIZES:
        url = bench_cfg.get(f"{size}_file_url")
        if not url:
            continue
        result = deps.adapter.benchmark(
            duration=bench_cfg.get("duration", 30), url=url,
        )
        baselines[size] = {
            "rps": result.requests_per_sec,
            "p50": result.latency_p50_ms,
            "p99": result.latency_p99_ms,
            "cpu_pct": result.cpu_pct,
            "mem_mb": result.mem_mb,
            "error_rate": result.error_rate,
            "url": url,
        }
        logger.benchmark(f"Baseline ({size})", result.requests_per_sec,
                         result.latency_p99_ms, result.cpu_pct, result.mem_mb)
        memory.save_context(session_id, "benchmark", f"baseline_{size}",
                            json.dumps(baselines[size]),
                            f"Baseline {size}: {result.requests_per_sec:.1f} RPS")

    baseline_rps = baselines.get("small", {}).get("rps", 0)
    memory.update_profile(session_id, baseline_rps=baseline_rps, best_rps=baseline_rps)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 3: Collect service config (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 3: Collecting service configuration...")
    config_data = deps.adapter.get_config()
    nginx_config = config_data.get("raw", "")
    memory.save_context(session_id, "command_output", "service_config",
                        nginx_config, "current service config")
    logger.status("collector", f"Config: {config_data.get('path', 'unknown')} ({len(nginx_config)} chars)")

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
            prior_fixes.append({
                "parameter": param,
                "value": f.get("after_value", ""),
                "impact": f.get("impact_pct", 0),
            })

    if prior_fixes:
        for pf in prior_fixes:
            logger.status("context", f"  Prior fix: {pf['parameter']} = {pf['value']} "
                          f"({pf['impact']:+.1f}%)")
    else:
        logger.status("context", "No prior fixes found — fresh diagnosis")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 4b: Query RAG knowledge base (vector search — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 4b: Querying knowledge base...")
    rag_queries = [
        "nginx performance tuning sendfile worker_processes tcp_nopush",
        "RHEL sysctl network tuning somaxconn backlog",
        "nginx open_file_cache static file serving optimization",
    ]
    knowledge_chunks = []
    for q in rag_queries:
        results = memory.semantic_search(q, top_k=2)
        for r in results:
            chunk = f"{r.get('parameter', '')}: {r.get('reasoning', '')[:200]}"
            if chunk not in knowledge_chunks:
                knowledge_chunks.append(chunk)
                logger.log("rag", f"  {r.get('parameter', '')[:60]}", "info")

    logger.status("rag", f"Retrieved {len(knowledge_chunks)} knowledge chunks")

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
        nginx_config=nginx_config[:3000],
        knowledge_chunks=knowledge_chunks,
        prior_fixes=prior_fixes,
    )

    logger.log("orchestrator", f"Context prompt: {len(context_prompt)} chars", "info")

    diagnosis = await diagnosis_agent.run(model, deps, context_prompt)

    logger.log("agent", f"Summary: {diagnosis.summary}", "result")
    logger.log("agent", f"Fixes applied: {len(diagnosis.fixes_applied)}", "result")

    # Update best RPS from agent's fixes
    best_rps = baseline_rps
    for fix in diagnosis.fixes_applied:
        after = fix.get("after_rps", 0)
        if after > best_rps:
            best_rps = after
    memory.update_profile(session_id, best_rps=best_rps)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 6: Final benchmarks — all 3 sizes (direct wrk2 — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 6: Running final benchmarks (small/medium/large)...")
    finals = {}

    for size in PAYLOAD_SIZES:
        url = bench_cfg.get(f"{size}_file_url")
        if not url:
            continue
        result = deps.adapter.benchmark(
            duration=bench_cfg.get("duration", 30), url=url,
        )
        finals[size] = {
            "rps": result.requests_per_sec,
            "p50": result.latency_p50_ms,
            "p99": result.latency_p99_ms,
            "cpu_pct": result.cpu_pct,
            "mem_mb": result.mem_mb,
            "url": url,
        }
        logger.benchmark(f"Final ({size})", result.requests_per_sec,
                         result.latency_p99_ms, result.cpu_pct, result.mem_mb)

    # Update best_rps from actual final benchmarks
    final_small_rps = finals.get("small", {}).get("rps", 0)
    if final_small_rps > best_rps:
        best_rps = final_small_rps
    memory.update_profile(session_id, best_rps=best_rps)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 6.5: Capture system throughput limits (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    # Capture system throughput limits silently (for report only)
    throughput_info = {}
    r = ssh.execute("ethtool ens3 2>/dev/null | grep Speed || "
                    "cat /sys/class/net/$(ip route | awk '/default/{print $5}')/speed 2>/dev/null")
    throughput_info["nic_speed"] = r.stdout.strip()[:50]
    r = ssh.execute("dd if=/dev/zero of=/tmp/disktest bs=1M count=256 oflag=direct 2>&1 | tail -1")
    throughput_info["disk_write"] = r.stdout.strip()[:80]
    ssh.execute("rm -f /tmp/disktest")
    for size in PAYLOAD_SIZES:
        data = finals.get(size, {})
        rps = data.get("rps", 0)
        file_sizes = {"small": 1, "medium": 100, "large": 1024}
        throughput_info[f"{size}_throughput_mb_s"] = round(
            (rps * file_sizes.get(size, 1)) / 1024, 1)

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

        logger.step(f"Step 7: Running {total_duration // 60}-minute stability test "
                    f"({num_samples} samples)...")
        samples = []

        for i in range(num_samples):
            result = deps.adapter.benchmark(duration=interval, url=stability_url)
            samples.append(result.requests_per_sec)
            logger.status("stability", f"Sample {i + 1}/{num_samples}: "
                          f"{result.requests_per_sec:.1f} RPS")

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
        memory.save_context(session_id, "benchmark", "stability_test",
                            json.dumps(stability_data),
                            f"Stability: mean={mean_rps:.1f} stdev={stdev_rps:.1f} CV={cv:.1f}%")
        logger.status("stability", f"Result: mean={mean_rps:.1f}  "
                      f"stdev={stdev_rps:.1f}  CV={cv:.1f}%")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 8: Generate report (template — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 8: Generating report...")

    # Save token usage to context for cross-session tracking
    tc = deps.token_counter
    memory.save_context(session_id, "metric", "token_usage",
                        json.dumps({
                            "input_tokens": tc.input_tokens,
                            "output_tokens": tc.output_tokens,
                            "total_tokens": tc.total,
                            "tool_calls": tc.tool_calls,
                            "session_id": session_id,
                        }),
                        f"Tokens: in={tc.input_tokens:,} out={tc.output_tokens:,} "
                        f"total={tc.total:,} calls={tc.tool_calls}")

    memory.update_profile(session_id, status="completed")

    token_history = memory.get_token_history()

    report_path = reporter.generate(
        session_id, memory, deps.token_counter,
        baselines=baselines, finals=finals, stability=stability_data,
        throughput=throughput_info, token_history=token_history,
    )

    total_improvement = (
        ((best_rps - baseline_rps) / baseline_rps * 100)
        if baseline_rps else 0.0
    )
    logger.panel("SlayMetricsAgent Complete",
                 f"Baseline (small): {baseline_rps:.1f} req/sec\n"
                 f"Best (small):     {best_rps:.1f} req/sec\n"
                 f"Improvement: {total_improvement:+.1f}%\n"
                 f"Fixes applied: {len(diagnosis.fixes_applied)}\n"
                 f"Tokens used: {deps.token_counter.summary()}\n"
                 f"Report: {report_path}\n"
                 f"Log: report/log_*_{session_id}.md")
    return report_path


def _build_context_prompt(rhel_ver, kernel_ver, cpu_cores, ram_gb,
                          checks_summary, baselines, nginx_config,
                          knowledge_chunks, prior_fixes=None) -> str:
    checks_text = "\n".join(checks_summary)

    baselines_text = ""
    for size, data in baselines.items():
        baselines_text += (
            f"  {size}: {data['rps']:.1f} RPS, p50={data['p50']:.1f}ms, "
            f"p99={data['p99']:.1f}ms, CPU={data['cpu_pct']:.1f}%, "
            f"error_rate={data['error_rate']:.1f}%\n"
        )

    knowledge_text = "\n".join(knowledge_chunks[:10])

    # Prior fixes section
    prior_text = ""
    if prior_fixes:
        prior_text = "## Already Applied (skip these)\n"
        for pf in prior_fixes:
            prior_text += f"- {pf['parameter']} = {pf['value']} ({pf['impact']:+.1f}%)\n"
        prior_text += "\n"

    return f"""## System
{rhel_ver} | Kernel {kernel_ver} | {cpu_cores} CPU | {ram_gb} GB RAM

## RHEL Checks
{checks_text}

## Baselines
{baselines_text}

{prior_text}## Knowledge
{knowledge_text}

## Nginx Config
```
{nginx_config}
```

Inspect the system, apply all proven fixes from your list that are not already configured, benchmark before and after, save findings.
"""
