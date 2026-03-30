from __future__ import annotations

import json
import os
import statistics
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import agents.agent as diagnosis_agent
import core.reporter as reporter
import rhel.system_checks as system_checks
from agents import AgentDeps
from core import log as logger
from core.lessons import (
    check_leaderboard,
    get_best_run_params,
    get_prior_knowledge_text,
    get_ranked_optimization_groups,
    get_top_runs,
    merge_targets,
)
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
    # STEP 1.6: Load lessons learned — merge proven params from best run
    # ══════════════════════════════════════════════════════════════════════════
    top_runs = get_top_runs(memory)
    prior_knowledge_text = ""
    if top_runs:
        best = top_runs[0]
        logger.status(
            "lessons",
            f"Best prior run: {best['session_id']} "
            f"(small={best['small_rps']:.0f} RPS, {best['tokens']} tokens)",
        )
        prior_knowledge_text = get_prior_knowledge_text(memory)
        proven = get_best_run_params(memory)
        if proven:
            tuning = cfg.get("tuning") or {}
            config_targets = {
                "webserver": tuning.get("webserver_targets") or {},
                "kernel": tuning.get("kernel_targets") or {},
                "resource_limits": tuning.get("resource_limits_targets") or {},
                "network": tuning.get("network_targets") or {},
                "storage": tuning.get("storage_targets") or {},
            }
            merged = merge_targets(config_targets, proven)
            # Write merged targets back into config so all downstream code uses them
            for cat, key in (
                ("webserver", "webserver_targets"),
                ("kernel", "kernel_targets"),
                ("resource_limits", "resource_limits_targets"),
                ("network", "network_targets"),
                ("storage", "storage_targets"),
            ):
                if cat in merged:
                    tuning[key] = merged[cat]
            logger.status("lessons", f"Merged {len(proven)} proven params into targets")
    else:
        logger.status("lessons", "No prior qualifying runs — using config.yaml defaults")

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
    optimization_cfg = ((cfg.get("agent") or {}).get("optimization") or {}).copy()
    optimization_enabled = bool(optimization_cfg.get("enabled", False))
    optimization_top_n = int(optimization_cfg.get("top_runs", 3) or 3)
    optimization_min_gain_pct = float(optimization_cfg.get("min_small_gain_pct", 1.0) or 1.0)
    optimization_gap_pct = float(optimization_cfg.get("leaderboard_gap_pct", 3.0) or 3.0)
    iteration_feedback = ""
    diagnosis: Any = None
    iteration_finals: dict[str, Any] = {}
    healthy_results: dict[str, Any] = {}
    final_results: dict[str, Any] = {}
    best_results = dict(baselines)
    best_iteration = 0
    rejected_optimization_groups: set[str] = set()
    in_optimization_mode = False
    has_top_runs = bool(top_runs)

    for iteration in range(1, max_iterations + 1):
        deps.iteration = iteration  # type: ignore[attr-defined]

        if in_optimization_mode:
            logger.step(f"Step 5: Iteration {iteration}/{max_iterations} — ranked optimization...")
            current_state = _collect_current_state(deps)
            ranked_groups = [
                group
                for group in get_ranked_optimization_groups(
                    memory,
                    current_state,
                    top_n=optimization_top_n,
                )
                if group["name"] not in rejected_optimization_groups
            ]
            _save_optimization_considered(memory, session_id, iteration, ranked_groups)
            if not ranked_groups:
                decision = "No ranked optimization groups remain — stopping"
                logger.status("optimization", decision)
                diagnosis_agent.save_iteration_summary(
                    deps,
                    iteration=iteration,
                    baselines=baselines,
                    results=healthy_results or final_results or baselines,
                    regressions=[],
                    decision=decision,
                    diagnosis=SimpleNamespace(
                        nginx_applied=False,
                        system_applied=False,
                        recommendations=[],
                    ),
                )
                break

            candidate = ranked_groups[0]
            candidate_params = _format_group_changes(candidate.get("changes", {}) or {})
            logger.status(
                "optimization",
                f"Selected group {candidate['name']} score={candidate['score']:.2f} "
                f"params={candidate_params}",
            )
            snapshot = _snapshot_optimization_state(deps, candidate)
            apply_result = _apply_optimization_group(deps, candidate)
            total_changes = sum(len(values) for values in candidate.get("changes", {}).values())
            diagnosis = SimpleNamespace(
                nginx_applied=bool(candidate.get("changes", {}).get("webserver")),
                system_applied=bool(candidate.get("changes", {}).get("kernel")),
                output=f"Optimization group {candidate['name']} applied",
                notes=f"Applied optimization group {candidate['name']} ({total_changes} params)",
                summary=f"Applied optimization group {candidate['name']} ({total_changes} params)",
                recommendations=[
                    {
                        "title": candidate["name"],
                        "scope": "optimization",
                        "changes": candidate["changes"],
                    }
                ],
                optimization_group=candidate,
                optimization_apply=apply_result,
            )
        else:
            logger.step(f"Step 5: Iteration {iteration}/{max_iterations} — LLM debate...")

            context_prompt = _build_context_prompt(
                rhel_ver=rhel_ver,
                kernel_ver=kernel_ver,
                cpu_cores=cpu_cores,
                ram_gb=ram_gb,
                checks_summary=checks_summary,
                benchmark_evidence_text=benchmark_evidence_text,
                prior_fixes=prior_fixes,
                prior_knowledge=prior_knowledge_text,
            )
            if iteration_feedback:
                context_prompt += f"\n{iteration_feedback}\n"

            logger.log(
                "orchestrator",
                f"Context prompt: {len(context_prompt)} chars (iteration {iteration})",
                "info",
            )

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

        nginx_applied = getattr(diagnosis, "nginx_applied", False)
        system_applied = getattr(diagnosis, "system_applied", False)
        if not nginx_applied and not system_applied:
            decision = "No changes applied — stopping"
            logger.status("iteration", f"Iteration {iteration}: {decision}")
            diagnosis_agent.save_iteration_summary(
                deps,
                iteration=iteration,
                baselines=baselines,
                results=iteration_finals or healthy_results or baselines,
                regressions=[],
                decision=decision,
                diagnosis=diagnosis,
            )
            break

        if bench_tool == "hackathon":
            logger.step(f"Step 5.{iteration}: Running post-iteration benchmark (all workloads)...")
            iteration_finals = _run_hackathon_benchmark(deps, cfg, f"iter{iteration}", session_id)

        healthy_floor = cfg.get("service", {}).get("benchmark", {}).get("healthy_floor_rps")
        should_stop, regressions = _check_iteration_exit(baselines, iteration_finals, healthy_floor)

        if in_optimization_mode:
            candidate = getattr(diagnosis, "optimization_group", None) or {}
            current_small = float(iteration_finals.get("small", {}).get("rps", 0) or 0)
            prior_results = dict(healthy_results or baselines)
            prior_small = float(prior_results.get("small", {}).get("rps", 0) or 0)
            gain_pct = ((current_small - prior_small) / prior_small * 100) if prior_small else 0.0
            keep_group = should_stop and gain_pct >= optimization_min_gain_pct
            decision = (
                f"Kept optimization group {candidate.get('name')} (small {gain_pct:+.1f}%)"
                if keep_group
                else f"Reverted optimization group {candidate.get('name')} (small {gain_pct:+.1f}%)"
            )
            if keep_group:
                healthy_results = dict(iteration_finals)
                final_results = dict(iteration_finals)
                if current_small > float(best_results.get("small", {}).get("rps", 0) or 0):
                    best_results = dict(iteration_finals)
                    best_iteration = iteration
            else:
                _revert_optimization_group(deps, snapshot)
                rejected_optimization_groups.add(candidate.get("name", "unknown"))
                final_results = dict(healthy_results or baselines)

            _save_optimization_outcome(
                memory,
                session_id,
                iteration,
                candidate,
                decision=decision,
                kept=keep_group,
                baseline_results=prior_results,
                benchmark_results=iteration_finals,
                applied=getattr(diagnosis, "optimization_apply", {}),
                reverted=not keep_group,
            )
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
            if iteration >= max_iterations:
                break
            continue

        if should_stop:
            current_small = float(iteration_finals.get("small", {}).get("rps", 0) or 0)
            best_small = top_runs[0]["small_rps"] if top_runs else 0
            healthy_results = dict(iteration_finals)
            final_results = dict(iteration_finals)
            if current_small > float(best_results.get("small", {}).get("rps", 0) or 0):
                best_results = dict(iteration_finals)
                best_iteration = iteration
            if (
                optimization_enabled
                and has_top_runs
                and current_small < best_small * (1 - (optimization_gap_pct / 100.0))
                and iteration < max_iterations
            ):
                decision = (
                    f"All workloads OK but small={current_small:.0f} < "
                    f"best={best_small:.0f} — entering optimization mode"
                )
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
                in_optimization_mode = True
                continue

            decision = f"All workloads OK — stopping after iteration {iteration}"
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
                "nginx_applied": getattr(diagnosis, "nginx_applied", False),
                "system_applied": getattr(diagnosis, "system_applied", False),
                "recommendation_count": len(getattr(diagnosis, "recommendations", []) or []),
                "rca_count": len(getattr(diagnosis, "rca_records", []) or []),
            },
            metadata={"session_id": session_id},
        )

    if max_phase <= 3:
        logger.status("main", "Stopping after Phase 3 planning (RCA + recommendations)")
        _save_token_usage(memory, session_id, deps.token_counter)
        fixes_applied_count = len(memory.get_facts(session_id, type="fix") or [])
        memory.complete_session(
            session_id,
            total_tokens=deps.token_counter.total,
            fixes_applied=fixes_applied_count,
            rps_start=baseline_rps,
            rps_end=baseline_rps,
        )
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

    best_rps = float(best_results.get("small", {}).get("rps", baseline_rps) or baseline_rps)
    memory.update_profile(session_id, best_rps=best_rps)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 6: Final benchmarks (direct — no LLM)
    # Reuse last iteration results if available; otherwise run fresh.
    # ══════════════════════════════════════════════════════════════════════════
    if final_results:
        logger.step("Step 6: Using last iteration benchmark as final results...")
        finals = final_results
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

    final_small_rps = finals.get("small", {}).get("rps", finals.get("homepage", {}).get("rps", 0))
    memory.update_profile(session_id, best_rps=best_rps)

    # Persist final results to benchmarks table (permanent, not session-scoped)
    for workload in ("homepage", "small", "medium", "large", "mixed"):
        wl_data = finals.get(workload, {})
        if wl_data.get("rps"):
            memory.save_benchmark(
                session_id=session_id,
                iteration_num=iteration,
                phase="final",
                payload_size=workload,
                rps=wl_data.get("rps"),
                latency_p99_ms=wl_data.get("p99"),
                cpu_pct=wl_data.get("cpu_pct"),
                mem_pct=wl_data.get("mem_mb"),
            )

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
    # STEP 7.5: Leaderboard check (query TiDB — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    leaderboard = check_leaderboard(memory, finals)
    if leaderboard.get("qualifies") and leaderboard.get("rank"):
        rank = leaderboard["rank"]
        logger.status(
            "leaderboard",
            f"This run ranks #{rank} with small={leaderboard['current_small']:.0f} RPS",
        )
        if leaderboard.get("beats_best"):
            logger.status("leaderboard", "NEW BEST RUN!")
    else:
        top = get_top_runs(memory)
        floor = top[-1]["small_rps"] if top else 0
        logger.status(
            "leaderboard",
            f"Did not qualify (floor: small>{floor:.0f}, medium>={1300}, large>={180})",
        )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 8: Generate report (template — no LLM)
    # ══════════════════════════════════════════════════════════════════════════
    logger.step("Step 8: Generating report...")

    _save_token_usage(memory, session_id, deps.token_counter)
    fixes_applied_count = len(memory.get_facts(session_id, type="fix") or [])
    memory.complete_session(
        session_id,
        total_tokens=deps.token_counter.total,
        fixes_applied=fixes_applied_count,
        rps_start=baseline_rps,
        rps_end=final_small_rps,
    )

    token_history = memory.get_token_history()

    report_path = reporter.generate(
        session_id,
        memory,
        deps.token_counter,
        baselines=baselines,
        finals=finals,
        best_results=best_results,
        best_iteration=best_iteration,
        stability=stability_data,
        throughput=throughput_info,
        token_history=token_history,
    )

    total_improvement = ((best_rps - baseline_rps) / baseline_rps * 100) if baseline_rps else 0.0
    nginx_applied = getattr(diagnosis, "nginx_applied", "unknown")
    system_applied = getattr(diagnosis, "system_applied", "unknown")
    hypothesis_path = f"hypothesis/{session_id}/"
    log_file_path = logger.get_log_path()
    logger.panel(
        "SlayMetricsAgent Complete",
        f"Session: {session_id}\n"
        f"Baseline (small): {baseline_rps:.1f} req/sec\n"
        f"Best achieved (small): {best_rps:.1f} req/sec\n"
        f"Final state (small):   {final_small_rps:.1f} req/sec\n"
        f"Improvement: {total_improvement:+.1f}%\n"
        f"Nginx applied: {nginx_applied}, System applied: {system_applied}\n"
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
    prior_knowledge: str = "",
) -> str:
    checks_text = "\n".join(checks_summary)

    prior_text = ""
    if prior_fixes:
        prior_text = "Already applied (skip): "
        prior_text += ", ".join(f"{pf['parameter']}" for pf in prior_fixes)
        prior_text += "\n"

    knowledge_text = f"\n{prior_knowledge}\n" if prior_knowledge else ""

    return f"""{rhel_ver} | {kernel_ver} | {cpu_cores} CPU | {ram_gb}GB
Checks: {checks_text}
Benchmark Evidence:
{benchmark_evidence_text}
{prior_text}{knowledge_text}
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
            f"- {source}: workers={summary.get('nginx_worker_count', 0)}, "
            f"cores={summary.get('nginx_worker_cores', [])}, "
            f"somaxconn={summary.get('somaxconn', 'unknown')}, "
            f"syn_backlog={summary.get('tcp_max_syn_backlog', 'unknown')}, "
            f"ports={summary.get('ip_local_port_range', 'unknown')}, "
            f"rx_drop={summary.get('rx_drop_total', 'unknown')}, "
            f"tx_drop={summary.get('tx_drop_total', 'unknown')}, "
            f"estab={summary.get('tcp_established', 'unknown')}"
        )
    return "\n".join(lines)


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


def _collect_current_state(deps: AgentDeps) -> dict[str, str]:
    from agents.tools_inspect import inspect_all

    inspection = inspect_all(deps.ssh, deps.config)
    current: dict[str, str] = {}
    for category in ("webserver", "kernel"):
        category_current = (inspection.get(category) or {}).get("current") or {}
        for param, value in category_current.items():
            current[f"{category}.{param}"] = str(value)
    return current


def _snapshot_optimization_state(deps: AgentDeps, candidate: dict[str, Any]) -> dict[str, Any]:
    changes = candidate.get("changes", {}) or {}
    snapshot: dict[str, Any] = {
        "kernel": dict(candidate.get("current", {}) or {}),
        "nginx_backup_dir": None,
    }
    if changes.get("webserver"):
        config_path = deps.config["service"]["config_path"]
        backup_dir = f"/tmp/slay_opt_{deps.session_id}_{getattr(deps, 'iteration', 0)}"
        deps.ssh.execute(
            "mkdir -p {dir}/conf.d && "
            "cp {config} {dir}/nginx.conf && "
            "cp -a /etc/nginx/conf.d/. {dir}/conf.d/ 2>/dev/null || true".format(
                dir=backup_dir,
                config=config_path,
            ),
            timeout=20,
        )
        snapshot["nginx_backup_dir"] = backup_dir
    return snapshot


def _apply_optimization_group(deps: AgentDeps, candidate: dict[str, Any]) -> dict[str, Any]:
    from agents.tools_apply import apply_kernel

    changes = candidate.get("changes", {}) or {}
    results: dict[str, Any] = {}
    web_changes = changes.get("webserver") or {}
    if web_changes:
        applied: list[str] = []
        failed: list[str] = []
        for param, value in web_changes.items():
            if deps.adapter.apply_config(param, value):
                applied.append(param)
            else:
                failed.append(param)
        reload_result = deps.ssh.execute("nginx -t 2>&1 && nginx -s reload 2>&1", timeout=20)
        results["webserver"] = {
            "applied": applied,
            "failed": failed,
            "reload_ok": reload_result.exit_code == 0,
        }
    kernel_changes = changes.get("kernel") or {}
    if kernel_changes:
        results["kernel"] = apply_kernel(deps.ssh, kernel_changes)
    return results


def _revert_optimization_group(deps: AgentDeps, snapshot: dict[str, Any]) -> None:
    from agents.tools_apply import apply_kernel

    backup_dir = snapshot.get("nginx_backup_dir")
    if backup_dir:
        config_path = deps.config["service"]["config_path"]
        deps.ssh.execute(
            "cp {dir}/nginx.conf {config} && "
            "rm -rf /etc/nginx/conf.d && mkdir -p /etc/nginx/conf.d && "
            "cp -a {dir}/conf.d/. /etc/nginx/conf.d/ 2>/dev/null || true && "
            "nginx -t 2>&1 && nginx -s reload 2>&1".format(
                dir=backup_dir,
                config=config_path,
            ),
            timeout=30,
        )
    kernel_snapshot = {}
    for full_param, value in (snapshot.get("kernel") or {}).items():
        if not full_param.startswith("kernel."):
            continue
        param = full_param.split(".", 1)[1]
        if value and value not in ("unknown", "not set"):
            kernel_snapshot[param] = value
    if kernel_snapshot:
        apply_kernel(deps.ssh, kernel_snapshot)


def _save_optimization_considered(
    memory, session_id: str, iteration: int, groups: list[dict[str, Any]]
):
    payload = [
        {
            "name": group["name"],
            "score": group["score"],
            "risk": group["risk"],
            "reasons": group["reasons"],
            "changes": group["changes"],
            "params_text": _format_group_changes(group["changes"]),
        }
        for group in groups
    ]
    memory.save_context(
        session_id,
        "command_output",
        f"optimization_considered_iter{iteration}",
        json.dumps(payload),
        f"optimization iter {iteration}: considered {len(groups)} groups",
    )


def _save_optimization_outcome(
    memory,
    session_id: str,
    iteration: int,
    candidate: dict[str, Any],
    *,
    decision: str,
    kept: bool,
    baseline_results: dict[str, Any],
    benchmark_results: dict[str, Any],
    applied: dict[str, Any],
    reverted: bool,
) -> None:
    memory.save_context(
        session_id,
        "command_output",
        f"optimization_decision_iter{iteration}_{candidate.get('name', 'unknown')}",
        json.dumps(
            {
                "group": candidate.get("name"),
                "score": candidate.get("score"),
                "risk": candidate.get("risk"),
                "reasons": candidate.get("reasons", []),
                "changes": candidate.get("changes", {}),
                "params_text": _format_group_changes(candidate.get("changes", {})),
                "decision": decision,
                "kept": kept,
                "reverted": reverted,
                "baseline_results": baseline_results,
                "benchmark_results": benchmark_results,
                "applied": applied,
            }
        ),
        decision,
    )

    # Persist optimization outcomes as validations on the logical fix identity.
    # This keeps the knowledge row stable and lets future ranking learn from
    # confirmed/contradicted history without deprecating the fact itself.
    group_changes = candidate.get("changes", {}) or {}
    group_name = candidate.get("name", "unknown")
    baseline_small = float(baseline_results.get("small", {}).get("rps", 0) or 0)
    benchmark_small = float(benchmark_results.get("small", {}).get("rps", 0) or 0)
    small_delta_pct = (
        ((benchmark_small - baseline_small) / baseline_small * 100) if baseline_small else None
    )
    validation_outcome = "confirmed" if kept else "contradicted"
    for category, params in group_changes.items():
        if not isinstance(params, dict):
            continue
        for param, value in params.items():
            full_param = f"{category}.{param}"
            current_values = candidate.get("current", {}) or {}
            memory.save_optimization_validation(
                session_id=session_id,
                parameter=full_param,
                reasoning=f"optimization group '{group_name}'",
                before_value=str(current_values.get(full_param, "")),
                after_value=str(value),
                outcome=validation_outcome,
                before_rps=baseline_small or None,
                after_rps=benchmark_small or None,
                impact_pct=small_delta_pct,
                notes=decision,
            )


def _format_group_changes(changes: dict[str, dict[str, str]]) -> str:
    parts: list[str] = []
    for category in ("webserver", "kernel", "resource_limits", "network", "storage"):
        for param, value in (changes.get(category) or {}).items():
            parts.append(f"{param}={value}")
    return ", ".join(parts) if parts else "none"


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
        "baseline_pre_workers": pre_summary.get("nginx_worker_count", 0),
        "baseline_post_workers": post_summary.get("nginx_worker_count", 0),
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
