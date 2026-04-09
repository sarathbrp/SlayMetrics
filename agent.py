"""
RCA Slay Metrics Agent
======================
Entry point. LangGraph workflow:

  deploy_and_run → run_benchmark → analyze → parse_fixes → remediate_fix ┐
                                                                ↑          │ more fixes
                                                                └──────────┘
                                                                ↓ done
                                                               END

DUT     : root@d21-h23-000-r650.rdu2.scalelab.redhat.com
System2 : root@d21-h24-000-r650.rdu2.scalelab.redhat.com (agent machine)
"""

import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import StateGraph, END

from core import (Config, RemoteExecutor, AuditRunner, RCAAnalyzer,
                  BenchmarkRunner, TOOL_REGISTRY, NETWORK_TOOL_NAMES,
                  FixApplier, Evaluator, FixEvaluatorLLM, Display, ReportWriter,
                  FeedbackOptimizer, SemanticMemory, LiveSampler,
                  NetworkAnalyzer, KernelAnalyzer, NginxAnalyzer,
                  extract_audit_groups, RunTracker)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FORMAT  = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
logger = logging.getLogger("slayMetrics")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR    = Path(__file__).parent
LOGS_DIR    = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config.yaml"
PROMPTS_DIR = BASE_DIR / "prompts"
SCRIPTS_DIR = BASE_DIR / "scripts"
DSPY_DIR    = BASE_DIR / "dspy_data"
REPORTS_DIR = BASE_DIR / "rca_reports"

AUDIT_SCRIPT = "omega_master_audit.sh"
REMOTE_TMP   = "/tmp"


# ---------------------------------------------------------------------------
# Agent state
# ---------------------------------------------------------------------------

class RCAState(TypedDict):
    session_id: str
    similar_cases: str            # retrieved from semantic memory, shared across LLM calls
    audit_output: str
    benchmark_results: str        # latest benchmark output (raw text)
    live_audit_output: str        # dynamic metrics collected during benchmark
    baseline_rps: dict            # {workload: float} from initial benchmark
    # Per-domain analysis outputs (chained context)
    network_fixes: list
    network_summary: str          # chained → analyze_kernel
    kernel_fixes: list
    kernel_summary: str           # chained → analyze_nginx
    nginx_fixes: list
    rca_report: str               # combined summaries (for report file + DSPy examples)
    fixes: list                   # merged + sorted from all 3 domains
    fix_index: int
    applied_fixes: list           # [(description, improvement_pct)]
    rejected_fixes: list          # [(description, improvement_pct)]
    total_input_tokens: int
    total_output_tokens: int
    llm_calls: list              # [(domain, elapsed_s, in_tok, out_tok, num_fixes)]
    error: str


# ---------------------------------------------------------------------------
# RCAAgent
# ---------------------------------------------------------------------------

