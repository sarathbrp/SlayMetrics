from __future__ import annotations

# ruff: noqa: E402
import json
import sys
import types

sys.modules.setdefault(
    "paramiko",
    types.SimpleNamespace(SSHClient=object, AutoAddPolicy=lambda: object()),
)

from agents.tools_inspect import (
    _count_cpuset_cpus,
    _cpu_quota_to_cores,
    inspect_network,
    inspect_system_metadata,
)
from core.eval_harness import (
    build_case_bundle_from_session,
    evaluate_case_bundle,
    evaluate_nginx,
    evaluate_synthesizer,
)
from tools.ssh import SSHResult


class FakeSSH:
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def execute(self, command: str, timeout=None):
        del timeout
        return SSHResult(self.mapping.get(command, ""), "", 0)


def _bundle() -> dict:
    return {
        "session_id": "s1",
        "iteration": 1,
        "system": {
            "os_cpu_count": 112,
            "ram_gb": 502,
            "cgroup_cpu_quota_cores": 4,
            "cpuset_cpu_count": None,
        },
        "inspection": {
            "webserver": {
                "current": {
                    "worker_processes": "2",
                    "worker_connections": "65536",
                    "worker_rlimit_nofile": "200000",
                }
            },
            "kernel": {
                "current": {
                    "net.core.somaxconn": "1024",
                    "net.ipv4.tcp_max_syn_backlog": "1024",
                    "net.core.netdev_max_backlog": "1024",
                }
            },
            "network": {"findings": {}},
        },
        "nginx_expert": {
            "rca_records": [
                {"setting": "worker_processes", "target": "auto"},
                {"setting": "worker_connections", "target": "65536"},
                {"setting": "worker_rlimit_nofile", "target": "200000"},
            ],
            "recommendations": [],
        },
        "rhel_expert": {
            "summary": "Investigate iptables and net.core.somaxconn",
            "recommendations": [],
            "rca_records": [],
        },
        "synthesizer": {"summary": "Combined view"},
    }


def test_nginx_fd_capacity_fails_for_proxy_budget():
    findings = evaluate_nginx(_bundle())
    fd = next(f for f in findings if f["rule_id"] == "nginx.fd_capacity")
    assert fd["severity"] == "fail"
    assert "Recommended Target" in fd["correction"]


def test_nginx_worker_budget_uses_cgroup_limit():
    bundle = _bundle()
    bundle["nginx_expert"]["rca_records"][0]["target"] = "112"
    findings = evaluate_nginx(bundle)
    worker = next(f for f in findings if f["rule_id"] == "nginx.hardware_saturation")
    assert worker["severity"] == "fail"
    assert worker["correction"] == "Set worker_processes to 4."


def test_eval_bundle_aggregate_thresholds():
    result = evaluate_case_bundle(
        _bundle(),
        synth_judge=lambda bundle: {
            "hallucination": {"pass": True, "message": "", "evidence_refs": []},
            "critical_omission": {"pass": False, "message": "missed critical", "evidence_refs": []},
            "merge_fidelity": {"pass": True, "message": "", "evidence_refs": []},
            "format_validity": {"pass": True, "message": "", "evidence_refs": []},
        },
    )
    assert result["action"] == "self_correct"
    assert result["nginx_score"] < 1.0
    assert result["synthesizer_score"] < 1.0


def test_build_case_bundle_from_session_uses_latest_iteration():
    class FakeMemory:
        def get_profile(self, session_id):
            assert session_id == "s1"
            return {"cpu_cores": 112, "ram_gb": 502, "host": "dut", "service": "nginx"}

        def get_contexts(self, session_id, type=None, source_prefix=None, limit=None):
            del session_id, type, source_prefix, limit
            return [
                {"source": "compound_inspection", "content": json.dumps({"system": {"os_cpu_count": 112}})},
                {"source": "iter1_synthesizer", "content": json.dumps({"summary": "old"})},
                {"source": "iter2_nginx_expert", "content": json.dumps({"summary": "nginx"})},
                {"source": "iter2_rhel_expert", "content": json.dumps({"summary": "rhel"})},
                {"source": "iter2_synthesizer", "content": json.dumps({"summary": "new"})},
            ]

    bundle = build_case_bundle_from_session(FakeMemory(), "s1")
    assert bundle["iteration"] == 2
    assert bundle["synthesizer"]["summary"] == "new"


