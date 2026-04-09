import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("slayMetrics.report")


class ReportWriter:
    """Saves RCA reports and final run summaries."""

    def __init__(self, reports_dir: Path):
        self.reports_dir = reports_dir

    def save(self, rca_report: str, session_id: str) -> Path:
        session_dir = self.reports_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / "rca_report.md"
        path.write_text(rca_report)
        logger.info("Report saved to %s", path)
        return path

    def generate_final_report(
        self,
        session_id: str,
        config,
        baseline_rps: dict[str, float],
        final_rps: dict[str, float] | None,
        applied_fixes: list[tuple[str, float]],
        rejected_fixes: list[tuple[str, float]],
        fixes: list[dict],
        llm_calls: list,
        rca_report: str,
        in_tok: int,
        out_tok: int,
        run_start: datetime,
        run_end: datetime,
    ) -> Path:
        """Generate a comprehensive final_report.md for the run."""
        session_dir = self.reports_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        elapsed = run_end - run_start
        minutes = int(elapsed.total_seconds() // 60)
        seconds = int(elapsed.total_seconds() % 60)

        lines: list[str] = []
        lines.append("# SlayMetrics — Final Run Report")
        lines.append("")
        lines.append("## Run Metadata")
        lines.append("")
        lines.append(f"| Field | Value |")
        lines.append(f"|-------|-------|")
        lines.append(f"| Session ID | `{session_id}` |")
        lines.append(f"| Date | {run_start.strftime('%Y-%m-%d %H:%M:%S')} |")
        lines.append(f"| DUT Host | `{config.dut_host}` |")
        lines.append(f"| LLM Model | `{config.llm_model}` |")
        lines.append(f"| Total Runtime | {minutes}m {seconds}s |")
        lines.append(f"| Total Tokens | {in_tok + out_tok:,} (in: {in_tok:,} / out: {out_tok:,}) |")
        lines.append("")

        # --- LLM Calls ---
        if llm_calls:
            lines.append("## LLM Analysis Calls")
            lines.append("")
            lines.append("| Domain | Time (s) | Input Tokens | Output Tokens | Fixes Found |")
            lines.append("|--------|----------|-------------|--------------|-------------|")
            total_elapsed = 0.0
            for domain, elapsed_s, i_tok, o_tok, n_fixes in llm_calls:
                lines.append(f"| {domain} | {elapsed_s:.1f} | {i_tok:,} | {o_tok:,} | {n_fixes} |")
                total_elapsed += elapsed_s
            lines.append(f"| **TOTAL** | **{total_elapsed:.1f}** | **{in_tok:,}** | **{out_tok:,}** | **{sum(c[4] for c in llm_calls)}** |")
            lines.append("")

        # --- Baseline Benchmark ---
        lines.append("## Baseline Benchmark (Before Fixes)")
        lines.append("")
        if baseline_rps:
            lines.append("| Workload | RPS |")
            lines.append("|----------|-----|")
            for w in sorted(baseline_rps):
                lines.append(f"| {w} | {baseline_rps[w]:,.1f} |")
        else:
            lines.append("_No baseline data available._")
        lines.append("")

        # --- Fix Plan ---
        lines.append(f"## Fix Plan ({len(fixes)} fixes evaluated)")
        lines.append("")
        if fixes:
            lines.append("| # | Tier | Tool | Description | Params |")
            lines.append("|---|------|------|-------------|--------|")
            for i, fix in enumerate(fixes, 1):
                params = ", ".join(f"{k}={v}" for k, v in fix.get("params", {}).items())
                lines.append(f"| {i} | {fix.get('tier', '?')} | {fix.get('tool', '')} | {fix.get('description', '')} | {params} |")
        lines.append("")

        # --- Applied Fixes ---
        lines.append(f"## Applied Fixes ({len(applied_fixes)})")
        lines.append("")
        if applied_fixes:
            lines.append("| Fix | Improvement |")
            lines.append("|-----|-------------|")
            for desc, pct in applied_fixes:
                lines.append(f"| {desc} | {pct:+.1f}% |")
        else:
            lines.append("_No fixes were applied._")
        lines.append("")

        # --- Rejected Fixes ---
        lines.append(f"## Rejected Fixes ({len(rejected_fixes)})")
        lines.append("")
        if rejected_fixes:
            lines.append("| Fix | Impact |")
            lines.append("|-----|--------|")
            for desc, pct in rejected_fixes:
                lines.append(f"| {desc} | {pct:+.1f}% |")
        else:
            lines.append("_No fixes were rejected._")
        lines.append("")

        # --- Final Benchmark ---
        lines.append("## Final Benchmark (After Fixes)")
        lines.append("")
        if final_rps and baseline_rps:
            lines.append("| Workload | Baseline RPS | Final RPS | Delta % |")
            lines.append("|----------|-------------|-----------|---------|")
            for w in sorted(set(baseline_rps) | set(final_rps)):
                b = baseline_rps.get(w, 0.0)
                f_val = final_rps.get(w, 0.0)
                delta = (f_val - b) / b * 100 if b else 0.0
                lines.append(f"| {w} | {b:,.1f} | {f_val:,.1f} | {delta:+.1f}% |")
            # Overall improvement
            common = set(baseline_rps) & set(final_rps)
            if common:
                total_baseline = sum(baseline_rps[w] for w in common)
                total_final = sum(final_rps[w] for w in common)
                overall = (total_final - total_baseline) / total_baseline * 100 if total_baseline else 0.0
                lines.append(f"| **TOTAL** | **{total_baseline:,.1f}** | **{total_final:,.1f}** | **{overall:+.1f}%** |")
        elif not applied_fixes:
            lines.append("_No fixes applied — final benchmark skipped._")
        else:
            lines.append("_Final benchmark data not available._")
        lines.append("")

        # --- RCA Summary ---
        lines.append("## RCA Summary")
        lines.append("")
        lines.append(rca_report if rca_report else "_No RCA report generated._")
        lines.append("")

        # --- Footer ---
        lines.append("---")
        lines.append(f"_Generated by SlayMetrics on {run_end.strftime('%Y-%m-%d %H:%M:%S')}_")
        lines.append("")

        path = session_dir / "final_report.md"
        path.write_text("\n".join(lines))
        logger.info("Final report saved to %s", path)
        return path
