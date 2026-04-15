"""Execution graph nodes: audit, benchmark, merge_fixes, remediate_fix, save_partial_state."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .audit import AuditRunner
from .remediation_tools import TOOL_REGISTRY, NETWORK_TOOL_NAMES
from .fix_applier import FixApplier
from .display import Display
from .constants import REPORTS_DIR, SCRIPTS_DIR, REMOTE_TMP, AUDIT_SCRIPT, BOOTSTRAP_SCRIPT

if TYPE_CHECKING:
    from .rca_agent import RCAAgent, RCAState

logger = logging.getLogger("slayMetrics.nodes")


def save_partial_state(agent: RCAAgent) -> None:
    """Persist whatever state has accumulated — called on signal or error."""
    ps = agent._partial_state
    if not ps:
        return
    session_id = ps.get("session_id", "")
    rca_report = ps.get("rca_report", "")
    if rca_report and session_id:
        try:
            agent.reporter.save(rca_report, session_id)
        except Exception as e:
            logger.error("Partial save — report failed: %s", e)
    audit   = ps.get("audit_output", "")
    bench   = ps.get("benchmark_results", "")
    applied = ps.get("applied_fixes", [])
    rejected = ps.get("rejected_fixes", [])
    if audit and rca_report:
        try:
            agent.analyzer.save_example(audit, rca_report, bench,
                                        applied_fixes=applied, rejected_fixes=rejected)
        except Exception as e:
            logger.error("Partial save — example failed: %s", e)
    logger.info(
        "Partial state saved (session: %s, applied: %d fixes) — skipping memory store",
        session_id[:8] if session_id else "?", len(applied),
    )


def run_audit(state: RCAState, agent: RCAAgent) -> RCAState:
    precomputed = state.get("audit_output", "")
    if precomputed:
        # Injected by fleet orchestrator — skip remote audit
        logger.info("Using pre-collected audit output (%d bytes) — skipping remote audit.",
                    len(precomputed))
        return {**state, "error": ""}
    # Use lightweight bootstrap when investigation is enabled (SRE agent is primary);
    # fall back to full static audit when investigation is disabled.
    if agent.config.investigation_enabled:
        script = BOOTSTRAP_SCRIPT
        logger.info("Investigation enabled — using lightweight bootstrap audit.")
    else:
        script = AUDIT_SCRIPT
        logger.info("Investigation disabled — using full static audit.")
    try:
        with agent._executor() as executor:
            output = AuditRunner(executor, SCRIPTS_DIR, REMOTE_TMP, script).deploy_and_run()
        return {**state, "audit_output": output, "error": ""}
    except Exception as e:
        logger.error("run_audit failed: %s", e)
        return {**state, "error": str(e)}


def preflight_check(state: RCAState, agent: RCAAgent) -> RCAState:
    """Verify nginx is running before benchmarking. Diagnose and recover if failed."""
    if state.get("error"):
        return state
    try:
        with agent._executor() as executor:
            active, _ = executor.run("systemctl is-active nginx.service 2>/dev/null || echo unknown")
            active = active.strip()
            if active == "active":
                # Verify nginx is actually serving HTTP
                http_check, _ = executor.run(
                    "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 http://127.0.0.1/ 2>/dev/null || echo 000"
                )
                http_code = http_check.strip()
                if http_code in ("200", "403", "301", "302", "304"):
                    logger.info("Preflight: nginx is active and serving HTTP (%s).", http_code)
                    return {**state, "error": ""}
                logger.warning(
                    "Preflight: nginx is active but HTTP check returned %s — may have config issues.",
                    http_code,
                )
                return {**state, "error": ""}

            logger.warning("Preflight: nginx is NOT active (state: %s) — diagnosing...", active)

            # Gather failure details
            result_prop, _ = executor.run(
                "systemctl show nginx.service -p Result | awk -F= '{print $2}'"
            )
            journal, _ = executor.run(
                "journalctl -u nginx.service -n 10 --no-pager 2>/dev/null"
            )
            logger.warning("nginx Result: %s", result_prop.strip())
            logger.warning("Journal:\n%s", journal.strip())

            # Check LimitNOFILE vs fs.nr_open conflict
            nr_open, _ = executor.run("sysctl -n fs.nr_open 2>/dev/null")
            nofile, _ = executor.run(
                "systemctl show nginx.service -p LimitNOFILE | awk -F= '{print $2}'"
            )
            nr_open_val = int(nr_open.strip()) if nr_open.strip().isdigit() else 0
            nofile_val = int(nofile.strip()) if nofile.strip().isdigit() else 0

            if nofile_val > nr_open_val > 0:
                logger.info(
                    "Preflight fix: LimitNOFILE(%d) > fs.nr_open(%d) — raising fs.nr_open",
                    nofile_val, nr_open_val,
                )
                new_nr_open = max(nofile_val, 1048576)
                executor.run(f"sysctl -w fs.nr_open={new_nr_open}")
                executor.run(f"sysctl -w fs.file-max={new_nr_open}")

            # Scan for crippling drop-in overrides
            dropin_dir = "/etc/systemd/system/nginx.service.d"
            ls_out, _ = executor.run(f"ls -1 {dropin_dir}/*.conf 2>/dev/null")
            for dropin in ls_out.strip().splitlines():
                if not dropin:
                    continue
                content, _ = executor.run(f"cat {dropin}")
                # Remove files that set sabotage-level limits
                sabotage = False
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("Nice=") and int(line.split("=")[1]) >= 15:
                        sabotage = True
                    elif line.startswith("OOMScoreAdjust=") and int(line.split("=")[1]) > 200:
                        sabotage = True
                    elif line.startswith("CPUWeight=") and int(line.split("=")[1]) <= 10:
                        sabotage = True
                    elif line.startswith("IOWeight=") and int(line.split("=")[1]) <= 10:
                        sabotage = True
                    elif line.startswith("TasksMax=") and int(line.split("=")[1]) <= 100:
                        sabotage = True
                if sabotage:
                    logger.info("Preflight fix: removing crippling drop-in %s", dropin)
                    executor.run(f"rm -f {dropin}")

            # Attempt restart
            executor.run("systemctl daemon-reload")
            executor.run("systemctl restart nginx.service")
            verify, _ = executor.run("systemctl is-active nginx.service 2>/dev/null || echo failed")
            if verify.strip() != "active":
                return {**state, "error": f"Preflight: nginx still not active after recovery attempt (state: {verify.strip()})"}

            # Verify HTTP is actually reachable after recovery
            http_check, _ = executor.run(
                "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 http://127.0.0.1/ 2>/dev/null || echo 000"
            )
            http_code = http_check.strip()
            if http_code in ("000",):
                return {**state, "error": f"Preflight: nginx is active but not serving HTTP (code: {http_code})"}
            logger.info("Preflight: nginx recovered successfully and serving HTTP (%s).", http_code)
            return {**state, "error": ""}

    except Exception as e:
        logger.error("preflight_check failed: %s", e)
        return {**state, "error": str(e)}


def run_benchmark(state: RCAState, agent: RCAAgent) -> RCAState:
    if state.get("error"):
        return state
    try:
        session_id = state.get("session_id", "unknown")
        csv_path   = REPORTS_DIR / session_id / "live_samples.csv"

        if agent.orchestrator and agent.target:
            raw, csv_text = agent.orchestrator.run_benchmark_with_live(agent.target)
            if csv_text.strip():
                csv_path.parent.mkdir(parents=True, exist_ok=True)
                csv_path.write_text(csv_text)
        else:
            agent.sampler.start(csv_path)
            try:
                raw = agent.benchmark.run()
            finally:
                agent.sampler.stop()

        formatted    = agent.benchmark.format_for_llm(raw)
        baseline_rps = agent.evaluator.parse_rps(raw)
        logger.info("Benchmark captured (%d bytes, %d workloads)", len(formatted), len(baseline_rps))
        Display.benchmark_results(formatted)
        agent.tracker.log_baseline(baseline_rps)

        live_audit = agent.sampler.analyze(csv_path) if csv_path.exists() else ""
        Display.live_analysis(live_audit)

        return {**state, "benchmark_results": formatted,
                "baseline_rps": baseline_rps, "live_audit_output": live_audit}
    except Exception as e:
        logger.error("run_benchmark failed: %s", e)
        return {**state, "error": str(e)}


def merge_fixes(state: RCAState, agent: RCAAgent) -> RCAState:
    """Combine all domain fixes, apply scope/no-op filters, build final plan."""
    if state.get("error"):
        return state
    # If fix generator already populated fixes, use those directly
    pre_generated = state.get("fixes", [])
    if pre_generated:
        all_fixes = pre_generated
    else:
        all_fixes = (
            state.get("network_fixes", []) +
            state.get("kernel_fixes", []) +
            state.get("nginx_fixes", [])
        )
    scoped = []
    for fix in all_fixes:
        tool = fix.get("tool", "")
        if tool in NETWORK_TOOL_NAMES:
            scope = agent.config.remediation_network_tool_scope(tool)
            if scope == "none":
                logger.warning("Network tool '%s' scope=none — excluded", tool)
                continue
            fix["_net_scope"] = scope
        if tool not in TOOL_REGISTRY:
            logger.warning("Unknown tool '%s' — dropped", tool)
            continue
        scoped.append(fix)

    def _sort_key(f: dict) -> tuple:
        is_net    = 0 if f.get("tool") in NETWORK_TOOL_NAMES else 1
        is_access = 0 if f.get("params", {}).get("directive") == "access_log" else 1
        return (f.get("tier", 99), is_net, is_access)

    scoped.sort(key=_sort_key)

    if agent.audit_only:
        Display.fix_plan(scoped)
        return {**state, "fixes": scoped, "fix_index": 0}

    with agent._executor() as executor:
        for fix in scoped:
            tool_cls = TOOL_REGISTRY.get(fix.get("tool", ""))
            if tool_cls:
                fix["current_value"] = tool_cls.read_current(executor, fix.get("params", {}))
                fix["_no_op"] = tool_cls.is_no_op(fix["current_value"], fix.get("params", {}))

    skipped = [f for f in scoped if f.get("_no_op")]
    fixes   = [f for f in scoped if not f.get("_no_op")]
    if skipped:
        logger.info("Skipping %d no-op fixes: %s",
                    len(skipped), [f.get("description") for f in skipped])
    Display.fix_plan(fixes)
    return {**state, "fixes": fixes, "fix_index": 0}


def _llm_fix_review(
    state: RCAState,
    agent: RCAAgent,
    fix: dict,
    baseline: dict,
    current_rps: dict,
    keep: bool,
    simple_pct: float,
    weighted_pct: float,
    degraded: dict,
) -> tuple[bool, bool, RCAState]:
    """Ask LLM to review a rejected fix when RPS-weighted improvement is positive.

    Returns (keep, llm_overridden, updated_state).
    """
    if keep or not agent.config.remediation_llm_review_rejected or weighted_pct <= 0:
        if not keep:
            logger.info(
                "LLM review SKIPPED — RPS-weighted improvement is %.2f%% (not positive). "
                "simple avg: %+.2f%%, degraded: %s",
                weighted_pct, simple_pct,
                ", ".join(f"{w}={d:+.1f}%" for w, d in degraded.items()) if degraded else "none",
            )
        return keep, False, state

    logger.info(
        "LLM review TRIGGERED — gate rejected but RPS-weighted improvement is +%.2f%% "
        "(simple avg: %+.2f%%, degraded: %s)",
        weighted_pct, simple_pct,
        ", ".join(f"{w}={d:+.1f}%" for w, d in degraded.items()),
    )
    rca_context = state.get("rca_report", "")
    save_dir = REPORTS_DIR / state.get("session_id", "unknown")
    accept, reasoning, r_in, r_out = agent.fix_reviewer.review(
        fix, baseline, current_rps, rca_context, degraded, save_dir=save_dir,
    )
    verdict_tag = "override_accept" if accept else "confirm_reject"
    calls = list(state.get("llm_calls", []))
    calls.append(("fix_review", 0, r_in, r_out, 0))
    updated = {**state, "llm_calls": calls,
               "total_input_tokens": state.get("total_input_tokens", 0) + r_in,
               "total_output_tokens": state.get("total_output_tokens", 0) + r_out}
    agent.tracker.log_llm_call(f"fix_review:{verdict_tag}", 0, r_in, r_out, 0)
    if accept:
        logger.info("LLM OVERRIDE: fix accepted — %s", reasoning)
    else:
        logger.info("LLM CONFIRMED rejection — %s", reasoning)
    return accept, accept, updated


def remediate_fix(state: RCAState, agent: RCAAgent) -> RCAState:
    """Apply fixes — group-aware. Fixes with same _group are applied together."""
    fixes    = state["fixes"]
    idx      = state["fix_index"]
    fix      = fixes[idx]
    baseline = state["baseline_rps"]
    applied  = list(state.get("applied_fixes", []))
    rejected = list(state.get("rejected_fixes", []))

    # Collect all fixes in the same group
    group_id = fix.get("_group")
    if group_id is not None:
        group_fixes = [f for f in fixes[idx:] if f.get("_group") == group_id]
        group_label = fix.get("_group_label", f"Group {group_id}")
        next_idx = idx + len(group_fixes)
    else:
        group_fixes = [fix]
        group_label = fix.get("description", "")
        next_idx = idx + 1

    logger.info(
        "--- Group %s (%d fixes) ---\n  %s",
        group_label, len(group_fixes),
        "\n  ".join(f"{f.get('tool')}: {f.get('description')}" for f in group_fixes),
    )

    try:
        with agent._executor() as executor:
            applier = FixApplier(executor)
            agent._current_applier = applier

            # Apply all fixes in the group
            applied_in_group = []
            for gf in group_fixes:
                if gf.get("_net_scope") == "read":
                    logger.warning("Network tool '%s' scope=read — skipping", gf.get("tool"))
                    continue
                try:
                    applier.apply(gf)
                    applied_in_group.append(gf)
                except ValueError as e:
                    logger.info("Skipped (no-op): %s — %s", gf.get("description"), e)
                except Exception as e:
                    logger.error("Failed to apply %s: %s", gf.get("description"), e)

            if not applied_in_group:
                logger.info("Group %s: no fixes applied (all no-op or failed)", group_label)
                for gf in group_fixes:
                    rejected.append((gf.get("description", ""), 0.0))
                agent._current_applier = None
                return {**state, "fix_index": next_idx, "baseline_rps": baseline,
                        "applied_fixes": applied, "rejected_fixes": rejected}

            # Check if all are network tools (auto-accept)
            all_network = all(gf.get("tool") in NETWORK_TOOL_NAMES for gf in applied_in_group)
            if all_network:
                logger.info("Group %s: all network tools — auto-accepted", group_label)
                for gf in applied_in_group:
                    applied.append((gf.get("description", ""), 0.0))
                agent._current_applier = None
                agent._partial_state["applied_fixes"] = applied
                return {**state, "fix_index": next_idx, "baseline_rps": baseline,
                        "applied_fixes": applied, "rejected_fixes": rejected}

            # Benchmark the group as a whole
            raw = agent.benchmark.run()
            current_rps = agent.evaluator.parse_rps(raw)
            tier = fix.get("tier", fix.get("_group", 1))
            keep, pct, degraded = agent.evaluator.should_keep_group(
                baseline, current_rps,
                tier=tier,
                tier_thresholds=agent.config.group_tier_thresholds or None,
                high_value_share=agent.config.group_high_value_share,
                high_value_max_degradation=agent.config.group_high_value_max_degradation,
            )

            desc_list = ", ".join(gf.get("description", "") for gf in applied_in_group)
            Display.fix_comparison(
                idx + 1, len(fixes), f"[GROUP] {group_label}",
                "group", {}, baseline, current_rps, keep, pct,
            )

            if keep:
                for gf in applied_in_group:
                    applied.append((gf.get("description", ""), round(pct, 2)))
                baseline = current_rps
                logger.info("Group %s ACCEPTED (+%.1f%%): %s", group_label, pct, desc_list)
            else:
                for gf in applied_in_group:
                    rejected.append((gf.get("description", ""), round(pct, 2)))
                applier.rollback()
                logger.info("Group %s REJECTED (%.1f%%): %s", group_label, pct, desc_list)

            agent.tracker.log_fix(f"[GROUP] {group_label}", "group", keep, pct)
            agent._current_applier = None
            agent._partial_state["applied_fixes"]  = applied
            agent._partial_state["rejected_fixes"] = rejected

    except Exception as e:
        logger.error("remediate_fix group [%s] failed: %s", group_label, e)
        for gf in group_fixes:
            rejected.append((gf.get("description", ""), 0.0))

    return {**state, "fix_index": next_idx, "baseline_rps": baseline,
            "applied_fixes": applied, "rejected_fixes": rejected}


def retry_rejected(state: RCAState, agent: RCAAgent) -> RCAState:
    """Re-queue rejected fixes for a second pass on the improved system."""
    rejected = state.get("rejected_fixes", [])
    fixes = state.get("fixes", [])
    if not rejected:
        return {**state, "_retry_done": True}

    # Find the original fix dicts for rejected descriptions
    rejected_descs = {desc for desc, _ in rejected}
    retry_fixes = [f for f in fixes if f.get("description", "") in rejected_descs]

    if not retry_fixes:
        logger.info("Retry: no rejected fixes found to re-queue.")
        return {**state, "_retry_done": True}

    logger.info("=== RETRY PASS: re-testing %d rejected fixes on improved system ===",
                len(retry_fixes))
    for f in retry_fixes:
        logger.info("  Retry: %s (tool=%s)", f.get("description", ""), f.get("tool", ""))

    # Reset fix list to only rejected fixes, clear rejected list, reset index
    return {**state,
            "fixes": retry_fixes,
            "fix_index": 0,
            "rejected_fixes": [],
            "_retry_done": True}