def test_cpu_quota_and_cpuset_helpers():
    assert _cpu_quota_to_cores("350000 100000") == 4
    assert _cpu_quota_to_cores("max 100000") is None
    assert _count_cpuset_cpus("0-3,8,10-11") == 7


def test_inspect_system_metadata_collects_eval_fields():
    ssh = FakeSSH(
        {
            "nproc 2>/dev/null || echo 0": "112",
            "cat /sys/fs/cgroup/system.slice/nginx.service/cpu.max 2>/dev/null || echo 'max 100000'": "350000 100000",
            "cat /sys/fs/cgroup/system.slice/nginx.service/cpuset.cpus.effective 2>/dev/null || cat /sys/fs/cgroup/system.slice/nginx.service/cpuset.cpus 2>/dev/null || echo ''": "0-3",
        }
    )
    metadata = inspect_system_metadata(ssh)
    assert metadata["os_cpu_count"] == 112
    assert metadata["cgroup_cpu_quota_cores"] == 4
    assert metadata["cpuset_cpu_count"] == 4


def test_inspect_network_includes_firewall_provenance():
    ssh = FakeSSH(
        {
            "timeout 5 iptables -L -n 2>/dev/null | grep -iE 'DROP|REJECT|connlimit|limit' | head -10": "DROP tcp -- 0.0.0.0/0 0.0.0.0/0",
            "sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || sysctl -n net.nf_conntrack_max 2>/dev/null || echo unknown": "4096",
            "tc qdisc show 2>/dev/null | grep -v 'noqueue\\|pfifo_fast\\|fq_codel' | head -5": "",
            "timeout 5 nft list ruleset 2>/dev/null | grep -iE 'drop|reject|limit' | head -5": "",
            "systemctl is-active firewalld 2>/dev/null || echo inactive": "active",
        }
    )
    findings = inspect_network(ssh, {"conntrack_max": "1048576"})
    assert findings["findings"]["firewalld_state"] == "active"
    assert findings["findings"]["firewall_provenance"]["iptables"] is True


def test_synth_critical_omission_ignores_optional_absent_control():
    bundle = _bundle()
    bundle["inspection"]["webserver"]["current"]["limit_conn"] = "not set"
    bundle["nginx_expert"] = {
        "rca_records": [{"setting": "limit_conn", "target": "remove"}],
        "recommendations": [],
    }
    bundle["synthesizer"] = {"summary": "Combined view", "recommendations": []}

    findings = evaluate_synthesizer(
        bundle,
        synth_judge=lambda payload: {
            "hallucination": {"pass": True, "message": "", "evidence_refs": []},
            "critical_omission": {
                "pass": False,
                "message": 'The synthesized artifact does not address the "limit_conn not set" issue.',
                "evidence_refs": [],
            },
            "merge_fidelity": {"pass": True, "message": "", "evidence_refs": []},
            "format_validity": {"pass": True, "message": "", "evidence_refs": []},
        },
    )

    assert not any(f["rule_id"] == "synthesizer.critical_omission" for f in findings)


