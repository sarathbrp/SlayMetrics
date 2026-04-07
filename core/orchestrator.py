from __future__ import annotations

import json
import os
import re
import statistics
from contextlib import nullcontext
from typing import Any

import agents.agent as diagnosis_agent
import core.reporter as reporter
import rhel.system_checks as system_checks
from agents import AgentDeps
from core import log as logger
from telemetry import (
    collect_snapshot,
    persist_sampler_result,
    persist_snapshot,
    start_sampler,
    stop_sampler,
)

PAYLOAD_SIZES = ["small", "medium", "large"]


async def run(model, deps: AgentDeps) -> str:
    """Main orchestration loop. Returns path to final report."""
    cfg = deps.config
    session_id = deps.session_id
    memory = deps.memory
    langfuse = getattr(deps, "langfuse", None)
    max_phase = int((cfg.get("agent") or {}).get("max_phase", 4))

    logger.panel(
        "SlayMetricsAgent",
        f"Session: {session_id}\nService: {cfg['service']['name']} on {cfg['target']['host']}",
    )
    if langfuse:
        langfuse.event(
            "run_started",
            metadata={
                "session_id": session_id,
                "service": cfg["service"]["name"],
                "planner_mode": (cfg.get("agent") or {}).get("planner_mode", "deterministic"),
                "max_phase": max_phase,
            },
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

    # Collect NIC speed and disk throughput for workload-aware reasoning
    nic_speed_raw = ssh.execute(
        "cat /sys/class/net/$(ip route get 1 | awk '{print $5; exit}')/speed 2>/dev/null || echo 0"
    ).stdout.strip()
    nic_speed_mbps = int(nic_speed_raw) if nic_speed_raw.isdigit() else 0
    disk_throughput = ssh.execute(
        "dd if=/dev/zero of=/tmp/slay_disktest bs=1M count=256 oflag=direct 2>&1 | tail -1"
    ).stdout.strip()
    ssh.execute("rm -f /tmp/slay_disktest 2>/dev/null || true")

    memory.update_profile(
        session_id,
        rhel_version=rhel_ver[:64],
        kernel_version=kernel_ver[:64],
        cpu_cores=cpu_cores,
        ram_gb=ram_gb,
    )
    deps.system_fingerprint = (
        f"{rhel_ver}, Kernel: {kernel_ver}, CPU: {cpu_cores} cores, RAM: {ram_gb} GB"
    )
    logger.status("system", f"RHEL: {deps.system_fingerprint}")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1.7: Pre-flight validation (LLM-assisted)
    # Verify DUT serves all workloads correctly before benchmarking.
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 1.7: Pre-flight validation — verifying DUT serves all workloads...")
    preflight_result = await diagnosis_agent.run_preflight(model, deps)
    preflight_status = preflight_result.get("status", "unknown")
    if preflight_status == "ok":
        logger.status("preflight", "All workloads verified — proceeding to benchmark")
    elif preflight_status == "fixed":
        logger.status(
            "preflight",
            f"Fixed {len(preflight_result.get('problems', []))} issues — proceeding",
        )
    else:
        remaining = preflight_result.get("problems", [])
        logger.status(
            "preflight",
            f"WARNING: {len(remaining)} issues remain — baselines may be unreliable",
        )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2: Baseline benchmarks (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    bench_cfg = cfg["service"]["benchmark"]
    bench_tool = bench_cfg.get("tool", "wrk2")
    baseline_mode = str((cfg.get("agent") or {}).get("baseline_mode", "fresh")).strip().lower()
    reused = _load_reusable_baseline(deps) if baseline_mode == "reuse" else None

    if reused:
        logger.step("Step 2: Reusing stored baseline benchmark...")
        baselines = reused["baselines"]
        telemetry_entries = reused["telemetry_entries"]
        logger.status(
            "benchmark",
            "Reused baseline from session "
            f"{reused['source_session_id']} for host {cfg['target']['host']}",
        )
        for workload in HACKATHON_WORKLOADS:
            data = baselines.get(workload, {})
            if data:
                logger.benchmark(
                    f"baseline ({workload})", data.get("rps", 0), data.get("p99", 0), 0, 0
                )
    elif bench_tool == "hackathon":
        logger.step("Step 2: Running hackathon baseline benchmark...")
        with (
            langfuse.span(
                "baseline_benchmark",
                input={"scope": "baseline", "tool": bench_tool},
                metadata={"session_id": session_id},
            )
            if langfuse
            else nullcontext()
        ):
            baselines = _run_benchmark_window_with_telemetry(
                deps,
                scope="baseline",
                runner=lambda: _run_hackathon_benchmark(deps, cfg, "baseline", session_id),
            )
        telemetry_entries = memory.get_contexts(session_id, "telemetry", limit=8)
    else:
        logger.step("Step 2: Running baseline benchmarks (small/medium/large)...")
        with (
            langfuse.span(
                "baseline_benchmark",
                input={"scope": "baseline", "tool": bench_tool},
                metadata={"session_id": session_id},
            )
            if langfuse
            else nullcontext()
        ):
            baselines = _run_benchmark_window_with_telemetry(
                deps,
                scope="baseline",
                runner=lambda: _run_wrk2_benchmarks(deps, cfg, "Baseline", session_id),
            )
        telemetry_entries = memory.get_contexts(session_id, "telemetry", limit=8)

    baseline_rps = baselines.get("small", {}).get(
        "rps", baselines.get("homepage", {}).get("rps", 0)
    )
    memory.update_profile(
        session_id,
        baseline_rps=baseline_rps,
        best_rps=baseline_rps,
    )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2.5: Aggregate benchmark evidence from bench + DUT telemetry
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 2.5: Aggregating benchmark evidence...")
    benchmark_evidence = _build_benchmark_evidence(baselines, telemetry_entries)
    if langfuse:
        langfuse.event(
            "benchmark_evidence",
            input={"baselines": baselines},
            output=benchmark_evidence,
            metadata={"session_id": session_id},
        )
    memory.save_context(
        session_id,
        "command_output",
        "benchmark_evidence",
        json.dumps(benchmark_evidence),
        benchmark_evidence["summary"],
    )
    logger.status("evidence", benchmark_evidence["summary"])

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 3: Collect service config (direct — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 3: Collecting service configuration...")
    config_data = deps.adapter.get_config()
    service_config = config_data.get("raw", "")
    memory.save_context(
        session_id, "command_output", "service_config", service_config, "current service config"
    )
    logger.status(
        "collector", f"Config: {config_data.get('path', 'unknown')} ({len(service_config)} chars)"
    )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 4a: Check context — previously applied fixes (database — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 4a: Checking for previously applied fixes...")
    # Only use current session's fixes — cross-session fixes may be stale
    # (system could have been reset between runs)
    past_fixes = list(memory.get_facts(session_id, type="fix") or [])

    prior_fixes = []
    seen_params: set[str] = set()
    for f in past_fixes:
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
            impact = pf.get("impact")
            impact_str = f"{impact:+.1f}%" if impact is not None else "n/a"
            logger.status(
                "context",
                f"  Prior fix: {pf.get('parameter', '?')} = {pf.get('value', '?')} ({impact_str})",
            )
    else:
        logger.status("context", "No prior fixes found — fresh diagnosis")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 4b: Assemble diagnosis evidence bundle
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 4b: Assembling diagnosis evidence...")
    benchmark_evidence_text = _build_benchmark_evidence_text(
        benchmark_evidence,
        nic_speed_mbps=nic_speed_mbps,
        disk_throughput=disk_throughput,
    )
    logger.status("evidence", "Benchmark and telemetry evidence assembled for RCA")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 5: RCA + recommendations (ITERATION LOOP)
    # ══════════════════════════════════════════════════════════════════════════
    max_iterations = int((cfg.get("agent") or {}).get("max_iterations", 3))
    iteration_feedback = ""
    diagnosis = None
    iteration_finals: dict[str, Any] = {}
    best_iteration_finals: dict[str, Any] = {}
    best_iteration_rps: float = 0.0

    for iteration in range(1, max_iterations + 1):
        logger.step(
            f"Step 5: Iteration {iteration}/{max_iterations} — RCA and recommendation planning..."
        )
        # Pass iteration number to agent for per-iteration hypothesis files
        deps.iteration = iteration  # type: ignore[attr-defined]

        context_prompt = _build_context_prompt(
            rhel_ver=rhel_ver,
            kernel_ver=kernel_ver,
            cpu_cores=cpu_cores,
            ram_gb=ram_gb,
            checks_summary=checks_summary,
            benchmark_evidence_text=benchmark_evidence_text,
            prior_fixes=prior_fixes,
        )
        if iteration_feedback:
            context_prompt += f"\n{iteration_feedback}\n"

        logger.log(
            "orchestrator",
            f"Context prompt: {len(context_prompt)} chars (iteration {iteration})",
            "info",
        )

        # Snapshot fact count to isolate this iteration's applied facts
        pre_iteration_facts = memory.get_facts(session_id, type="fix")
        pre_iteration_fact_count = len(pre_iteration_facts)

        with (
            langfuse.span(
                "diagnosis_planning",
                input={
                    "context_prompt_length": len(context_prompt),
                    "planner_mode": (cfg.get("agent") or {}).get("planner_mode", "debate"),
                    "iteration": iteration,
                },
                metadata={"session_id": session_id},
            )
            if langfuse
            else nullcontext()
        ):
            diagnosis = await diagnosis_agent.run(model, deps, context_prompt)

        # Check if any changes were applied
        service_applied = getattr(diagnosis, "service_applied", False)
        system_applied = getattr(diagnosis, "system_applied", False)
        if not service_applied and not system_applied:
            decision = "No changes applied — stopping"
            logger.status("iteration", f"Iteration {iteration}: {decision}")
            diagnosis_agent.save_iteration_summary(
                deps,
                iteration=iteration,
                baselines=baselines,
                results=iteration_finals or baselines,
                regressions=[],
                decision=decision,
                diagnosis=diagnosis,
            )
            break

        # Benchmark ALL workloads to check for regressions
        if bench_tool == "hackathon":
            logger.step(f"Step 5.{iteration}: Running post-iteration benchmark (all workloads)...")
            iteration_finals = _run_hackathon_benchmark(deps, cfg, f"iter{iteration}", session_id)

        # Check exit criteria: all workloads within 1% of baseline
        healthy_floor = cfg.get("service", {}).get("benchmark", {}).get("healthy_floor_rps")
        should_stop, regressions = _check_iteration_exit(baselines, iteration_finals, healthy_floor)

        # Check if this iteration degraded vs best so far — rollback if so
        current_small_rps = float(
            iteration_finals.get("small", {}).get("rps", 0)
            or iteration_finals.get("homepage", {}).get("rps", 0)
            or 0
        )
        iteration_degraded = False
        if best_iteration_rps and current_small_rps < best_iteration_rps * 0.99:
            iteration_degraded = True
        elif not best_iteration_rps:
            # First iteration — compare against baseline
            if current_small_rps < baseline_rps * 0.99:
                iteration_degraded = True

        if iteration_degraded and getattr(diagnosis, "system_applied", False):
            # Revert this iteration's sysctl params
            # get_facts returns ORDER BY created_at — safe to slice by count
            all_facts = memory.get_facts(session_id, type="fix")
            iteration_facts = all_facts[pre_iteration_fact_count:]
            reverted = _revert_iteration_params(deps, iteration_facts)
            if reverted:
                logger.status(
                    "rollback",
                    f"Iteration {iteration}: Reverted {len(reverted)} sysctl params "
                    f"(small {current_small_rps:.0f} < best {best_iteration_rps:.0f})",
                )
                _mark_facts_reverted(
                    deps, session_id, iteration_facts, iteration, memory
                )
        elif iteration_degraded:
            # Degraded but no system params applied — nothing to revert
            logger.status(
                "iteration",
                f"Iteration {iteration}: Degraded (small {current_small_rps:.0f} "
                f"< best {best_iteration_rps or baseline_rps:.0f}) but no sysctl "
                f"params to revert",
            )
        else:
            best_iteration_finals = dict(iteration_finals)
            best_iteration_rps = current_small_rps

        if should_stop:
            decision = f"All workloads OK after iteration {iteration} — continuing to find further gains"
            logger.status("iteration", f"Iteration {iteration}: {decision}")
            diagnosis_agent.save_iteration_summary(
                deps,
                iteration=iteration,
                baselines=baselines,
                results=iteration_finals,
                regressions=regressions,
                decision=decision,
                diagnosis=diagnosis,
            )
            # Don't break — always run max_iterations to explore optimization groups

        if iteration >= max_iterations:
            decision = f"Max iterations ({max_iterations}) reached — stopping"
            logger.status("iteration", f"Iteration {iteration}: {decision}")
            diagnosis_agent.save_iteration_summary(
                deps,
                iteration=iteration,
                baselines=baselines,
                results=iteration_finals,
                regressions=regressions,
                decision=decision,
                diagnosis=diagnosis,
            )
            break

        # Continuing to next iteration
        if not should_stop:
            decision = f"{len(regressions)} regressions — continuing to iteration {iteration + 1}"
            logger.status("iteration", f"Iteration {iteration}: {decision}")
            diagnosis_agent.save_iteration_summary(
                deps,
                iteration=iteration,
                baselines=baselines,
                results=iteration_finals,
                regressions=regressions,
                decision=decision,
                diagnosis=diagnosis,
            )

        iteration_feedback = _build_iteration_feedback(
            iteration=iteration,
            baselines=baselines,
            current_results=iteration_finals,
            regressions=regressions,
        )

    notes = getattr(diagnosis, "notes", getattr(diagnosis, "summary", ""))
    logger.log("agent", f"Summary: {notes}", "result")
    if langfuse:
        langfuse.event(
            "diagnosis_completed",
            output={
                "notes": notes,
                "service_applied": getattr(diagnosis, "service_applied", False),
                "system_applied": getattr(diagnosis, "system_applied", False),
                "recommendation_count": len(getattr(diagnosis, "recommendations", []) or []),
                "rca_count": len(getattr(diagnosis, "rca_records", []) or []),
            },
            metadata={"session_id": session_id},
        )

    if max_phase <= 3:
        logger.status("main", "Stopping after Phase 3 planning (RCA + recommendations)")
        memory.update_profile(session_id, status="completed")
        _save_token_usage(memory, session_id, deps.token_counter)
        token_history = memory.get_token_history()
        report_path = reporter.generate(
            session_id,
            memory,
            deps.token_counter,
            baselines=baselines,
            finals=None,
            stability=None,
            throughput=None,
            token_history=token_history,
        )
        logger.panel(
            "SlayMetricsAgent Planning Complete",
            f"Baseline (small): {baseline_rps:.1f} req/sec\n"
            f"Phase cutoff: {max_phase}\n"
            f"Recommendations generated: {len(getattr(diagnosis, 'recommendations', []) or [])}\n"
            f"Tokens used: {deps.token_counter.summary()}\n"
            f"Report: {report_path}\n"
            f"Log: report/log_*_{session_id}.md",
        )
        return report_path

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
    # Reuse last iteration results if available; otherwise run fresh.
    # ══════════════════════════════════════════════════════════════════════════
    if iteration_finals:
        logger.step("Step 6: Using best iteration benchmark as final results...")
        finals = best_iteration_finals if best_iteration_finals else iteration_finals
        # Run telemetry capture for the final state
        _capture_telemetry(deps, scope="final", source="post")
    elif bench_tool == "hackathon":
        logger.step("Step 6: Running hackathon final benchmark...")
        with (
            langfuse.span(
                "final_benchmark",
                input={"scope": "final", "tool": bench_tool},
                metadata={"session_id": session_id},
            )
            if langfuse
            else nullcontext()
        ):
            finals = _run_benchmark_window_with_telemetry(
                deps,
                scope="final",
                runner=lambda: _run_hackathon_benchmark(deps, cfg, "tuned", session_id),
            )
    else:
        logger.step("Step 6: Running final benchmarks (small/medium/large)...")
        with (
            langfuse.span(
                "final_benchmark",
                input={"scope": "final", "tool": bench_tool},
                metadata={"session_id": session_id},
            )
            if langfuse
            else nullcontext()
        ):
            finals = _run_benchmark_window_with_telemetry(
                deps,
                scope="final",
                runner=lambda: _run_wrk2_benchmarks(deps, cfg, "Final", session_id),
            )

    # Update best_rps from actual final benchmarks
    final_small_rps = finals.get("small", {}).get("rps", finals.get("homepage", {}).get("rps", 0))
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

    _save_token_usage(memory, session_id, deps.token_counter)

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
    service_applied = getattr(diagnosis, "service_applied", "unknown")
    system_applied = getattr(diagnosis, "system_applied", "unknown")
    hypothesis_path = f"hypothesis/{session_id}/"
    log_file_path = logger.get_log_path()
    logger.panel(
        "SlayMetricsAgent Complete",
        f"Session: {session_id}\n"
        f"Baseline (small): {baseline_rps:.1f} req/sec\n"
        f"Best (small):     {best_rps:.1f} req/sec\n"
        f"Improvement: {total_improvement:+.1f}%\n"
        f"Service applied: {service_applied}, System applied: {system_applied}\n"
        f"Tokens used: {deps.token_counter.summary()}\n"
        f"Report: {report_path}\n"
        f"Log: {log_file_path}\n"
        f"Hypotheses: {hypothesis_path}",
    )
    if langfuse:
        langfuse.event(
            "run_completed",
            output={
                "report_path": report_path,
                "baseline_rps": baseline_rps,
                "best_rps": best_rps,
                "improvement_pct": total_improvement,
            },
            metadata={"session_id": session_id},
        )
    return report_path


def _save_token_usage(memory, session_id: str, token_counter) -> None:
    tc = token_counter
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


def _build_context_prompt(
    rhel_ver,
    kernel_ver,
    cpu_cores,
    ram_gb,
    checks_summary,
    benchmark_evidence_text,
    prior_fixes=None,
) -> str:
    checks_text = "\n".join(checks_summary)

    prior_text = ""
    if prior_fixes:
        prior_text = "Already applied (skip): "
        prior_text += ", ".join(f"{pf['parameter']}" for pf in prior_fixes)
        prior_text += "\n"

    return f"""{rhel_ver} | {kernel_ver} | {cpu_cores} CPU | {ram_gb}GB
Checks: {checks_text}
Benchmark Evidence:
{benchmark_evidence_text}
{prior_text}
Inspect, apply proven fixes, benchmark after, save_findings.
"""


def _capture_telemetry(deps: AgentDeps, *, scope: str, source: str) -> None:
    snapshot = collect_snapshot(
        deps.ssh,
        scope=scope,
        host=deps.config["target"]["host"],
        source=source,
    )
    persist_snapshot(deps.memory, deps.session_id, snapshot)


def _run_benchmark_window_with_telemetry(deps: AgentDeps, *, scope: str, runner):
    _capture_telemetry(deps, scope=scope, source="pre")
    start_sampler(deps.ssh, scope=scope, host=deps.config["target"]["host"])
    try:
        return runner()
    finally:
        sampler_result = stop_sampler(deps.ssh, scope=scope, host=deps.config["target"]["host"])
        persist_sampler_result(deps.memory, deps.session_id, sampler_result)
        summary = sampler_result.get("summary", {})
        last_sample = summary.get("last_sample", {})
        if last_sample:
            persist_snapshot(
                deps.memory,
                deps.session_id,
                {
                    "scope": scope,
                    "source": "post",
                    "host": deps.config["target"]["host"],
                    "summary": last_sample,
                    "sections": {},
                },
            )


def _build_telemetry_summary(entries: list[dict]) -> str:
    lines: list[str] = []
    for entry in entries[:4]:
        source = entry.get("source", "telemetry")
        try:
            payload = json.loads(entry.get("content", "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        summary = payload.get("summary", {})
        if source.endswith(":series"):
            lines.append(
                f"- {source}: samples={summary.get('sample_count', 0)}, "
                f"duration={summary.get('duration_sec', 0)}s, "
                f"runq_avg={summary.get('run_queue_avg', 0)}, "
                f"runq_max={summary.get('run_queue_max', 0)}, "
                f"rx_drop_delta={summary.get('rx_drop_delta', 0)}, "
                f"rx_drop_rate={summary.get('rx_drop_rate_per_sec', 0)}"
            )
            continue
        lines.append(
            f"- {source}: workers={summary.get('service_worker_count', 0)}, "
            f"cores={summary.get('service_worker_cores', [])}, "
            f"somaxconn={summary.get('somaxconn', 'unknown')}, "
            f"syn_backlog={summary.get('tcp_max_syn_backlog', 'unknown')}, "
            f"ports={summary.get('ip_local_port_range', 'unknown')}, "
            f"rx_drop={summary.get('rx_drop_total', 'unknown')}, "
            f"tx_drop={summary.get('tx_drop_total', 'unknown')}, "
            f"estab={summary.get('tcp_established', 'unknown')}"
        )
    return "\n".join(lines)


_SYSCTL_PREFIXES = ("net.", "vm.", "kernel.", "fs.")


_SAFE_SYSCTL_VALUE = re.compile(r"^[\w./:\- ]+$")


def _revert_iteration_params(
    deps: Any, iteration_facts: list[dict]
) -> list[dict[str, str]]:
    """Revert sysctl params applied in this iteration using before_value from saved facts."""
    sysctl_reverts: list[tuple[str, str]] = []
    for fact in iteration_facts:
        param = fact.get("parameter", "")
        before = fact.get("before_value", "")
        if not before or not any(param.startswith(p) for p in _SYSCTL_PREFIXES):
            continue
        # Sanitize values to prevent command injection
        if not _SAFE_SYSCTL_VALUE.match(param) or not _SAFE_SYSCTL_VALUE.match(before):
            logger.log(
                "rollback",
                f"Skipping revert of {param!r} — unsafe characters in param or value",
                "warning",
            )
            continue
        # Strip kernel. prefix if present (e.g. kernel.net.core.somaxconn → net.core.somaxconn)
        sysctl_param = param
        if param.startswith("kernel.") and any(
            param[7:].startswith(p) for p in ("net.", "vm.", "fs.")
        ):
            sysctl_param = param[7:]
        sysctl_reverts.append((sysctl_param, before))

    if not sysctl_reverts:
        return []

    script_lines = [f"sysctl -w {p}={v}" for p, v in sysctl_reverts]
    script = " && ".join(script_lines)
    result = deps.ssh.execute(script)

    if not getattr(result, "ok", True):
        logger.log(
            "rollback",
            f"SSH revert failed: {getattr(result, 'stderr', '') or getattr(result, 'output', '')}",
            "error",
        )
        return []

    return [{"parameter": p, "reverted_to": v} for p, v in sysctl_reverts]


def _mark_facts_reverted(
    deps: Any,
    session_id: str,
    iteration_facts: list[dict],
    iteration: int,
    memory: Any,
) -> None:
    """Mark this iteration's applied facts as reverted in the knowledge store."""
    for fact in iteration_facts:
        param = fact.get("parameter", "")
        if not any(param.startswith(p) for p in _SYSCTL_PREFIXES):
            continue
        if not fact.get("before_value"):
            continue
        memory.save_fact(
            session_id=session_id,
            type="fix",
            parameter=param,
            reasoning=f"Reverted: iteration {iteration} caused performance regression",
            before_value=fact.get("after_value", ""),
            after_value=fact.get("before_value", ""),
            status="reverted",
        )


def _check_iteration_exit(
    baselines: dict[str, Any],
    current_results: dict[str, Any],
    healthy_floor: dict[str, float] | None = None,
) -> tuple[bool, list[str]]:
    """Check if all workloads are within 1% of baseline. Returns (should_stop, regressions).

    If healthy_floor is provided, a workload that meets the floor RPS is never
    flagged as a regression — this handles cases where the degraded baseline is
    artificially inflated (e.g. limit_rate only kicks in after 1MB).
    """
    regressions: list[str] = []
    floor = healthy_floor or {}
    for workload in ("small", "medium", "large"):
        baseline_rps = float(baselines.get(workload, {}).get("rps", 0) or 0)
        current_rps = float(current_results.get(workload, {}).get("rps", 0) or 0)
        if not baseline_rps:
            continue
        # If current RPS meets the healthy floor, it's not a regression
        floor_rps = float(floor.get(workload, 0) or 0)
        if floor_rps and current_rps >= floor_rps:
            continue
        if current_rps < baseline_rps * 0.99:
            pct = ((current_rps - baseline_rps) / baseline_rps) * 100
            regressions.append(
                f"{workload}: {current_rps:.0f} vs baseline {baseline_rps:.0f} ({pct:+.1f}%)"
            )
    return (len(regressions) == 0, regressions)


def _build_iteration_feedback(
    *,
    iteration: int,
    baselines: dict[str, Any],
    current_results: dict[str, Any],
    regressions: list[str],
) -> str:
    """Build feedback for the next iteration's context prompt."""
    lines = [
        f"\n--- ITERATION {iteration} RESULTS ---",
        "Per-workload comparison against baseline:",
    ]
    for workload in ("homepage", "small", "medium", "large", "mixed"):
        b_rps = float(baselines.get(workload, {}).get("rps", 0) or 0)
        c_rps = float(current_results.get(workload, {}).get("rps", 0) or 0)
        c_p99 = float(current_results.get(workload, {}).get("p99", 0) or 0)
        if not b_rps:
            continue
        pct = ((c_rps - b_rps) / b_rps) * 100
        status = "OK" if c_rps >= b_rps * 0.99 else "REGRESSED"
        lines.append(
            f"  {workload:10s}: baseline={b_rps:.0f} → now={c_rps:.0f} RPS"
            f" ({pct:+.1f}%) p99={c_p99:.1f}ms [{status}]"
        )
    if regressions:
        lines.append("")
        lines.append("REGRESSIONS DETECTED:")
        for r in regressions:
            lines.append(f"  - {r}")
        lines.append("")
        lines.append(
            "Diagnose what caused the regressions above. "
            "Identify which change from the previous iteration hurt "
            "medium/large workloads and revert or adjust ONLY that change. "
            "Do NOT revert changes that improved other workloads."
        )
    return "\n".join(lines)


def _build_benchmark_evidence(baselines: dict, telemetry_entries: list[dict]) -> dict[str, Any]:
    baseline_small = baselines.get("small", {})
    baseline_homepage = baselines.get("homepage", {})
    series = _find_telemetry_entry(telemetry_entries, "baseline:series")
    pre = _find_telemetry_entry(telemetry_entries, "baseline:pre")
    post = _find_telemetry_entry(telemetry_entries, "baseline:post")
    series_summary = series.get("summary", {}) if series else {}
    pre_summary = (
        series.get("first_sample", {})
        if series.get("first_sample")
        else (pre.get("summary", {}) if pre else {})
    )
    post_summary = (
        series.get("last_sample", {})
        if series.get("last_sample")
        else (post.get("summary", {}) if post else {})
    )
    rx_pre = _safe_int(pre_summary.get("rx_drop_total"))
    rx_post = _safe_int(post_summary.get("rx_drop_total"))
    tx_pre = _safe_int(pre_summary.get("tx_drop_total"))
    tx_post = _safe_int(post_summary.get("tx_drop_total"))
    # Include ALL workload baselines for per-size reasoning
    baseline_medium = baselines.get("medium", {})
    baseline_large = baselines.get("large", {})
    baseline_mixed = baselines.get("mixed", {})
    evidence = {
        "baseline_small_rps": baseline_small.get("rps", 0.0),
        "baseline_small_p99_ms": baseline_small.get("p99", 0.0),
        "baseline_homepage_rps": baseline_homepage.get("rps", 0.0),
        "baseline_homepage_p99_ms": baseline_homepage.get("p99", 0.0),
        "baseline_medium_rps": baseline_medium.get("rps", 0.0),
        "baseline_medium_p99_ms": baseline_medium.get("p99", 0.0),
        "baseline_large_rps": baseline_large.get("rps", 0.0),
        "baseline_large_p99_ms": baseline_large.get("p99", 0.0),
        "baseline_mixed_rps": baseline_mixed.get("rps", 0.0),
        "baseline_mixed_p99_ms": baseline_mixed.get("p99", 0.0),
        "baseline_pre_workers": pre_summary.get("service_worker_count", 0),
        "baseline_post_workers": post_summary.get("service_worker_count", 0),
        "somaxconn": post_summary.get("somaxconn", pre_summary.get("somaxconn", "unknown")),
        "tcp_max_syn_backlog": post_summary.get(
            "tcp_max_syn_backlog", pre_summary.get("tcp_max_syn_backlog", "unknown")
        ),
        "rx_drop_delta": max(rx_post - rx_pre, 0),
        "tx_drop_delta": max(tx_post - tx_pre, 0),
        "tcp_established_post": post_summary.get("tcp_established", "unknown"),
        "telemetry_sample_count": series_summary.get("sample_count", 0),
        "telemetry_duration_sec": series_summary.get("duration_sec", 0),
        "run_queue_avg": series_summary.get("run_queue_avg", 0),
        "run_queue_max": series_summary.get("run_queue_max", 0),
        "worker_core_spread_max": series_summary.get("worker_core_spread_max", 0),
        "rx_drop_rate_per_sec": series_summary.get("rx_drop_rate_per_sec", 0),
    }
    evidence["summary"] = (
        f"Bench baseline small={evidence['baseline_small_rps']:.1f} RPS "
        f"p99={evidence['baseline_small_p99_ms']:.1f}ms; "
        f"homepage={evidence['baseline_homepage_rps']:.1f} RPS; "
        f"workers={evidence['baseline_post_workers']}; "
        f"somaxconn={evidence['somaxconn']}; "
        f"samples={evidence['telemetry_sample_count']}; "
        f"rx_drop_delta={evidence['rx_drop_delta']}"
    )
    return evidence


def _build_benchmark_evidence_text(
    evidence: dict[str, Any],
    nic_speed_mbps: int = 0,
    disk_throughput: str = "",
) -> str:
    lines = [
        "Baseline benchmarks per workload:",
        f"  homepage: {float(evidence.get('baseline_homepage_rps', 0.0)):.1f} RPS, "
        f"p99={float(evidence.get('baseline_homepage_p99_ms', 0.0)):.1f}ms",
        f"  small:    {float(evidence.get('baseline_small_rps', 0.0)):.1f} RPS, "
        f"p99={float(evidence.get('baseline_small_p99_ms', 0.0)):.1f}ms",
        f"  medium:   {float(evidence.get('baseline_medium_rps', 0.0)):.1f} RPS, "
        f"p99={float(evidence.get('baseline_medium_p99_ms', 0.0)):.1f}ms",
        f"  large:    {float(evidence.get('baseline_large_rps', 0.0)):.1f} RPS, "
        f"p99={float(evidence.get('baseline_large_p99_ms', 0.0)):.1f}ms",
        f"  mixed:    {float(evidence.get('baseline_mixed_rps', 0.0)):.1f} RPS, "
        f"p99={float(evidence.get('baseline_mixed_p99_ms', 0.0)):.1f}ms",
        "",
        "Hardware constraints:",
    ]
    if nic_speed_mbps:
        max_gbps = nic_speed_mbps / 1000
        lines.append(
            f"  NIC speed: {nic_speed_mbps} Mbps ({max_gbps:.0f} Gbps"
            f" = ~{nic_speed_mbps // 8} MB/s max throughput)"
        )
    if disk_throughput:
        lines.append(f"  Disk throughput: {disk_throughput}")
    lines.extend(
        [
            "",
            "Telemetry during baseline benchmark:",
            f"  workers={evidence.get('baseline_post_workers', 0)}, "
            f"somaxconn={evidence.get('somaxconn', 'unknown')}, "
            f"syn_backlog={evidence.get('tcp_max_syn_backlog', 'unknown')}",
            f"  runq_avg={evidence.get('run_queue_avg', 0)}, "
            f"runq_max={evidence.get('run_queue_max', 0)}, "
            f"rx_drop_delta={evidence.get('rx_drop_delta', 0)}, "
            f"rx_drop_rate={evidence.get('rx_drop_rate_per_sec', 0)}",
            "",
            "IMPORTANT: Tuning changes must not regress ANY workload size.",
            "Large files are NIC/disk-bound; changes like 'aio threads' can "
            "conflict with sendfile and cause severe regression on large files.",
        ]
    )
    return "\n".join(lines)


def _find_telemetry_entry(entries: list[dict], source: str) -> dict:
    for entry in entries:
        if entry.get("source") != source:
            continue
        try:
            payload = json.loads(entry.get("content", "{}"))
        except (TypeError, json.JSONDecodeError):
            return {}
        if isinstance(payload, dict):
            return payload
    return {}


def _safe_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return 0


def _load_reusable_baseline(deps: AgentDeps) -> dict[str, Any] | None:
    host = deps.config["target"]["host"]
    source_session_id = deps.memory.get_latest_session_for_host(
        host, exclude_session_id=deps.session_id
    )
    if not source_session_id:
        return None

    baselines: dict[str, dict] = {}
    for workload in HACKATHON_WORKLOADS:
        rows = deps.memory.get_contexts(
            source_session_id,
            type="benchmark",
            source_prefix=f"baseline_{workload}",
            limit=1,
        )
        if not rows:
            continue
        content = rows[0].get("content", "")
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            baselines[workload] = parsed

    telemetry_entries = deps.memory.get_contexts(source_session_id, "telemetry", limit=8)
    if not baselines:
        return None
    return {
        "source_session_id": source_session_id,
        "baselines": baselines,
        "telemetry_entries": telemetry_entries,
    }


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
    results_dir = "/root/hackathon-results"

    # Remove stale JSON files to prevent reading cached results from prior runs
    for wl in HACKATHON_WORKLOADS:
        deps.bench.execute(f"rm -f {results_dir}/{contestant}_{wl}.json")

    cmd = f"TARGET_HOST={target_host} {script} {contestant}"

    logger.status("benchmark", f"Running: {cmd}")
    logger.status("benchmark", "This takes ~5 minutes (5 workloads)...")

    result = deps.bench.execute(cmd, timeout=600)
    logger.log("benchmark", f"Exit code: {result.exit_code}", "info")

    # Parse results from JSON files
    results = {}
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
            session_id,
            "benchmark",
            f"{label}_{workload}",
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
            duration=bench_cfg.get("duration", 30),
            url=url,
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
            f"{label} ({size})",
            result.requests_per_sec,
            result.latency_p99_ms,
            result.cpu_pct,
            result.mem_mb,
        )
        deps.memory.save_context(
            session_id,
            "benchmark",
            f"{label.lower()}_{size}",
            json.dumps(results[size]),
            f"{label} {size}: {result.requests_per_sec:.1f} RPS",
        )

    return results
