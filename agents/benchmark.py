from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from agents import AgentDeps
from adapters.base import BenchmarkResult as AdapterBenchmarkResult


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


def build(model) -> Agent:
    agent: Agent[AgentDeps, BenchmarkOutput] = Agent(
        model,
        deps_type=AgentDeps,
        result_type=BenchmarkOutput,
        system_prompt=(
            "You are a benchmarking agent. Run the benchmark tool and return structured results. "
            "Always include a one-sentence summary of what the numbers mean."
        ),
    )

    @agent.tool
    async def run_benchmark(ctx: RunContext[AgentDeps], duration: int = 30,
                            url: str = "") -> dict:
        """Run the service benchmark (wrk2/pgbench) and return raw metrics."""
        result: AdapterBenchmarkResult = ctx.deps.adapter.benchmark(duration, url)
        ctx.deps.memory.save_context(
            ctx.deps.session_id, "benchmark", url or "default",
            str(result.__dict__),
            f"RPS={result.requests_per_sec:.1f} p99={result.latency_p99_ms:.1f}ms",
        )
        ctx.deps.token_counter.tool_calls += 1
        return result.__dict__

    return agent


async def run(model, deps: AgentDeps, duration: int = 30,
              url: str = "") -> BenchmarkOutput:
    agent = build(model)
    bench_cfg = deps.config["service"]["benchmark"]
    target_url = url or bench_cfg.get("small_file_url", "http://localhost/")
    result = await agent.run(
        f"Run benchmark for {duration}s against {target_url}. "
        f"Return structured BenchmarkOutput.",
        deps=deps,
    )
    deps.token_counter.add(result.usage())
    return result.data
