import logging
import re
from datetime import datetime

from prettytable import PrettyTable

logger = logging.getLogger("slayMetrics.display")


class Display:
    """Logging-based output helpers."""

    @staticmethod
    def audit_summary(host: str, script: str, output: str) -> None:
        t = PrettyTable()
        t.field_names = ["Field", "Value"]
        t.align["Field"] = "l"
        t.align["Value"] = "l"
        t.add_rows([
            ["Timestamp",      datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
            ["DUT Host",       host],
            ["Script",         script],
            ["Bytes captured", f"{len(output):,}"],
            ["Lines captured", f"{len(output.splitlines()):,}"],
        ])
        logger.info("Audit Capture Summary\n%s", t.get_string(title="Audit Capture Summary"))

    @staticmethod
    def benchmark_results(results: str) -> None:
        workload_re = re.compile(r"\[\d+/\d+\]\s+(\w+):")
        rps_re      = re.compile(r"rps=([\d.]+)")
        latency_re  = re.compile(r"latency=([\d.]+\w+)")
        p99_re      = re.compile(r"99%\s+([\d.]+\w+)")

        rows: list[list[str]] = []
        current_workload: str | None = None
        current_p99: str = "—"

        for line in results.splitlines():
            m = workload_re.search(line)
            if m:
                current_workload = m.group(1)
                current_p99 = "—"
                continue
            p99_m = p99_re.search(line)
            if p99_m:
                current_p99 = p99_m.group(1)
            rps_m = rps_re.search(line)
            lat_m = latency_re.search(line)
            if rps_m and lat_m and current_workload:
                rows.append([current_workload, rps_m.group(1), lat_m.group(1), current_p99])
                current_workload = None

        if not rows:
            logger.info("Benchmark Results\n%s", results)
            return

        t = PrettyTable()
        t.field_names = ["Workload", "RPS", "Avg Latency", "P99 Latency"]
        t.align["Workload"] = "l"
        t.align["RPS"] = "r"
        t.align["Avg Latency"] = "r"
        t.align["P99 Latency"] = "r"
        t.add_rows(rows)
        logger.info("Benchmark Summary\n%s", t.get_string(title="Benchmark Results"))

    @staticmethod
    def fix_comparison(fix_num: int, total: int, description: str,
                       tool: str, params: dict,
                       baseline: dict[str, float], current: dict[str, float],
                       keep: bool, pct: float,
                       llm_override: bool = False) -> None:
        if llm_override:
            verdict = "ACCEPTED (LLM override)"
        else:
            verdict = "ACCEPTED" if keep else "REJECTED"
        params_str = ", ".join(f"{k}={v}" for k, v in params.items())
        t = PrettyTable()
        t.field_names = ["Workload", "Baseline RPS", "Current RPS", "Delta %"]
        t.align["Workload"] = "l"
        t.align["Baseline RPS"] = "r"
        t.align["Current RPS"] = "r"
        t.align["Delta %"] = "r"
        for workload in sorted(set(baseline) | set(current)):
            b = baseline.get(workload, 0.0)
            c = current.get(workload, 0.0)
            delta = (c - b) / b * 100 if b else 0.0
            t.add_row([workload, f"{b:.1f}", f"{c:.1f}", f"{delta:+.1f}%"])
        title = f"Fix {fix_num}/{total} [{verdict} {pct:+.1f}%]: {description}"
        logger.info("Fix Comparison — tool=%s  target: %s\n%s",
                    tool, params_str, t.get_string(title=title))

    @staticmethod
    def fix_plan(fixes: list[dict]) -> None:
        t = PrettyTable()
        t.field_names = ["#", "Tier", "Tool", "Description", "Current", "Target"]
        t.align["#"] = "r"
        t.align["Tier"] = "c"
        t.align["Tool"] = "l"
        t.align["Description"] = "l"
        t.align["Current"] = "l"
        t.align["Target"] = "l"
        for i, fix in enumerate(fixes, 1):
            params = fix.get("params", {})
            target = ", ".join(f"{k}={v}" for k, v in params.items())
            current = fix.get("current_value", "—")
            t.add_row([i, fix.get("tier", "?"), fix.get("tool", ""),
                       fix.get("description", ""), current, target])
        logger.info("LLM Recommended Fixes\n%s",
                    t.get_string(title=f"Recommended Fixes ({len(fixes)} total)"))

    @staticmethod
    def llm_summary(host: str, model: str, output: str,
                    input_tokens: int, output_tokens: int) -> None:
        t = PrettyTable()
        t.field_names = ["Field", "Value"]
        t.align["Field"] = "l"
        t.align["Value"] = "l"
        t.add_rows([
            ["Timestamp",       datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
            ["DUT Host",        host],
            ["LLM Model",       model],
            ["Bytes sent",      f"{len(output):,}"],
            ["Lines sent",      f"{len(output.splitlines()):,}"],
            ["Tokens (input)",  f"{input_tokens:,}"],
            ["Tokens (output)", f"{output_tokens:,}"],
            ["Tokens (total)",  f"{input_tokens + output_tokens:,}"],
        ])
        logger.info("LLM RCA Request Summary\n%s",
                    t.get_string(title="LLM RCA Request Summary"))

    @staticmethod
    def llm_calls_summary(calls: list) -> None:
        """Print per-domain LLM call stats + totals."""
        if not calls:
            return
        t = PrettyTable()
        t.field_names = ["Domain", "Time (s)", "Input tok", "Output tok", "Total tok", "Fixes"]
        t.align["Domain"] = "l"
        t.align["Time (s)"] = "r"
        t.align["Input tok"] = "r"
        t.align["Output tok"] = "r"
        t.align["Total tok"] = "r"
        t.align["Fixes"] = "r"
        total_in = total_out = total_elapsed = 0
        for domain, elapsed, in_tok, out_tok, num_fixes in calls:
            t.add_row([domain, f"{elapsed:.1f}", f"{in_tok:,}", f"{out_tok:,}",
                       f"{in_tok + out_tok:,}", num_fixes])
            total_in += in_tok
            total_out += out_tok
            total_elapsed += elapsed
        t.add_row(["─" * 7, "─" * 8, "─" * 9, "─" * 10, "─" * 9, "─" * 5])
        t.add_row(["TOTAL", f"{total_elapsed:.1f}", f"{total_in:,}", f"{total_out:,}",
                   f"{total_in + total_out:,}", sum(c[4] for c in calls)])
        logger.info("LLM Calls Summary\n%s", t.get_string(title="LLM Analysis Summary"))

    @staticmethod
    def live_analysis(analysis: str) -> None:
        if analysis:
            logger.info("Live Benchmark Analysis\n%s", analysis)

    @staticmethod
    def run_summary(rca_report: str, applied: list, rejected: list,
                    in_tok: int, out_tok: int) -> None:
        # Extract up to 200 words from the RCA report for the console summary
        words = rca_report.split()
        snippet = " ".join(words[:200])
        if len(words) > 200:
            snippet += " ..."
        logger.info("RCA Summary\n%s\n%s\n%s", "=" * 70, snippet, "=" * 70)

        # Remediation results table
        t = PrettyTable()
        t.field_names = ["Result", "Fix", "Impact %"]
        t.align["Result"] = "c"
        t.align["Fix"] = "l"
        t.align["Impact %"] = "r"
        for desc, pct in applied:
            t.add_row(["APPLIED", desc, f"+{pct:.1f}%"])
        for desc, pct in rejected:
            t.add_row(["REJECTED", desc, f"{pct:+.1f}%"])
        logger.info("Remediation Results\n%s",
                    t.get_string(title=f"Remediation Results — "
                                       f"{len(applied)} applied / {len(rejected)} rejected | "
                                       f"Tokens: {in_tok + out_tok:,}"))
