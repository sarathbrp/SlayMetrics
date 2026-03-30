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
                "source": "baseline:series",
                "content": '{"summary":{"sample_count":5,"duration_sec":4,"run_queue_avg":1.2,"run_queue_max":3,"rx_drop_delta":2,"rx_drop_rate_per_sec":0.5,"worker_core_spread_max":4},"first_sample":{"nginx_worker_count":112,"nginx_worker_cores":[0,1],"somaxconn":"4096","tcp_max_syn_backlog":"1024","ip_local_port_range":"32768 60999","rx_drop_total":0,"tx_drop_total":0,"tcp_established":900,"mem_used_mb":1000,"vmstat_run_queue":1,"vmstat_blocked":0},"last_sample":{"nginx_worker_count":112,"nginx_worker_cores":[0,1,2,3],"somaxconn":"4096","tcp_max_syn_backlog":"1024","ip_local_port_range":"32768 60999","rx_drop_total":2,"tx_drop_total":0,"tcp_established":1024,"mem_used_mb":1001,"vmstat_run_queue":3,"vmstat_blocked":0}}',
            },
            {
                "source": "baseline:post",
                "content": '{"summary":{"nginx_worker_count":112,"nginx_worker_cores":[0,1],"somaxconn":"4096","tcp_max_syn_backlog":"1024","ip_local_port_range":"32768 60999","rx_drop_total":0,"tx_drop_total":0,"tcp_established":1024}}',
            }
        ]

    def save_fact(self, **kwargs):
        self.saved.append(("fact", kwargs))

    def save_optimization_validation(self, **kwargs):
        self.saved.append(("optimization_validation", kwargs))

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

    def complete_session(self, *args, **kwargs):
        self.saved.append(("complete_session", args, kwargs))

    def get_token_history(self):
        return []

    def get_profile(self, session_id):
        return {"service": "nginx", "host": "localhost"}

    def get_latest_session_for_host(self, host, exclude_session_id=None):
        del host, exclude_session_id
        return None

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
                "baseline_mode": "fresh",
                "stability": {
                    "enabled": True,
                    "duration_sec": 2,
                    "sample_interval_sec": 1,
                    "url_key": "small_file_url",
                }
            },
        },
    )