class RCAAgent:
    """LangGraph agent: audit → benchmark → RCA → remediation loop."""

    def __init__(self, config: Config):
        self.config             = config
        self.analyzer           = RCAAnalyzer(config, PROMPTS_DIR, DSPY_DIR)  # kept for save_example
        self.net_analyzer       = NetworkAnalyzer(config, PROMPTS_DIR)
        self.kernel_analyzer    = KernelAnalyzer(config, PROMPTS_DIR)
        self.nginx_analyzer     = NginxAnalyzer(config, PROMPTS_DIR)
        self.benchmark          = BenchmarkRunner(config)
        self.evaluator          = Evaluator()
        self.fix_reviewer       = FixEvaluatorLLM(config, PROMPTS_DIR)
        self.reporter         = ReportWriter(REPORTS_DIR)
        self.optimizer        = FeedbackOptimizer(
            min_new_examples=config.optimization_min_new_examples,
            max_bootstrap_demos=config.optimization_max_bootstrap_demos,
        )
        self.sampler          = LiveSampler(config, SCRIPTS_DIR, REMOTE_TMP,
                                             self._executor)
        self.memory           = SemanticMemory(
            persist_dir=DSPY_DIR / "chroma",
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
            embed_model=config.llm_embed_model,
        )
        self.tracker          = RunTracker(config)
        self._current_applier: FixApplier | None = None
        self._partial_state:   dict              = {}
        self.graph            = self._build_graph()
        self._setup_signal_handlers()

    def _setup_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum: int, frame: object) -> None:
        sig_name = signal.Signals(signum).name
        logger.warning("Signal %s received — rolling back any applied fix...", sig_name)
        if self._current_applier:
            try:
                self._current_applier.rollback()
                logger.info("Rollback complete.")
            except Exception as e:
                logger.error("Rollback failed: %s", e)
            self._current_applier = None
        self._save_partial()
        logger.info("Exiting.")
        sys.exit(0)

    def _save_partial(self) -> None:
        """Save whatever state has accumulated so far — called on signal or error."""
        ps = self._partial_state
        if not ps:
            return
        session_id = ps.get("session_id", "")
        rca_report = ps.get("rca_report", "")
        if rca_report and session_id:
            try:
                self.reporter.save(rca_report, session_id)
            except Exception as e:
                logger.error("Partial save — report failed: %s", e)
        audit   = ps.get("audit_output", "")
        bench   = ps.get("benchmark_results", "")
        applied = ps.get("applied_fixes", [])
        rejected = ps.get("rejected_fixes", [])
        if audit and rca_report:
            try:
                self.analyzer.save_example(audit, rca_report, bench,
                                           applied_fixes=applied,
                                           rejected_fixes=rejected)
            except Exception as e:
                logger.error("Partial save — example failed: %s", e)
        # Do NOT store partial runs in semantic memory — incomplete outcomes
        # would confuse future RCA prompts with misleading fix histories.
        logger.info("Partial state saved (session: %s, applied: %d fixes) — skipping memory store",
                    session_id[:8] if session_id else "?", len(applied))

    def _executor(self) -> RemoteExecutor:
        return RemoteExecutor(
            host=self.config.dut_host, user=self.config.dut_user,
            key_path=self.config.dut_key, port=self.config.dut_port,
            timeout=self.config.dut_timeout,
        )

    # --- nodes ---

    def _run_audit(self, state: RCAState) -> RCAState:
        try:
            with self._executor() as executor:
                output = AuditRunner(
                    executor, SCRIPTS_DIR, REMOTE_TMP, AUDIT_SCRIPT
                ).deploy_and_run()
            return {**state, "audit_output": output, "error": ""}
        except Exception as e:
            logger.error("run_audit failed: %s", e)
            return {**state, "error": str(e)}

    def _run_benchmark(self, state: RCAState) -> RCAState:
        if state.get("error"):
            return state
        try:
            session_id  = state.get("session_id", "unknown")
            csv_path    = REPORTS_DIR / session_id / "live_samples.csv"

            # Start background sampler before benchmark runs
            self.sampler.start(csv_path)
            try:
                raw = self.benchmark.run()
            finally:
                self.sampler.stop()

            formatted    = self.benchmark.format_for_llm(raw)
            baseline_rps = self.evaluator.parse_rps(raw)
            logger.info("Benchmark captured (%d bytes, %d workloads)",
                        len(formatted), len(baseline_rps))
            Display.benchmark_results(formatted)
            self.tracker.log_baseline(baseline_rps)

            # Analyze collected CSV → compact hypothesis for LLM + console
            live_audit = self.sampler.analyze(csv_path) if csv_path.exists() else ""
            Display.live_analysis(live_audit)

            return {**state, "benchmark_results": formatted,
                    "baseline_rps": baseline_rps, "live_audit_output": live_audit}
        except Exception as e:
            logger.error("run_benchmark failed: %s", e)
            return {**state, "error": str(e)}

    def _analyze_network(self, state: RCAState) -> RCAState:
        if state.get("error"):
            return state
        audit_output      = state["audit_output"]
        benchmark_results = state.get("benchmark_results", "")
        similar_cases = (
            self.memory.retrieve(audit_output, benchmark_results)
            if self.config.memory_inject_into_rca else ""
        )
        network_section = extract_audit_groups(audit_output, [5])
        save_dir = REPORTS_DIR / state.get("session_id", "unknown")
        try:
            fixes, summary, in_tok, out_tok, elapsed = self.net_analyzer.analyze(
                network_section, state.get("live_audit_output", ""), similar_cases,
                save_dir=save_dir,
            )
            calls = list(state.get("llm_calls", []))
            calls.append(("network", round(elapsed, 1), in_tok, out_tok, len(fixes)))
            self.tracker.log_llm_call("network", elapsed, in_tok, out_tok, len(fixes))
            self._partial_state.update({
                "session_id": state.get("session_id", ""),
                "audit_output": audit_output,
                "benchmark_results": benchmark_results,
                "rca_report": summary, "applied_fixes": [], "rejected_fixes": [],
            })
            return {**state, "similar_cases": similar_cases,
                    "network_fixes": fixes, "network_summary": summary,
                    "llm_calls": calls,
                    "total_input_tokens": state.get("total_input_tokens", 0) + in_tok,
                    "total_output_tokens": state.get("total_output_tokens", 0) + out_tok}
        except Exception as e:
            logger.error("analyze_network failed: %s", e)
            return {**state, "similar_cases": similar_cases,
                    "network_fixes": [], "network_summary": ""}

    def _analyze_kernel(self, state: RCAState) -> RCAState:
        if state.get("error"):
            return state
        kernel_section = extract_audit_groups(state["audit_output"], [1, 2, 3])
        sc = state.get("similar_cases", "") if self.config.memory_inject_into_fix_extraction else ""
        save_dir = REPORTS_DIR / state.get("session_id", "unknown")
        try:
            fixes, summary, in_tok, out_tok, elapsed = self.kernel_analyzer.analyze(
                kernel_section, state.get("benchmark_results", ""),
                state.get("network_summary", ""), sc,
                save_dir=save_dir,
            )
            calls = list(state.get("llm_calls", []))
            calls.append(("kernel", round(elapsed, 1), in_tok, out_tok, len(fixes)))
            self.tracker.log_llm_call("kernel", elapsed, in_tok, out_tok, len(fixes))
            return {**state, "kernel_fixes": fixes, "kernel_summary": summary,
                    "llm_calls": calls,
                    "total_input_tokens": state.get("total_input_tokens", 0) + in_tok,
                    "total_output_tokens": state.get("total_output_tokens", 0) + out_tok}
        except Exception as e:
            logger.error("analyze_kernel failed: %s", e)
            return {**state, "kernel_fixes": [], "kernel_summary": ""}

    def _analyze_nginx(self, state: RCAState) -> RCAState:
        if state.get("error"):
            return state
        nginx_section = extract_audit_groups(state["audit_output"], [4])
        sc = state.get("similar_cases", "") if self.config.memory_inject_into_fix_extraction else ""
        save_dir = REPORTS_DIR / state.get("session_id", "unknown")
        try:
            fixes, in_tok, out_tok, elapsed = self.nginx_analyzer.analyze(
                nginx_section, state.get("benchmark_results", ""),
                state.get("network_summary", ""), state.get("kernel_summary", ""), sc,
                save_dir=save_dir,
            )
            calls = list(state.get("llm_calls", []))
            calls.append(("nginx", round(elapsed, 1), in_tok, out_tok, len(fixes)))
            self.tracker.log_llm_call("nginx", elapsed, in_tok, out_tok, len(fixes))
            rca_report = "\n\n".join(filter(None, [
                state.get("network_summary", ""),
                state.get("kernel_summary", ""),
                f"Nginx fixes: {len(fixes)} identified.",
            ]))
            return {**state, "nginx_fixes": fixes, "rca_report": rca_report,
                    "llm_calls": calls,
                    "total_input_tokens": state.get("total_input_tokens", 0) + in_tok,
                    "total_output_tokens": state.get("total_output_tokens", 0) + out_tok}
        except Exception as e:
            logger.error("analyze_nginx failed: %s", e)
            return {**state, "nginx_fixes": [], "rca_report": state.get("rca_report", "")}

    def _merge_fixes(self, state: RCAState) -> RCAState:
        """Combine all domain fixes, apply scope/no-op filters, build final plan."""
        if state.get("error"):
            return state
        all_fixes = (
            state.get("network_fixes", []) +
            state.get("kernel_fixes", []) +
            state.get("nginx_fixes", [])
        )
        # Scope filter
        scoped = []
        for fix in all_fixes:
            tool = fix.get("tool", "")
            if tool in NETWORK_TOOL_NAMES:
                scope = self.config.remediation_network_tool_scope(tool)
                if scope == "none":
                    logger.warning("Network tool '%s' scope=none — excluded", tool)
                    continue
                fix["_net_scope"] = scope
            if tool not in TOOL_REGISTRY:
                logger.warning("Unknown tool '%s' — dropped", tool)
                continue
            scoped.append(fix)

        # Sort: network tools first → access_log off second → then by tier
        def _sort_key(f: dict) -> tuple:
            is_net     = 0 if f.get("tool") in NETWORK_TOOL_NAMES else 1
            is_access  = 0 if f.get("params", {}).get("directive") == "access_log" else 1
            return (f.get("tier", 99), is_net, is_access)

        scoped.sort(key=_sort_key)

        # Read current values + no-op detection
        with self._executor() as executor:
            for fix in scoped:
                tool_cls = TOOL_REGISTRY.get(fix.get("tool", ""))
                if tool_cls:
                    fix["current_value"] = tool_cls.read_current(
                        executor, fix.get("params", {}))
                    fix["_no_op"] = tool_cls.is_no_op(
                        fix["current_value"], fix.get("params", {}))

        skipped = [f for f in scoped if f.get("_no_op")]
        fixes   = [f for f in scoped if not f.get("_no_op")]
        if skipped:
            logger.info("Skipping %d no-op fixes: %s",
                        len(skipped), [f.get("description") for f in skipped])
        Display.fix_plan(fixes)
        return {**state, "fixes": fixes, "fix_index": 0}

    def _remediate_fix(self, state: RCAState) -> RCAState:
        fixes     = state["fixes"]
        idx       = state["fix_index"]
        fix       = fixes[idx]
        baseline  = state["baseline_rps"]
        applied   = list(state.get("applied_fixes", []))
        rejected  = list(state.get("rejected_fixes", []))
        threshold = self.config.remediation_threshold

        logger.info(
            "--- Fix %d/%d [Tier %s] ---\n  tool: %s\n  desc: %s\n  params: %s",
            idx + 1, len(fixes), fix.get("tier", "?"),
            fix.get("tool", ""), fix.get("description", ""), fix.get("params", {}),
        )
        try:
            if fix.get("_net_scope") == "read":
                logger.warning(
                    "Network tool '%s' scope=read — skipping apply. "
                    "Set scope to 'write' in config.yaml to allow.",
                    fix.get("tool", ""),
                )
                rejected.append((fix.get("description", ""), 0.0))
                return {**state, "fix_index": idx + 1, "baseline_rps": baseline,
                        "applied_fixes": applied, "rejected_fixes": rejected}

            with self._executor() as executor:
                applier = FixApplier(executor)
                self._current_applier = applier
                applier.apply(fix)

                # Network chaos tools are auto-accepted — no benchmark needed.
                # They remove hard infrastructure caps (TC shaping, iptables connlimit,
                # nftables rate limits). Benchmarking while other caps are still active
                # gives misleading results and could cause incorrect rollback.
                if fix.get("tool") in NETWORK_TOOL_NAMES:
                    logger.info("Network tool '%s' auto-accepted (no benchmark needed)",
                                fix.get("tool"))
                    applied.append((fix.get("description", ""), 0.0))
                    self._current_applier = None
                    self._partial_state["applied_fixes"] = applied
                    return {**state, "fix_index": idx + 1, "baseline_rps": baseline,
                            "applied_fixes": applied, "rejected_fixes": rejected}

                # benchmark runs while SSH connection stays open (reused for rollback)
                raw = self.benchmark.run()
                current_rps = self.evaluator.parse_rps(raw)
                keep, pct, degraded = self.evaluator.should_keep(
                    baseline, current_rps, threshold,
                    self.config.remediation_degradation_tolerance,
                )

                # LLM review: when gate rejects but RPS-weighted improvement
                # is positive, ask the LLM to evaluate with full context.
                # RPS-weighted avg gives high-volume workloads proportional
                # influence — a -12% dip on 1k-RPS mixed won't mask +18%
                # gains on 70k-RPS homepage/small.
                llm_overridden = False
                simple_pct = self.evaluator.improvement_pct(baseline, current_rps)
                weighted_pct = self.evaluator.weighted_improvement_pct(
                    baseline, current_rps,
                )

                if not keep and self.config.remediation_llm_review_rejected:
                    if weighted_pct > 0:
                        logger.info(
                            "LLM review TRIGGERED — gate rejected but "
                            "RPS-weighted improvement is +%.2f%% "
                            "(simple avg: %+.2f%%, degraded: %s)",
                            weighted_pct, simple_pct,
                            ", ".join(f"{w}={d:+.1f}%" for w, d in degraded.items()),
                        )
                        rca_context = state.get("rca_report", "")
                        save_dir = REPORTS_DIR / state.get("session_id", "unknown")
                        accept, reasoning, r_in, r_out = self.fix_reviewer.review(
                            fix, baseline, current_rps, rca_context, degraded,
                            save_dir=save_dir,
                        )
                        # Track LLM review tokens + call in MLflow and state
                        verdict_tag = "override_accept" if accept else "confirm_reject"
                        calls = list(state.get("llm_calls", []))
                        calls.append(("fix_review", 0, r_in, r_out, 0))
                        state = {**state, "llm_calls": calls,
                                 "total_input_tokens": state.get("total_input_tokens", 0) + r_in,
                                 "total_output_tokens": state.get("total_output_tokens", 0) + r_out}
                        self.tracker.log_llm_call(
                            f"fix_review:{verdict_tag}", 0, r_in, r_out, 0,
                        )
                        if accept:
                            keep = True
                            llm_overridden = True
                            logger.info(
                                "LLM OVERRIDE: fix accepted — %s", reasoning,
                            )
                        else:
                            logger.info(
                                "LLM CONFIRMED rejection — %s", reasoning,
                            )
                    else:
                        logger.info(
                            "LLM review SKIPPED — RPS-weighted improvement "
                            "is %.2f%% (not positive). Fix is genuinely "
                            "unhelpful even when weighted by throughput volume "
                            "(simple avg: %+.2f%%, degraded: %s)",
                            weighted_pct, simple_pct,
                            ", ".join(f"{w}={d:+.1f}%" for w, d in degraded.items())
                            if degraded else "none",
                        )

                Display.fix_comparison(
                    idx + 1, len(fixes), fix.get("description", ""),
                    fix.get("tool", ""), fix.get("params", {}),
                    baseline, current_rps, keep, pct,
                    llm_override=llm_overridden,
                )
                if keep:
                    applied.append((fix.get("description", ""), round(pct, 2)))
                    baseline = current_rps
                else:
                    rejected.append((fix.get("description", ""), round(pct, 2)))
                    applier.rollback()
                self.tracker.log_fix(fix.get("description", ""), fix.get("tool", ""), keep, pct)
                self._current_applier = None
                self._partial_state["applied_fixes"]  = applied
                self._partial_state["rejected_fixes"] = rejected

        except ValueError as e:
            # No-op at apply time (state changed since plan was built) — skip silently
            logger.info("remediate_fix [%d] skipped (no-op at apply time): %s", idx, e)
            rejected.append((fix.get("description", ""), 0.0))
        except Exception as e:
            logger.error("remediate_fix [%d] failed: %s", idx, e)
            rejected.append((fix.get("description", ""), 0.0))

        return {**state, "fix_index": idx + 1, "baseline_rps": baseline,
                "applied_fixes": applied, "rejected_fixes": rejected}

    # --- routing ---

    @staticmethod
    def _route_deploy(state: RCAState) -> str:
        return "error" if state.get("error") else "run_benchmark"

    @staticmethod
    def _route_benchmark(state: RCAState) -> str:
        return "error" if state.get("error") else "analyze_network"

    def _route_remediate(self, state: RCAState) -> str:
        if state.get("error"):
            return "end"
        idx = state.get("fix_index", 0)
        if idx < len(state.get("fixes", [])) and idx < self.config.remediation_max_fixes:
            return "remediate_fix"
        return "end"

    # --- graph ---

    def _build_graph(self):
        g = StateGraph(RCAState)
        g.add_node("run_audit",        self._run_audit)
        g.add_node("run_benchmark",    self._run_benchmark)
        g.add_node("analyze_network",  self._analyze_network)
        g.add_node("analyze_kernel",   self._analyze_kernel)
        g.add_node("analyze_nginx",    self._analyze_nginx)
        g.add_node("merge_fixes",      self._merge_fixes)
        g.add_node("remediate_fix",    self._remediate_fix)
        g.set_entry_point("run_audit")
        g.add_conditional_edges("run_audit",       self._route_deploy,
                                {"run_benchmark": "run_benchmark", "error": END})
        g.add_conditional_edges("run_benchmark",   self._route_benchmark,
                                {"analyze_network": "analyze_network", "error": END})
        g.add_edge("analyze_network",  "analyze_kernel")
        g.add_edge("analyze_kernel",   "analyze_nginx")
        g.add_edge("analyze_nginx",    "merge_fixes")
        g.add_conditional_edges("merge_fixes",     self._route_remediate,
                                {"remediate_fix": "remediate_fix", "end": END})
        g.add_conditional_edges("remediate_fix",   self._route_remediate,
                                {"remediate_fix": "remediate_fix", "end": END})
        return g.compile()

    def run(self) -> None:
        session_id = str(uuid4())
        logger.info("Session ID: %s", session_id)
        self.tracker.start(session_id)
        initial: RCAState = {
            "session_id": session_id, "similar_cases": "", "live_audit_output": "",
            "audit_output": "", "benchmark_results": "", "baseline_rps": {},
            "network_fixes": [], "network_summary": "",
            "kernel_fixes":  [], "kernel_summary":  "",
            "nginx_fixes":   [],
            "rca_report": "", "fixes": [], "fix_index": 0,
            "applied_fixes": [], "rejected_fixes": [],
            "total_input_tokens": 0, "total_output_tokens": 0,
            "llm_calls": [], "error": "",
        }
        result = self.graph.invoke(initial)

        if result["error"]:
            logger.error("Agent failed: %s", result["error"])
            return

        self.reporter.save(result["rca_report"], result["session_id"])

        # Save example with full remediation outcomes for DSPy optimization
        try:
            self.analyzer.save_example(
                result["audit_output"], result["rca_report"],
                result.get("benchmark_results", ""),
                applied_fixes=result["applied_fixes"],
                rejected_fixes=result["rejected_fixes"],
            )
        except Exception as e:
            logger.error("Failed to save example: %s", e)

        # Store run in semantic memory for future retrieval
        try:
            self.memory.add(
                result["session_id"], result["audit_output"],
                result.get("benchmark_results", ""), result["rca_report"],
                result["applied_fixes"], result["rejected_fixes"],
            )
        except Exception as e:
            logger.error("Failed to store case in semantic memory: %s", e)

        # Trigger BootstrapFewShot optimization if enough new examples
        if self.optimizer.should_optimize(DSPY_DIR / "examples.jsonl"):
            logger.info("Optimization triggered — running BootstrapFewShot...")
            try:
                self.optimizer.optimize_rca(self.analyzer, DSPY_DIR)
            except Exception as e:
                logger.error("Optimization failed: %s", e)

        in_tok  = result.get("total_input_tokens", 0)
        out_tok = result.get("total_output_tokens", 0)
        self.tracker.log_final(
            result["applied_fixes"], result["rejected_fixes"],
            in_tok, out_tok,
            session_dir=REPORTS_DIR / result["session_id"],
        )
        self.tracker.end()
        Display.llm_calls_summary(result.get("llm_calls", []))
        Display.run_summary(
            result["rca_report"],
            result["applied_fixes"],
            result["rejected_fixes"],
            in_tok, out_tok,
        )

        # Final extended benchmark if any fixes were accepted
        if result["applied_fixes"]:
            dur = self.config.benchmark_final_duration_minutes
            logger.info("Running final %d-minute benchmark with all accepted fixes applied...", dur)
            try:
                raw_final = self.benchmark.run_final(dur)
                Display.benchmark_results(raw_final)
                final_path = REPORTS_DIR / result["session_id"] / "final_benchmark.txt"
                final_path.parent.mkdir(parents=True, exist_ok=True)
                final_path.write_text(raw_final)
                logger.info("Final benchmark saved to %s", final_path)
            except Exception as e:
                logger.error("Final benchmark failed: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config    = Config(CONFIG_PATH)
    log_level = getattr(logging, config.log_level, logging.INFO)
    logging.getLogger().setLevel(log_level)

    LOGS_DIR.mkdir(exist_ok=True)
    log_file     = LOGS_DIR / f"audit_rca_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    logger.info("=" * 60)
    logger.info("slayMetrics Agent starting")
    logger.info("Log level : %s", config.log_level)
    logger.info("Log file  : %s", log_file)
    logger.info("=" * 60)

    agent = RCAAgent(config)
    agent.analyzer.configure()
    agent.run()


if __name__ == "__main__":
    main()
