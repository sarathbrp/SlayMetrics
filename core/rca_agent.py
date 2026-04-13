"""RCAAgent: LangGraph orchestration + state management."""

from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import StateGraph, END

from .config import Config
from .ssh import RemoteExecutor
from .analyzer import RCAAnalyzer
from .benchmark import BenchmarkRunner
from .evaluator import Evaluator
from .fix_evaluator import FixEvaluatorLLM
from .fix_applier import FixApplier
from .display import Display
from .report import ReportWriter
from .optimizer import FeedbackOptimizer
from .memory import SemanticMemory
from .live_sampler import LiveSampler
from .domain_analyzers import NetworkAnalyzer, KernelAnalyzer, NginxAnalyzer
from .tracking import RunTracker
from .orchestrator import FleetTarget, InstallerOrchestrator
from .investigator import SREInvestigator
from .constants import PROMPTS_DIR, DSPY_DIR, REPORTS_DIR, SCRIPTS_DIR, REMOTE_TMP, AUDIT_SCRIPT
from .comparison import run_comparisons
from . import rca_nodes
from . import rca_analysis
from . import investigation_node

logger = logging.getLogger("slayMetrics.agent")


class RCAState(TypedDict):
    session_id: str
    similar_cases: str            # retrieved from semantic memory
    audit_output: str
    benchmark_results: str        # latest benchmark output (raw text)
    live_audit_output: str        # dynamic metrics collected during benchmark
    investigation_notes: str      # accumulated findings from SRE investigation
    baseline_rps: dict            # {workload: float} from initial benchmark
    network_fixes: list
    network_summary: str          # chained → analyze_kernel
    kernel_fixes: list
    kernel_summary: str           # chained → analyze_nginx
    nginx_fixes: list
    rca_report: str               # combined summaries
    fixes: list                   # merged + sorted from all 3 domains
    fix_index: int
    applied_fixes: list           # [(description, improvement_pct)]
    rejected_fixes: list          # [(description, improvement_pct)]
    total_input_tokens: int
    total_output_tokens: int
    llm_calls: list               # [(domain, elapsed_s, in_tok, out_tok, num_fixes)]
    error: str