def test_synth_deterministic_checks_flag_drift_keys_and_risk():
    bundle = _bundle()
    bundle["nginx_expert"] = {
        "rca_records": [{"setting": "worker_connections", "target": "65536"}],
        "recommendations": [],
    }
    bundle["synthesizer"] = {
        "summary": "Combined view",
        "recommendations": [
            {
                "title": "Drifted worker connections",
                "scope": "nginx",
                "changes": {"worker_connections": "8192"},
                "risk_level": "high",
            },
            {
                "title": "Bad key",
                "scope": "system",
                "changes": {"SELinux": "permissive"},
                "risk_level": "medium",
            },
        ],
    }

    findings = evaluate_synthesizer(
        bundle,
        synth_judge=lambda payload: {
            "hallucination": {"pass": True, "message": "", "evidence_refs": []},
            "critical_omission": {"pass": True, "message": "", "evidence_refs": []},
            "merge_fidelity": {"pass": True, "message": "", "evidence_refs": []},
            "format_validity": {"pass": True, "message": "", "evidence_refs": []},
        },
    )

    rule_ids = {finding["rule_id"] for finding in findings}
    assert "synthesizer.target_drift" in rule_ids
    assert "synthesizer.change_key_normalization" in rule_ids
    assert "synthesizer.risk_calibration" in rule_ids


def test_synth_promotes_limit_rate_after_omission_and_ignores_limit_req():
    bundle = _bundle()
    bundle["nginx_expert"] = {
        "rca_records": [
            {"setting": "limit_rate_after", "target": "0"},
            {"setting": "limit_req", "target": "remove"},
        ],
        "recommendations": [],
    }
    bundle["synthesizer"] = {"summary": "Combined view", "recommendations": []}

    findings = evaluate_synthesizer(
        bundle,
        synth_judge=lambda payload: {
            "hallucination": {"pass": True, "message": "", "evidence_refs": []},
            "critical_omission": {"pass": False, "message": "missed critical", "evidence_refs": []},
            "merge_fidelity": {"pass": True, "message": "", "evidence_refs": []},
            "format_validity": {"pass": True, "message": "", "evidence_refs": []},
        },
    )

    omission = next(f for f in findings if f["rule_id"] == "synthesizer.critical_omission")
    assert "limit_rate_after" in omission["message"]
    assert "limit_req" not in omission["message"]


def test_synth_alias_keys_are_flagged_as_normalization_issues():
    bundle = _bundle()
    bundle["synthesizer"] = {
        "summary": "Combined view",
        "recommendations": [
            {
                "title": "Bad aliases",
                "scope": "system",
                "changes": {
                    "DefaultLimitNOFILE": "1048576",
                    "CPUWeight": "100",
                    "SELINUX": "permissive",
                },
                "risk_level": "low",
            }
        ],
    }

    findings = evaluate_synthesizer(
        bundle,
        synth_judge=lambda payload: {
            "hallucination": {"pass": True, "message": "", "evidence_refs": []},
            "critical_omission": {"pass": True, "message": "", "evidence_refs": []},
            "merge_fidelity": {"pass": True, "message": "", "evidence_refs": []},
            "format_validity": {"pass": True, "message": "", "evidence_refs": []},
        },
    )

    normalization = next(f for f in findings if f["rule_id"] == "synthesizer.change_key_normalization")
    assert "DefaultLimitNOFILE" in normalization["message"]
    assert "CPUWeight" in normalization["message"]


def test_synth_sensitive_drift_gets_stronger_penalty():
    bundle = _bundle()
    bundle["nginx_expert"] = {
        "rca_records": [
            {"setting": "tcp_nodelay", "target": "on"},
            {"setting": "multi_accept", "target": "on"},
        ],
        "recommendations": [],
    }
    bundle["synthesizer"] = {
        "summary": "Combined view",
        "recommendations": [
            {
                "title": "Drifted transport",
                "scope": "nginx",
                "changes": {"tcp_nodelay": "off", "multi_accept": "off"},
                "risk_level": "low",
            }
        ],
    }

    findings = evaluate_synthesizer(
        bundle,
        synth_judge=lambda payload: {
            "hallucination": {"pass": True, "message": "", "evidence_refs": []},
            "critical_omission": {"pass": True, "message": "", "evidence_refs": []},
            "merge_fidelity": {"pass": True, "message": "", "evidence_refs": []},
            "format_validity": {"pass": True, "message": "", "evidence_refs": []},
        },
    )

    drift = next(f for f in findings if f["rule_id"] == "synthesizer.target_drift")
    assert drift["score_delta"] == -0.2
