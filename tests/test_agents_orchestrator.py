from __future__ import annotations

import asyncio
from types import SimpleNamespace

from adapters.base import BenchmarkResult
from agents import TokenCounter, analyzer, benchmark, collector, remediation
from core import orchestrator
from rhel.system_checks import CheckResult
from tools.ssh import SSHResult


class FakeMemory:
    def __init__(self):
        self.saved = []

    def save_context(self, *args):
        self.saved.append(args)

    def get_contexts(self, session_id, type=None, source_prefix=None, limit=None):
        del session_id, type, source_prefix, limit
        return [
            {
                "source": "baseline:post",
                "content": '{"summary":{"nginx_worker_count":112,"nginx_worker_cores":[0,1],"somaxconn":"4096","tcp_max_syn_backlog":"1024","ip_local_port_range":"32768 60999","rx_drop_total":0,"tx_drop_total":0,"tcp_established":1024}}',
            }
        ]

    def save_fact(self, **kwargs):
        self.saved.append(("fact", kwargs))

    def get_facts(self, session_id, type=None):
        if type == "fix":
            return []
        return [{"embedding": [1], "parameter": "p"}]

    def get_all_fixes_for_host(self, host):
        return [{"parameter": "sendfile", "after_value": "on", "impact_pct": 10.0}]

    def semantic_search(self, query, session_id=None, top_k=3):
        return [{"parameter": "doc", "reasoning": "text"}]

    def update_profile(self, *args, **kwargs):
        self.saved.append(("update", args, kwargs))

    def get_token_history(self):
        return []

    def get_profile(self, session_id):
        return {"service": "nginx", "host": "localhost"}

    def get_queue(self, session_id):
        return []


class FakeAdapter:
    def __init__(self):
        self.count = 0

    def benchmark(self, duration=30, url=""):
        self.count += 1
        return BenchmarkResult(
            100 + self.count, 1.0, 2.0, 0.0, duration, url=url, cpu_pct=10.0, mem_mb=20.0
        )

    def get_config(self):
        return {"raw": "worker_processes auto;", "path": "/etc/nginx/nginx.conf"}

    def get_logs(self, tail=100):
        return "line1\nline2"

    def get_metrics(self):
        return {"m1": "v1"}

    def apply_config(self, parameter, value):
        return True

    def reload(self):
        return True


class FakeSSH:
    def execute(self, command, timeout=None):
        del timeout
        mapping = {
            "cat /etc/redhat-release 2>/dev/null || echo unknown": "RHEL",
            "uname -r": "6.0",
            "nproc": "8",
            "grep MemTotal /proc/meminfo | awk '{print $2}'": str(8 * 1024 * 1024),
            "ethtool ens3 2>/dev/null | grep Speed || cat /sys/class/net/$(ip route | awk '/default/{print $5}')/speed 2>/dev/null": "1000Mb/s",
            "dd if=/dev/zero of=/tmp/disktest bs=1M count=256 oflag=direct 2>&1 | tail -1": "256 MB copied",
            "rm -f /tmp/disktest": "",
            "cmd": "output",
        }
        return SSHResult(mapping.get(command, ""), "", 0)


def _deps():
    return SimpleNamespace(
        adapter=FakeAdapter(),
        memory=FakeMemory(),
        ssh=FakeSSH(),
        session_id="s1",
        token_counter=TokenCounter(),
        config={
            "target": {"host": "localhost"},
            "service": {
                "name": "nginx",
                "benchmark": {
                    "small_file_url": "http://x/1",
                    "medium_file_url": "http://x/2",
                    "large_file_url": "http://x/3",
                    "duration": 1,
                },
            },
            "rhel": {"checks": ["cpu_governor"]},
            "agent": {
                "stability": {
                    "enabled": True,
                    "duration_sec": 2,
                    "sample_interval_sec": 1,
                    "url_key": "small_file_url",
                }
            },
        },
    )


def test_benchmark_and_collector_run():
    deps = _deps()
    out = asyncio.run(benchmark.run(None, deps, 5, "http://z"))
    assert out.requests_per_sec > 0
    collected = asyncio.run(collector.run(None, deps, "task"))
    assert "service_config" in collected.checks_run
    assert deps.token_counter.tool_calls >= 4


def test_analyzer_tools_and_run(monkeypatch):
    deps = _deps()
    agent = analyzer.build("model")
    query_memory = agent._function_toolset.tools["query_memory"].function
    get_past_facts = agent._function_toolset.tools["get_past_facts"].function
    run_diagnostic = agent._function_toolset.tools["run_diagnostic_command"].function
    ctx = SimpleNamespace(deps=deps)
    assert asyncio.run(query_memory(ctx, "symptom"))[0]["parameter"] == "doc"
    assert asyncio.run(get_past_facts(ctx))[0]["parameter"] == "p"
    assert "output" in asyncio.run(run_diagnostic(ctx, "cmd", "why"))

    fake_output = analyzer.AnalysisOutput(
        symptom="s",
        root_cause="r",
        confidence=0.5,
        hypothesis="h",
        recommended_action="a",
        reasoning="why",
    )
    monkeypatch.setattr(
        analyzer,
        "build",
        lambda model: SimpleNamespace(
            run=lambda prompt, deps: asyncio.sleep(
                0,
                result=SimpleNamespace(
                    output=fake_output,
                    usage=lambda: SimpleNamespace(input_tokens=1, output_tokens=2),
                ),
            )
        ),
    )
    result = asyncio.run(analyzer.run(None, deps, "h", "ctx"))
    assert result.hypothesis == "h"