class RCAAgent:
    """LangGraph agent: audit → benchmark → RCA → remediation loop."""

    def __init__(self, config: Config, audit_only: bool = False,
                 orchestrator: InstallerOrchestrator | None = None,
                 target: FleetTarget | None = None):
        self.config          = config
        self.audit_only      = audit_only
        self.orchestrator    = orchestrator
        self.target          = target
        self.analyzer        = RCAAnalyzer(config, PROMPTS_DIR, DSPY_DIR)
        self.net_analyzer    = NetworkAnalyzer(config, PROMPTS_DIR)
        self.kernel_analyzer = KernelAnalyzer(config, PROMPTS_DIR)
        self.nginx_analyzer  = NginxAnalyzer(config, PROMPTS_DIR)
        self.investigator     = SREInvestigator(config, PROMPTS_DIR)
        self.benchmark       = BenchmarkRunner(config)
        self.evaluator       = Evaluator()
        self.fix_reviewer    = FixEvaluatorLLM(config, PROMPTS_DIR)
        self.reporter        = ReportWriter(REPORTS_DIR)
        self.optimizer       = FeedbackOptimizer(
            min_new_examples=config.optimization_min_new_examples,
            max_bootstrap_demos=config.optimization_max_bootstrap_demos,
        )
        self.sampler         = LiveSampler(config, SCRIPTS_DIR, REMOTE_TMP, self._executor)
        self.memory          = SemanticMemory(
            persist_dir=DSPY_DIR / "chroma",
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
            embed_model=config.llm_embed_model,
        )
        self.tracker            = RunTracker(config)
        self._current_applier:  FixApplier | None = None
        self._partial_state:    dict              = {}
        self.graph              = self._build_graph()
        self._setup_signal_handlers()

    def _executor(self) -> RemoteExecutor:
        return RemoteExecutor(
            host=self.config.dut_host, user=self.config.dut_user,
            key_path=self.config.dut_key, port=self.config.dut_port,
            timeout=self.config.dut_timeout,
        )

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
        rca_nodes.save_partial_state(self)
        logger.info("Exiting.")
        sys.exit(0)

    @staticmethod
    def _route_deploy(state: RCAState) -> str:
        return "error" if state.get("error") else "preflight_check"

    @staticmethod
    def _route_preflight(state: RCAState) -> str:
        return "error" if state.get("error") else "run_benchmark"

    @staticmethod
    def _route_benchmark(state: RCAState) -> str:
        return "error" if state.get("error") else "investigate"

    @staticmethod
    def _route_investigate(state: RCAState) -> str:
        return "error" if state.get("error") else "analyze_network"

    def _route_remediate(self, state: RCAState) -> str:
        if state.get("error"):
            return "end"
        if self.audit_only:
            logger.info("Audit mode enabled — skipping remediation loop.")
            return "end"
        idx = state.get("fix_index", 0)
        if idx < len(state.get("fixes", [])) and idx < self.config.remediation_max_fixes:
            return "remediate_fix"
        return "end"

    def _build_graph(self):
        g = StateGraph(RCAState)
        g.add_node("run_audit",        lambda s: rca_nodes.run_audit(s, self))
        g.add_node("preflight_check", lambda s: rca_nodes.preflight_check(s, self))
        g.add_node("run_benchmark",   lambda s: rca_nodes.run_benchmark(s, self))
        g.add_node("investigate",     lambda s: investigation_node.investigate(s, self))
        g.add_node("analyze_network", lambda s: rca_analysis.analyze_network(s, self))
        g.add_node("analyze_kernel",  lambda s: rca_analysis.analyze_kernel(s, self))
        g.add_node("analyze_nginx",   lambda s: rca_analysis.analyze_nginx(s, self))
        g.add_node("merge_fixes",     lambda s: rca_nodes.merge_fixes(s, self))
        g.add_node("remediate_fix",   lambda s: rca_nodes.remediate_fix(s, self))
        g.set_entry_point("run_audit")
        g.add_conditional_edges("run_audit",       self._route_deploy,
                                {"preflight_check": "preflight_check", "error": END})
        g.add_conditional_edges("preflight_check", self._route_preflight,
                                {"run_benchmark": "run_benchmark", "error": END})
        g.add_conditional_edges("run_benchmark",   self._route_benchmark,
                                {"investigate": "investigate", "error": END})
        g.add_conditional_edges("investigate",     self._route_investigate,
                                {"analyze_network": "analyze_network", "error": END})
        g.add_edge("analyze_network", "analyze_kernel")
        g.add_edge("analyze_kernel",  "analyze_nginx")
        g.add_edge("analyze_nginx",   "merge_fixes")
        g.add_conditional_edges("merge_fixes",   self._route_remediate,
                                {"remediate_fix": "remediate_fix", "end": END})
        g.add_conditional_edges("remediate_fix", self._route_remediate,
                                {"remediate_fix": "remediate_fix", "end": END})
        return g.compile()

    def run(self, initial_state: dict[str, Any] | None = None) -> RCAState:
        run_start  = datetime.now()
        session_id = str(uuid4())
        logger.info("Session ID: %s", session_id)
        self.tracker.start(session_id)
        initial: RCAState = {
            "session_id": session_id, "similar_cases": "", "live_audit_output": "",
            "audit_output": "", "benchmark_results": "", "investigation_notes": "",
            "baseline_rps": {},
            "network_fixes": [], "network_summary": "",
            "kernel_fixes":  [], "kernel_summary":  "",
            "nginx_fixes":   [],
            "rca_report": "", "fixes": [], "fix_index": 0,
            "applied_fixes": [], "rejected_fixes": [],
            "total_input_tokens": 0, "total_output_tokens": 0,
            "llm_calls": [], "error": "",
        }
        if initial_state:
            initial.update(initial_state)
        result = self.graph.invoke(initial)

        if result["error"]:
            logger.error("Agent failed: %s", result["error"])
            return result

        self.reporter.save(result["rca_report"], result["session_id"])
        self._persist_learning(result)

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
            result["rca_report"], result["applied_fixes"], result["rejected_fixes"],
            in_tok, out_tok,
            audit_only=self.audit_only, fixes=result.get("fixes", []),
        )

        return self._finalize_run(result, run_start, in_tok, out_tok)

    def _persist_learning(self, result: RCAState) -> None:
        """Save DSPy example + semantic memory + trigger optimization (skipped in audit-only mode)."""
        if self.audit_only:
            logger.info(
                "Audit-only mode: skipping remediation-learning persistence "
                "(examples, semantic memory, optimization)."
            )
            return
        try:
            self.analyzer.save_example(
                result["audit_output"], result["rca_report"],
                result.get("benchmark_results", ""),
                applied_fixes=result["applied_fixes"],
                rejected_fixes=result["rejected_fixes"],
            )
        except Exception as e:
            logger.error("Failed to save example: %s", e)
        try:
            self.memory.add(
                result["session_id"], result["audit_output"],
                result.get("benchmark_results", ""), result["rca_report"],
                result["applied_fixes"], result["rejected_fixes"],
            )
        except Exception as e:
            logger.error("Failed to store case in semantic memory: %s", e)
        if self.optimizer.should_optimize(DSPY_DIR / "examples.jsonl"):
            logger.info("Optimization triggered — running BootstrapFewShot...")
            try:
                self.optimizer.optimize_rca(self.analyzer, DSPY_DIR)
            except Exception as e:
                logger.error("Optimization failed: %s", e)

    def _finalize_run(
        self, result: RCAState, run_start: datetime, in_tok: int, out_tok: int,
    ) -> RCAState:
        """Run final benchmark (if fixes applied) and generate the final report."""
        final_rps = None
        if result["applied_fixes"]:
            dur = self.config.benchmark_final_duration_minutes
            logger.info("Running final %d-minute benchmark with all accepted fixes applied...", dur)
            try:
                raw_final = self.benchmark.run_final(dur)
                Display.benchmark_results(raw_final)
                final_rps = self.evaluator.parse_rps(raw_final)
                final_path = REPORTS_DIR / result["session_id"] / "final_benchmark.txt"
                final_path.parent.mkdir(parents=True, exist_ok=True)
                final_path.write_text(raw_final)
                logger.info("Final benchmark saved to %s", final_path)
            except Exception as e:
                logger.error("Final benchmark failed: %s", e)
        try:
            report_path = self.reporter.generate_final_report(
                session_id=result["session_id"], config=self.config,
                baseline_rps=result["baseline_rps"], final_rps=final_rps,
                applied_fixes=result["applied_fixes"], rejected_fixes=result["rejected_fixes"],
                fixes=result.get("fixes", []), llm_calls=result.get("llm_calls", []),
                rca_report=result.get("rca_report", ""),
                in_tok=in_tok, out_tok=out_tok,
                run_start=run_start, run_end=datetime.now(),
            )
            logger.info("Final report: %s", report_path)
        except Exception as e:
            logger.error("Failed to generate final report: %s", e)

        # Run benchmark comparison against baseline and vanilla
        run_comparisons(self.config)

        return result
