from __future__ import annotations

import json
import statistics

from rich.console import Console
from rich.panel import Panel

import agents.analyzer as analyzer_agent
import agents.benchmark as benchmark_agent
import agents.collector as collector_agent
import agents.remediation as remediation_agent
import core.decision_engine as engine
import core.reporter as reporter
import rhel.system_checks as system_checks
from agents import AgentDeps

console = Console()

PAYLOAD_SIZES = ["small", "medium", "large"]


async def run(model, deps: AgentDeps) -> str:
    """Main orchestration loop. Returns path to final report."""
    cfg = deps.config
    session_id = deps.session_id
    memory = deps.memory
    max_iterations = cfg["agent"].get("max_iterations", 50)
    threshold = cfg["agent"].get("improvement_threshold_pct", 5)

    console.print(Panel(
        f"[bold green]SlayMetricsAgent[/bold green] starting\n"
        f"Session: {session_id}\n"
        f"Service: {cfg['service']['name']} on {cfg['target']['host']}",
        title="SlayMetricsAgent",
    ))

    # ── Step 1: RHEL system checks ──────────────────────────────────────────
    console.print("\n[bold]Step 1:[/bold] Running RHEL system checks...")
    checks = system_checks.run_all(deps.ssh, cfg["rhel"]["checks"])
    for chk in checks:
        status_color = {"ok": "green", "warning": "yellow", "critical": "red"}.get(
            chk.status, "white"
        )
        console.print(f"  [{status_color}]●[/{status_color}] {chk.name}: {chk.value[:80]}")
        memory.save_context(session_id, "system_check", chk.name,
                            chk.value, chk.recommendation)

    # ── Step 1.5: System fingerprint ─────────────────────────────────────────
    console.print("\n[bold]Step 1.5:[/bold] Collecting system fingerprint...")
    ssh = deps.ssh
    rhel_ver = ssh.execute("cat /etc/redhat-release 2>/dev/null || echo unknown").stdout.strip()
    kernel_ver = ssh.execute("uname -r").stdout.strip()
    cpu_cores_str = ssh.execute("nproc").stdout.strip()
    ram_kb_str = ssh.execute("grep MemTotal /proc/meminfo | awk '{print $2}'").stdout.strip()

    try:
        cpu_cores = int(cpu_cores_str)
    except ValueError:
        cpu_cores = 0
    try:
        ram_gb = int(ram_kb_str) // (1024 * 1024)
    except ValueError:
        ram_gb = 0

    memory.update_profile(session_id,
                          rhel_version=rhel_ver[:32],
                          kernel_version=kernel_ver[:64],
                          cpu_cores=cpu_cores,
                          ram_gb=ram_gb)
    console.print(f"  RHEL: {rhel_ver}, Kernel: {kernel_ver}, "
                  f"CPU: {cpu_cores} cores, RAM: {ram_gb} GB")

    # ── Step 2: Baseline benchmarks (small/medium/large) ─────────────────────
    console.print("\n[bold]Step 2:[/bold] Running baseline benchmarks (small/medium/large)...")
    bench_cfg = cfg["service"]["benchmark"]
    baselines = {}

    for size in PAYLOAD_SIZES:
        url = bench_cfg.get(f"{size}_file_url")
        if not url:
            continue
        result = await benchmark_agent.run(
            model, deps, duration=bench_cfg.get("duration", 30), url=url,
        )
        baselines[size] = result
        console.print(
            f"  Baseline ({size}): [cyan]{result.requests_per_sec:.1f}[/cyan] req/sec  "
            f"p99=[yellow]{result.latency_p99_ms:.1f}[/yellow]ms  "
            f"CPU=[dim]{result.cpu_pct:.1f}%[/dim]  MEM=[dim]{result.mem_mb:.0f}MB[/dim]"
        )
        memory.save_context(session_id, "benchmark", f"baseline_{size}",
                            json.dumps({"rps": result.requests_per_sec,
                                        "p99": result.latency_p99_ms,
                                        "cpu_pct": result.cpu_pct,
                                        "mem_mb": result.mem_mb,
                                        "url": url, "size": size}),
                            f"Baseline {size}: {result.requests_per_sec:.1f} RPS")

    # Use small as primary baseline for hypothesis loop
    baseline = baselines.get("small", next(iter(baselines.values())))
    memory.update_profile(session_id,
                          baseline_rps=baseline.requests_per_sec,
                          best_rps=baseline.requests_per_sec)

    # ── Step 3: Collect initial data ─────────────────────────────────────────
    console.print("\n[bold]Step 3:[/bold] Collecting service config and metrics...")
    await collector_agent.run(
        model, deps,
        "Read the service config, fetch recent logs, and collect live metrics. "
        "Summarize what you find.",
    )

    # ── Step 4: Seed hypothesis queue ────────────────────────────────────────
    hypotheses = deps.adapter.get_hypothesis_queue()
    engine.populate(session_id, memory, hypotheses)
    console.print(f"\n[bold]Step 4:[/bold] {len(hypotheses)} hypotheses queued")

    # ── Step 5: Hypothesis loop ──────────────────────────────────────────────
    console.print("\n[bold]Step 5:[/bold] Starting hypothesis-driven diagnosis loop...\n")
    iteration = 0
    best_rps = baseline.requests_per_sec

    while not engine.is_exhausted(session_id, memory) and iteration < max_iterations:
        iteration += 1
        hypothesis = engine.next_hypothesis(session_id, memory)
        if not hypothesis:
            break

        name = hypothesis["name"]
        console.print(f"[dim]  [{iteration}][/dim] Testing hypothesis: [bold]{name}[/bold]")

        # Mark running
        memory.mark_hypothesis(session_id, name, "running")

        # Analyze
        analysis = await analyzer_agent.run(
            model, deps, hypothesis=name,
            context_summary=_build_context_summary(checks, baseline, best_rps),
        )

        if analysis.skip:
            console.print(f"       [dim]-> skipped — already addressed in memory[/dim]")
            engine.mark_skipped(session_id, memory, name, "seen in memory")
            continue

        console.print(f"       -> {analysis.reasoning[:120]}")
        console.print(f"       -> action: [yellow]{analysis.recommended_action[:100]}[/yellow]")

        # Remediate
        fix = await remediation_agent.run(
            model, deps,
            analysis_summary=f"{analysis.symptom}: {analysis.root_cause}",
            recommended_action=analysis.recommended_action,
        )

        if fix.success and fix.impact_pct >= threshold:
            best_rps = fix.after_rps
            memory.update_profile(session_id, best_rps=best_rps)
            console.print(
                f"       [green]OK[/green] Fixed! "
                f"RPS: {fix.before_rps:.1f} -> {fix.after_rps:.1f} "
                f"([bold green]{fix.impact_pct:+.1f}%[/bold green])"
            )
            engine.mark_done(session_id, memory, name,
                             f"+{fix.impact_pct:.1f}% improvement")
        elif fix.success:
            console.print(
                f"       [yellow]~[/yellow] Applied but minimal impact "
                f"({fix.impact_pct:+.1f}%)"
            )
            engine.mark_done(session_id, memory, name,
                             f"minimal impact {fix.impact_pct:+.1f}%")
        else:
            console.print(f"       [red]X[/red] Fix failed")
            engine.mark_done(session_id, memory, name, "fix failed")

    # ── Step 5.5: Sustained stability test ───────────────────────────────────
    stability_cfg = cfg["agent"].get("stability", {})
    stability_data = None

    if stability_cfg.get("enabled", False):
        total_duration = stability_cfg.get("duration_sec", 1800)
        interval = stability_cfg.get("sample_interval_sec", 60)
        url_key = stability_cfg.get("url_key", "small_file_url")
        stability_url = bench_cfg.get(url_key, "http://localhost/")
        num_samples = total_duration // interval

        console.print(f"\n[bold]Step 5.5:[/bold] Running {total_duration // 60}-minute "
                      f"stability test ({num_samples} samples)...")
        samples = []

        for i in range(num_samples):
            result = deps.adapter.benchmark(duration=interval, url=stability_url)
            samples.append(result.requests_per_sec)
            console.print(f"  Sample {i + 1}/{num_samples}: "
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
        console.print(f"  Result: mean={mean_rps:.1f}  stdev={stdev_rps:.1f}  CV={cv:.1f}%")

    # ── Step 5.75: Final multi-payload benchmarks ────────────────────────────
    console.print("\n[bold]Step 5.75:[/bold] Running final benchmarks (small/medium/large)...")
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
            "p99": result.latency_p99_ms,
            "cpu_pct": result.cpu_pct,
            "mem_mb": result.mem_mb,
            "url": url,
        }
        console.print(
            f"  Final ({size}): [cyan]{result.requests_per_sec:.1f}[/cyan] req/sec  "
            f"p99=[yellow]{result.latency_p99_ms:.1f}[/yellow]ms  "
            f"CPU=[dim]{result.cpu_pct:.1f}%[/dim]  MEM=[dim]{result.mem_mb:.0f}MB[/dim]"
        )

    # ── Step 6: Generate report ───────────────────────────────────────────────
    console.print("\n[bold]Step 6:[/bold] Generating report...")

    if engine.is_exhausted(session_id, memory):
        console.print("[yellow]Hypothesis queue exhausted — escalating to human[/yellow]")
        memory.save_fact(session_id, "escalation", "hypothesis_queue",
                         "All hypotheses tested. See report for details.")
        memory.update_profile(session_id, status="escalated")
    else:
        memory.update_profile(session_id, status="completed")

    # Convert baselines for reporter
    baselines_data = {
        s: {"rps": b.requests_per_sec, "p99": b.latency_p99_ms,
            "cpu_pct": b.cpu_pct, "mem_mb": b.mem_mb,
            "url": bench_cfg.get(f"{s}_file_url", "")}
        for s, b in baselines.items()
    }

    report_path = reporter.generate(
        session_id, memory, deps.token_counter,
        baselines=baselines_data, finals=finals, stability=stability_data,
    )

    total_improvement = (
        ((best_rps - baseline.requests_per_sec) / baseline.requests_per_sec * 100)
        if baseline.requests_per_sec else 0.0
    )
    console.print(Panel(
        f"[bold green]Done![/bold green]\n"
        f"Baseline (small): {baseline.requests_per_sec:.1f} req/sec\n"
        f"Best (small):     {best_rps:.1f} req/sec\n"
        f"Improvement: [bold]{total_improvement:+.1f}%[/bold]\n"
        f"Report: {report_path}",
        title="SlayMetricsAgent Complete",
    ))
    return report_path


def _build_context_summary(checks, baseline, current_rps: float) -> str:
    warnings = [c for c in checks if c.status in ("warning", "critical")]
    warn_str = ", ".join(c.name for c in warnings) if warnings else "none"
    return (
        f"Baseline RPS: {baseline.requests_per_sec:.1f}. "
        f"Current best RPS: {current_rps:.1f}. "
        f"RHEL checks with warnings: {warn_str}."
    )
