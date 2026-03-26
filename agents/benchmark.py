from __future__ import annotations

from pydantic import BaseModel

from adapters.base import BenchmarkResult as AdapterBenchmarkResult
from agents import AgentDeps
from core.log import log


class BenchmarkOutput(BaseModel):
    requests_per_sec: float
    latency_p50_ms: float
    latency_p99_ms: float
    error_rate: float
    duration_sec: int
    url: str
    payload_size: str = ""
    cpu_pct: float = 0.0
    mem_mb: float = 0.0
    summary: str = ""


async def run(model, deps: AgentDeps, duration: int = 30, url: str = "") -> BenchmarkOutput:
    """Run benchmark directly — no LLM needed for this step."""
    bench_cfg = deps.config["service"]["benchmark"]
    target_url = url or bench_cfg.get("small_file_url", "http://localhost/")

    log("benchmark", f"Running wrk2 for {duration}s against {target_url}", "action")
    result: AdapterBenchmarkResult = deps.adapter.benchmark(duration, target_url)
    log(
        "benchmark",
        f"Result: {result.requests_per_sec:.1f} RPS, "
        f"p99={result.latency_p99_ms:.1f}ms, CPU={result.cpu_pct:.1f}%",
        "result",
    )

    deps.memory.save_context(
        deps.session_id,
        "benchmark",
        target_url,
        str(result.__dict__),
        f"RPS={result.requests_per_sec:.1f} p99={result.latency_p99_ms:.1f}ms",
    )
    deps.token_counter.tool_calls += 1

    return BenchmarkOutput(
        requests_per_sec=result.requests_per_sec,
        latency_p50_ms=result.latency_p50_ms,
        latency_p99_ms=result.latency_p99_ms,
        error_rate=result.error_rate,
        duration_sec=result.duration_sec,
        url=result.url,
        payload_size=result.payload_size,
        cpu_pct=result.cpu_pct,
        mem_mb=result.mem_mb,
        summary=(
            f"RPS={result.requests_per_sec:.1f} "
            f"p50={result.latency_p50_ms:.1f}ms "
            f"p99={result.latency_p99_ms:.1f}ms"
        ),
    )