def test_orchestrator_reuses_stored_baseline(monkeypatch):
    deps = _deps()
    deps.memory.get_latest_session_for_host = lambda host, exclude_session_id=None: "prior-s1"

    def fake_get_contexts(session_id, type=None, source_prefix=None, limit=None):
        del type, limit
        if session_id == "prior-s1" and source_prefix == "baseline_small":
            return [{"content": '{"rps": 123.0, "p99": 4.0, "cpu_pct": 0, "mem_mb": 0, "error_rate": 0}'}]
        if session_id == "prior-s1" and source_prefix == "baseline_homepage":
            return [{"content": '{"rps": 456.0, "p99": 1.0, "cpu_pct": 0, "mem_mb": 0, "error_rate": 0}'}]
        if session_id == "prior-s1" and source_prefix is None:
            return [
                {
                    "source": "baseline:series",
                    "content": '{"summary":{"sample_count":3,"duration_sec":2,"run_queue_avg":0.5,"run_queue_max":1,"rx_drop_delta":1,"rx_drop_rate_per_sec":0.1,"worker_core_spread_max":8},"first_sample":{"nginx_worker_count":113,"somaxconn":"4096","tcp_max_syn_backlog":"1024","rx_drop_total":10,"tx_drop_total":0,"tcp_established":90},"last_sample":{"nginx_worker_count":113,"somaxconn":"4096","tcp_max_syn_backlog":"1024","rx_drop_total":11,"tx_drop_total":0,"tcp_established":95}}',
                }
            ]
        return []

    deps.memory.get_contexts = fake_get_contexts
    deps.config["agent"]["baseline_mode"] = "reuse"

    monkeypatch.setattr(orchestrator.system_checks, "run_all", lambda ssh, checks: [])
    monkeypatch.setattr(
        orchestrator.diagnosis_agent,
        "run",
        lambda model, deps, context_prompt: asyncio.sleep(
            0,
            result=SimpleNamespace(
                notes="planned",
                recommendations=[],
                rca_records=[],
                nginx_applied=False,
                system_applied=False,
                after_rps=0.0,
            ),
        ),
    )
    monkeypatch.setattr(
        orchestrator.diagnosis_agent,
        "run_preflight",
        lambda model, deps: asyncio.sleep(0, result={"status": "ok", "problems": [], "fixes": [], "diagnostics": {}}),
    )
    monkeypatch.setattr(orchestrator.reporter, "generate", lambda *a, **k: "report.md")

    report = asyncio.run(orchestrator.run("model", deps))

    assert report == "report.md"
    assert any(
        args[2] == "benchmark_evidence" and "123.0" in args[3]
        for args in deps.memory.saved
        if isinstance(args, tuple) and len(args) >= 4
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
        orchestrator.diagnosis_agent,
        "run_preflight",
        lambda model, deps: asyncio.sleep(0, result={"status": "ok", "problems": [], "fixes": [], "diagnostics": {}}),
    )
    monkeypatch.setattr(
        orchestrator.reporter, "generate", lambda *a, **k: str(tmp_path / "report.md")
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_snapshot",
        lambda *a, **k: {
            "scope": k["scope"],
            "source": k["source"],
            "host": "localhost",
            "summary": {},
            "sections": {},
        },
    )
    monkeypatch.setattr(orchestrator, "start_sampler", lambda *a, **k: {"scope": k["scope"], "ok": True})
    monkeypatch.setattr(
        orchestrator,
        "stop_sampler",
        lambda *a, **k: {
            "scope": k["scope"],
            "ok": True,
            "csv_content": "timestamp,nginx_worker_count,nginx_worker_cores,somaxconn,tcp_max_syn_backlog,ip_local_port_range,rx_drop_total,tx_drop_total,tcp_established,mem_used_mb,vmstat_run_queue,vmstat_blocked\n1,112,\"0,1\",4096,1024,\"32768 60999\",0,0,900,1000,1,0\n2,112,\"0,1,2,3\",4096,1024,\"32768 60999\",2,0,1024,1001,3,0\n",
            "summary": {
                "sample_count": 2,
                "duration_sec": 1,
                "run_queue_avg": 2.0,
                "run_queue_max": 3,
                "rx_drop_delta": 2,
                "rx_drop_rate_per_sec": 2.0,
                "first_sample": {
                    "nginx_worker_count": 112,
                    "nginx_worker_cores": [0, 1],
                    "somaxconn": "4096",
                    "tcp_max_syn_backlog": "1024",
                    "ip_local_port_range": "32768 60999",
                    "rx_drop_total": 0,
                    "tx_drop_total": 0,
                    "tcp_established": 900,
                    "mem_used_mb": 1000,
                    "vmstat_run_queue": 1,
                    "vmstat_blocked": 0,
                },
                "last_sample": {
                    "nginx_worker_count": 112,
                    "nginx_worker_cores": [0, 1, 2, 3],
                    "somaxconn": "4096",
                    "tcp_max_syn_backlog": "1024",
                    "ip_local_port_range": "32768 60999",
                    "rx_drop_total": 2,
                    "tx_drop_total": 0,
                    "tcp_established": 1024,
                    "mem_used_mb": 1001,
                    "vmstat_run_queue": 3,
                    "vmstat_blocked": 0,
                },
            },
        },
    )
    monkeypatch.setattr(orchestrator, "persist_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "persist_sampler_result", lambda *a, **k: None)
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
    )
    assert "Already applied (skip)" in prompt
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
        orchestrator.diagnosis_agent,
        "run_preflight",
        lambda model, deps: asyncio.sleep(0, result={"status": "ok", "problems": [], "fixes": [], "diagnostics": {}}),
    )
    monkeypatch.setattr(
        orchestrator.reporter, "generate", lambda *a, **k: str(tmp_path / "report.md")
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_snapshot",
        lambda *a, **k: {
            "scope": k["scope"],
            "source": k["source"],
            "host": "localhost",
            "summary": {},
            "sections": {},
        },
    )
    monkeypatch.setattr(orchestrator, "start_sampler", lambda *a, **k: {"scope": k["scope"], "ok": True})
    monkeypatch.setattr(
        orchestrator,
        "stop_sampler",
        lambda *a, **k: {
            "scope": k["scope"],
            "ok": True,
            "csv_content": "",
            "summary": {"sample_count": 0, "first_sample": {}, "last_sample": {}},
        },
    )
    monkeypatch.setattr(orchestrator, "persist_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "persist_sampler_result", lambda *a, **k: None)
    for name in ["panel", "step", "check", "status", "benchmark", "log"]:
        monkeypatch.setattr(orchestrator.logger, name, lambda *a, **k: None)

    report = asyncio.run(orchestrator.run("model", deps))

    assert report.endswith("report.md")
    assert any(item[0] == "complete_session" for item in deps.memory.saved)


def test_orchestrator_optimization_mode_reverts_failed_group(monkeypatch, tmp_path):
    deps = _deps()
    deps.config["agent"]["max_iterations"] = 2
    deps.config["agent"]["optimization"] = {
        "enabled": True,
        "top_runs": 3,
        "min_small_gain_pct": 1.0,
        "leaderboard_gap_pct": 3.0,
    }

    monkeypatch.setattr(orchestrator.system_checks, "run_all", lambda ssh, checks: [])
    monkeypatch.setattr(
        orchestrator,
        "get_top_runs",
        lambda memory: [
            {
                "session_id": "best",
                "small_rps": 1000.0,
                "medium_rps": 1400.0,
                "large_rps": 186.0,
                "tokens": 10,
                "iterations": 1,
            }
        ],
    )
    monkeypatch.setattr(orchestrator, "get_best_run_params", lambda memory: {})
    monkeypatch.setattr(orchestrator, "get_prior_knowledge_text", lambda memory: "")
    monkeypatch.setattr(
        orchestrator.diagnosis_agent,
        "run",
        lambda model, deps, context_prompt: asyncio.sleep(
            0,
            result=SimpleNamespace(
                notes="planned",
                recommendations=[],
                rca_records=[],
                nginx_applied=True,
                system_applied=False,
            ),
        ),
    )
    monkeypatch.setattr(
        orchestrator.diagnosis_agent,
        "run_preflight",
        lambda model, deps: asyncio.sleep(
            0, result={"status": "ok", "problems": [], "fixes": [], "diagnostics": {}}
        ),
    )
    monkeypatch.setattr(
        orchestrator.reporter, "generate", lambda *a, **k: str(tmp_path / "report.md")
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_hackathon_benchmark",
        lambda deps, cfg, label, session_id: {
            "baseline": {
                "small": {"rps": 100.0, "p99": 1.0},
                "medium": {"rps": 1300.0, "p99": 1.0},
                "large": {"rps": 180.0, "p99": 1.0},
            },
            "iter1": {
                "small": {"rps": 500.0, "p99": 1.0},
                "medium": {"rps": 1400.0, "p99": 1.0},
                "large": {"rps": 186.0, "p99": 1.0},
            },
            "iter2": {
                "small": {"rps": 500.4, "p99": 1.0},
                "medium": {"rps": 1400.0, "p99": 1.0},
                "large": {"rps": 186.0, "p99": 1.0},
            },
        }[label],
    )
    monkeypatch.setattr(
        orchestrator,
        "_collect_current_state",
        lambda deps: {
            "webserver.worker_connections": "4096",
            "kernel.net.core.somaxconn": "4096",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "get_ranked_optimization_groups",
        lambda memory, current_state, top_n=3: [
            {
                "name": "accept_path",
                "score": 10.0,
                "risk": "low",
                "reasons": ["core group"],
                "changes": {
                    "webserver": {"worker_connections": "65536"},
                    "kernel": {"net.core.somaxconn": "65535"},
                },
                "current": {
                    "webserver.worker_connections": "4096",
                    "kernel.net.core.somaxconn": "4096",
                },
            }
        ],
    )
    monkeypatch.setattr(orchestrator, "_snapshot_optimization_state", lambda deps, candidate: {})
    monkeypatch.setattr(
        orchestrator,
        "_apply_optimization_group",
        lambda deps, candidate: {"webserver": {"applied": ["worker_connections"]}},
    )
    reverted = {"called": False}
    monkeypatch.setattr(
        orchestrator,
        "_revert_optimization_group",
        lambda deps, snapshot: reverted.__setitem__("called", True),
    )
    monkeypatch.setattr(
        orchestrator,
        "collect_snapshot",
        lambda *a, **k: {"scope": k["scope"], "source": k["source"], "summary": {}, "sections": {}},
    )
    monkeypatch.setattr(orchestrator, "start_sampler", lambda *a, **k: {"scope": k["scope"]})
    monkeypatch.setattr(
        orchestrator,
        "stop_sampler",
        lambda *a, **k: {
            "scope": k["scope"],
            "ok": True,
            "csv_content": "",
            "summary": {"sample_count": 0, "first_sample": {}, "last_sample": {}},
        },
    )
    monkeypatch.setattr(orchestrator, "persist_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "persist_sampler_result", lambda *a, **k: None)
    for name in ["panel", "step", "check", "status", "benchmark", "log"]:
        monkeypatch.setattr(orchestrator.logger, name, lambda *a, **k: None)

    report = asyncio.run(orchestrator.run("model", deps))

    assert report.endswith("report.md")
    assert reverted["called"] is True
    complete = [item for item in deps.memory.saved if item[0] == "complete_session"][-1]
    assert complete[2]["rps_end"] == 500.0
    optimization_validations = [
        item for item in deps.memory.saved if item[0] == "optimization_validation"
    ]
    assert optimization_validations
    assert optimization_validations[0][1]["outcome"] == "contradicted"