def test_remediation_tools_and_run(monkeypatch):
    deps = _deps()
    agent = remediation.build("model")
    ctx = SimpleNamespace(deps=deps)
    assert (
        asyncio.run(agent._function_toolset.tools["run_benchmark"].function(ctx, 1))[
            "requests_per_sec"
        ]
        > 0
    )
    assert (
        asyncio.run(
            agent._function_toolset.tools["apply_config_change"].function(
                ctx, "sendfile", "on", "why"
            )
        )
        is True
    )
    assert asyncio.run(agent._function_toolset.tools["reload_service"].function(ctx, "why")) is True
    assert "output" in asyncio.run(
        agent._function_toolset.tools["run_command"].function(ctx, "cmd", "why")
    )

    fake_output = remediation.RemediationOutput(
        parameter="p",
        old_value="a",
        new_value="b",
        reasoning="why",
        success=True,
        before_rps=1,
        after_rps=2,
        impact_pct=100,
    )
    monkeypatch.setattr(
        remediation,
        "build",
        lambda model: SimpleNamespace(
            run=lambda prompt, deps: asyncio.sleep(
                0,
                result=SimpleNamespace(
                    output=fake_output,
                    usage=lambda: SimpleNamespace(input_tokens=1, output_tokens=1),
                ),
            )
        ),
    )
    result = asyncio.run(remediation.run(None, deps, "analysis", "action"))
    assert result.success is True


def test_orchestrator_run_and_context_prompt(monkeypatch, tmp_path):
    deps = _deps()
    monkeypatch.setattr(
        orchestrator.system_checks,
        "run_all",
        lambda ssh, checks: [CheckResult("cpu_governor", "ok", "ok", "")],
    )
    diagnosis = SimpleNamespace(summary="done", fixes_applied=[{"after_rps": 150}])
    monkeypatch.setattr(
        orchestrator.diagnosis_agent,
        "run",
        lambda model, deps, context_prompt: asyncio.sleep(0, result=diagnosis),
    )
    monkeypatch.setattr(
        orchestrator.reporter, "generate", lambda *a, **k: str(tmp_path / "report.md")
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_snapshot",
        lambda *a, **k: {"scope": k["scope"], "source": k["source"], "host": "localhost", "summary": {}, "sections": {}},
    )
    monkeypatch.setattr(orchestrator, "persist_snapshot", lambda *a, **k: None)
    for name in ["panel", "step", "check", "status", "benchmark", "log"]:
        monkeypatch.setattr(orchestrator.logger, name, lambda *a, **k: None)
    report = asyncio.run(orchestrator.run("model", deps))
    assert report.endswith("report.md")
    prompt = orchestrator._build_context_prompt(
        "RHEL",
        "6.0",
        8,
        16,
        ["- x"],
        "- baseline small: 1.0 RPS, p99=2.0ms",
        [{"parameter": "p", "value": "v", "impact": 1.0}],
        "- baseline:post: workers=112",
    )
    assert "Already applied (skip)" in prompt
    assert "Telemetry:" in prompt
    assert "Benchmark Evidence:" in prompt


def test_orchestrator_stops_after_phase_3(monkeypatch, tmp_path):
    deps = _deps()
    deps.config["agent"]["max_phase"] = 3
    monkeypatch.setattr(
        orchestrator.system_checks,
        "run_all",
        lambda ssh, checks: [CheckResult("cpu_governor", "ok", "ok", "")],
    )
    diagnosis = SimpleNamespace(summary="done", recommendations=[{"title": "Raise limits"}])
    monkeypatch.setattr(
        orchestrator.diagnosis_agent,
        "run",
        lambda model, deps, context_prompt: asyncio.sleep(0, result=diagnosis),
    )
    monkeypatch.setattr(
        orchestrator.reporter, "generate", lambda *a, **k: str(tmp_path / "report.md")
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_snapshot",
        lambda *a, **k: {"scope": k["scope"], "source": k["source"], "host": "localhost", "summary": {}, "sections": {}},
    )
    monkeypatch.setattr(orchestrator, "persist_snapshot", lambda *a, **k: None)
    for name in ["panel", "step", "check", "status", "benchmark", "log"]:
        monkeypatch.setattr(orchestrator.logger, name, lambda *a, **k: None)

    report = asyncio.run(orchestrator.run("model", deps))

    assert report.endswith("report.md")
