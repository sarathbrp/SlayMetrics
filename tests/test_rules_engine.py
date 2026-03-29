"""Tests for agents.rules_engine — deterministic apply plan builder."""

from __future__ import annotations

from agents.rules_engine import (
    CATEGORY_TARGET_KEYS,
    _is_blocked,
    apply_validation_result,
    build_apply_plan,
    build_rca_records,
    build_recommendations,
    build_summary,
    build_validation_prompt,
    compact_plan_text,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _config(
    *,
    webserver_targets: dict | None = None,
    kernel_targets: dict | None = None,
    resource_limits_targets: dict | None = None,
    network_targets: dict | None = None,
    storage_targets: dict | None = None,
    blocked_values: dict | None = None,
) -> dict:
    return {
        "tuning": {
            "webserver_targets": webserver_targets or {
                "worker_processes": "auto",
                "worker_connections": "65536",
                "tcp_nodelay": "on",
                "access_log": "off",
            },
            "kernel_targets": kernel_targets or {
                "net.core.somaxconn": "65535",
                "vm.swappiness": "10",
            },
            "resource_limits_targets": resource_limits_targets or {
                "cgroup_io_weight": "100",
            },
            "network_targets": network_targets or {
                "tc_rules": "remove",
            },
            "storage_targets": storage_targets or {
                "io_scheduler": "none",
            },
            "blocked_values": blocked_values or {},
        }
    }


def _inspection(
    *,
    webserver_fixing: dict | None = None,
    kernel_fixing: dict | None = None,
    resource_problems: list | None = None,
    network_problems: list | None = None,
    storage_problems: list | None = None,
) -> dict:
    return {
        "webserver": {
            "needs_fixing": webserver_fixing or {
                "worker_processes": {"current": "8", "target": "auto"},
                "worker_connections": {"current": "2048", "target": "65536"},
            },
            "ok_count": 2,
        },
        "kernel": {
            "needs_fixing": kernel_fixing or {
                "net.core.somaxconn": {"current": "1024", "target": "65535"},
                "vm.swappiness": {"current": "80", "target": "10"},
            },
            "ok_count": 0,
        },
        "resource_limits": {
            "needs_fixing": {},
            "problems": resource_problems or [],
        },
        "network": {
            "needs_fixing": {},
            "problems": network_problems or [],
        },
        "storage": {
            "needs_fixing": {},
            "problems": storage_problems or [],
        },
        "summary": {
            "total_issues": 4,
            "by_category": {"webserver": 2, "kernel": 2},
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# _is_blocked
# ═══════════════════════════════════════════════════════════════════════════

class TestIsBlocked:
    def test_not_blocked_when_no_guardrails(self):
        assert _is_blocked("aio", "on", {}) is False

    def test_blocked_value_matches(self):
        cfg = {"tuning": {"blocked_values": {"aio": ["threads", "on"]}}}
        assert _is_blocked("aio", "on", cfg) is True
        assert _is_blocked("aio", "threads", cfg) is True

    def test_blocked_case_insensitive(self):
        cfg = {"tuning": {"blocked_values": {"aio": ["ON"]}}}
        assert _is_blocked("aio", "on", cfg) is True

    def test_not_blocked_for_different_param(self):
        cfg = {"tuning": {"blocked_values": {"aio": ["on"]}}}
        assert _is_blocked("sendfile", "on", cfg) is False

    def test_not_blocked_when_value_allowed(self):
        cfg = {"tuning": {"blocked_values": {"aio": ["threads"]}}}
        assert _is_blocked("aio", "off", cfg) is False

    def test_none_config(self):
        assert _is_blocked("aio", "on", None) is False

    def test_whitespace_stripped(self):
        cfg = {"tuning": {"blocked_values": {"aio": ["on"]}}}
        assert _is_blocked("aio", "  on  ", cfg) is True


# ═══════════════════════════════════════════════════════════════════════════
# build_apply_plan
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildApplyPlan:
    def test_basic_plan_from_needs_fixing(self):
        plan = build_apply_plan(_inspection(), _config())
        assert plan["webserver"]["worker_processes"] == "auto"
        assert plan["webserver"]["worker_connections"] == "65536"
        assert plan["kernel"]["net.core.somaxconn"] == "65535"
        assert plan["kernel"]["vm.swappiness"] == "10"

    def test_fills_missing_targets(self):
        """Targets in config but not in needs_fixing should still appear."""
        inspection = _inspection(webserver_fixing={
            "worker_processes": {"current": "8", "target": "auto"},
        })
        plan = build_apply_plan(inspection, _config())
        # worker_connections is in config targets but not in needs_fixing
        assert plan["webserver"]["worker_connections"] == "65536"
        assert plan["webserver"]["tcp_nodelay"] == "on"

    def test_blocked_values_excluded(self):
        cfg = _config(
            webserver_targets={"aio": "threads", "sendfile": "on"},
            blocked_values={"aio": ["threads", "on"]},
        )
        inspection = _inspection(webserver_fixing={
            "aio": {"current": "off", "target": "threads"},
        })
        plan = build_apply_plan(inspection, cfg)
        assert "aio" not in plan["webserver"]
        assert plan["webserver"]["sendfile"] == "on"

    def test_problems_mapped_to_config_targets(self):
        inspection = _inspection(
            resource_problems=[{"param": "cgroup_io_weight", "detail": "weight=10"}],
        )
        plan = build_apply_plan(inspection, _config())
        assert plan["resource_limits"]["cgroup_io_weight"] == "100"

    def test_network_problems(self):
        inspection = _inspection(
            network_problems=[{"param": "tc_rules", "detail": "htb shaping found"}],
        )
        plan = build_apply_plan(inspection, _config())
        assert plan["network"]["tc_rules"] == "remove"

    def test_storage_problems(self):
        inspection = _inspection(
            storage_problems=[{"setting": "io_scheduler", "detail": "mq-deadline"}],
        )
        plan = build_apply_plan(inspection, _config())
        assert plan["storage"]["io_scheduler"] == "none"

    def test_all_categories_present(self):
        plan = build_apply_plan(_inspection(), _config())
        for cat in CATEGORY_TARGET_KEYS:
            assert cat in plan

    def test_empty_inspection(self):
        plan = build_apply_plan({}, _config())
        # Should still have all targets from config (fill-missing logic)
        assert plan["webserver"]["worker_processes"] == "auto"
        assert plan["kernel"]["net.core.somaxconn"] == "65535"

    def test_empty_config(self):
        plan = build_apply_plan(_inspection(), {"tuning": {}})
        # Even with no config targets, needs_fixing still provides target values
        assert plan["webserver"]["worker_processes"] == "auto"
        assert plan["webserver"]["worker_connections"] == "65536"
        # Categories with no needs_fixing AND no config targets are empty
        assert plan["resource_limits"] == {}
        assert plan["network"] == {}
        assert plan["storage"] == {}

    def test_needs_fixing_target_preferred_over_config(self):
        """If inspection says target=X and config says Y, inspection wins."""
        inspection = _inspection(webserver_fixing={
            "worker_processes": {"current": "8", "target": "112"},
        })
        cfg = _config(webserver_targets={"worker_processes": "auto"})
        plan = build_apply_plan(inspection, cfg)
        assert plan["webserver"]["worker_processes"] == "112"

    def test_needs_fixing_empty_target_uses_config(self):
        inspection = _inspection(webserver_fixing={
            "worker_processes": {"current": "8", "target": ""},
        })
        cfg = _config(webserver_targets={"worker_processes": "auto"})
        plan = build_apply_plan(inspection, cfg)
        assert plan["webserver"]["worker_processes"] == "auto"

    def test_problem_with_setting_key(self):
        """Problems can use 'setting' instead of 'param'."""
        inspection = _inspection(
            storage_problems=[{"setting": "io_scheduler", "detail": "wrong"}],
        )
        plan = build_apply_plan(inspection, _config())
        assert plan["storage"]["io_scheduler"] == "none"

    def test_problem_param_not_in_targets_ignored(self):
        inspection = _inspection(
            resource_problems=[{"param": "unknown_param", "detail": "bad"}],
        )
        plan = build_apply_plan(inspection, _config())
        assert "unknown_param" not in plan["resource_limits"]


# ═══════════════════════════════════════════════════════════════════════════
# build_rca_records
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildRcaRecords:
    def test_basic_rca_generation(self):
        records = build_rca_records(_inspection())
        assert len(records) == 4  # 2 webserver + 2 kernel

    def test_rca_fields_present(self):
        records = build_rca_records(_inspection())
        for rec in records:
            assert "symptom" in rec
            assert "root_cause" in rec
            assert "confidence" in rec
            assert "recommendation" in rec
            assert "evidence" in rec

    def test_confidence_is_deterministic(self):
        records = build_rca_records(_inspection())
        for rec in records:
            assert rec["confidence"] == 0.99

    def test_symptom_includes_param_and_current(self):
        inspection = _inspection(webserver_fixing={
            "worker_processes": {"current": "8", "target": "auto"},
        })
        records = build_rca_records(inspection)
        wp_rec = [r for r in records if "worker_processes" in r["symptom"]][0]
        assert "8" in wp_rec["symptom"]

    def test_evidence_includes_category(self):
        records = build_rca_records(_inspection())
        wp_rec = [r for r in records if "worker_processes" in r["symptom"]][0]
        assert any("webserver" in e for e in wp_rec["evidence"])

    def test_empty_inspection(self):
        records = build_rca_records({})
        assert records == []

    def test_summary_key_skipped(self):
        """The 'summary' key in inspection should not generate RCA records."""
        records = build_rca_records(_inspection())
        summary_recs = [r for r in records if "summary" in str(r.get("evidence", []))]
        assert len(summary_recs) == 0

    def test_non_dict_category_skipped(self):
        inspection = {"webserver": "not a dict", "summary": {}}
        records = build_rca_records(inspection)
        assert records == []


# ═══════════════════════════════════════════════════════════════════════════
# build_recommendations
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildRecommendations:
    def test_basic_recommendations(self):
        recs = build_recommendations(_inspection(), _config())
        assert len(recs) == 4  # 2 webserver + 2 kernel

    def test_recommendation_fields(self):
        recs = build_recommendations(_inspection(), _config())
        for rec in recs:
            assert "title" in rec
            assert "scope" in rec
            assert "changes" in rec
            assert "rationale" in rec
            assert "risk_level" in rec
            assert isinstance(rec["changes"], dict)

    def test_scope_mapping(self):
        recs = build_recommendations(_inspection(), _config())
        wp_recs = [r for r in recs if "worker_processes" in str(r["changes"])]
        assert wp_recs[0]["scope"] == "nginx"

        kern_recs = [r for r in recs if "somaxconn" in str(r["changes"])]
        assert kern_recs[0]["scope"] == "system"

    def test_blocked_excluded(self):
        cfg = _config(
            webserver_targets={"aio": "threads"},
            blocked_values={"aio": ["threads"]},
        )
        inspection = _inspection(webserver_fixing={
            "aio": {"current": "off", "target": "threads"},
        })
        recs = build_recommendations(inspection, cfg)
        aio_recs = [r for r in recs if "aio" in str(r["changes"])]
        assert len(aio_recs) == 0

    def test_empty_inspection(self):
        recs = build_recommendations({}, _config())
        assert recs == []


# ═══════════════════════════════════════════════════════════════════════════
# compact_plan_text
# ═══════════════════════════════════════════════════════════════════════════

class TestCompactPlanText:
    def test_basic_format(self):
        plan = {"webserver": {"worker_processes": "auto"}, "kernel": {}}
        text = compact_plan_text(plan)
        assert "[webserver]" in text
        assert "worker_processes = auto" in text
        assert "[kernel]" not in text  # empty categories excluded

    def test_empty_plan(self):
        text = compact_plan_text({})
        assert text == ""

    def test_multiple_params(self):
        plan = {"webserver": {"a": "1", "b": "2"}}
        text = compact_plan_text(plan)
        assert "a = 1" in text
        assert "b = 2" in text

    def test_all_empty_categories(self):
        plan = {"webserver": {}, "kernel": {}, "network": {}}
        assert compact_plan_text(plan) == ""


# ═══════════════════════════════════════════════════════════════════════════
# build_summary
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildSummary:
    def test_basic_summary(self):
        plan = {"webserver": {"a": "1", "b": "2"}, "kernel": {"c": "3"}}
        summary = build_summary(_inspection(), plan)
        assert "4 misconfigured" in summary  # 2 webserver + 2 kernel needs_fixing
        assert "3 fixes" in summary
        assert "webserver" in summary
        assert "kernel" in summary

    def test_empty_plan(self):
        summary = build_summary(_inspection(), {"webserver": {}, "kernel": {}})
        assert "0 fixes" in summary

    def test_empty_inspection(self):
        summary = build_summary({}, {"webserver": {"a": "1"}})
        assert "0 misconfigured" in summary
        assert "1 fixes" in summary


# ═══════════════════════════════════════════════════════════════════════════
# build_validation_prompt
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildValidationPrompt:
    def test_includes_hardware_info(self):
        plan = {"webserver": {"worker_processes": "auto"}}
        prompt = build_validation_prompt(plan, cpu_cores=112, ram_gb=502)
        assert "112" in prompt
        assert "502" in prompt

    def test_includes_plan(self):
        plan = {"webserver": {"worker_processes": "auto"}}
        prompt = build_validation_prompt(plan)
        assert "worker_processes = auto" in prompt

    def test_includes_baseline_when_provided(self):
        plan = {"webserver": {}}
        prompt = build_validation_prompt(plan, baseline_summary="small=22K RPS")
        assert "small=22K RPS" in prompt

    def test_return_format_instructions(self):
        prompt = build_validation_prompt({"webserver": {}})
        assert '"remove"' in prompt
        assert '"add"' in prompt
        assert '"reasoning"' in prompt

    def test_empty_baseline(self):
        prompt = build_validation_prompt({"webserver": {}}, baseline_summary="")
        assert "Baseline:" not in prompt or "Baseline: \n" in prompt


# ═══════════════════════════════════════════════════════════════════════════
# apply_validation_result
# ═══════════════════════════════════════════════════════════════════════════

class TestApplyValidationResult:
    def test_remove_params(self):
        plan = {"webserver": {"aio": "threads", "sendfile": "on"}}
        validation = {"remove": ["aio"]}
        result = apply_validation_result(plan, validation, _config())
        assert "aio" not in result["webserver"]
        assert result["webserver"]["sendfile"] == "on"

    def test_add_params_in_allowlist(self):
        plan = {"webserver": {"sendfile": "on"}, "kernel": {}}
        validation = {"add": {"net.core.somaxconn": "65535"}}
        result = apply_validation_result(plan, validation, _config())
        assert result["kernel"]["net.core.somaxconn"] == "65535"

    def test_add_blocked_param_rejected(self):
        cfg = _config(
            webserver_targets={"aio": "threads"},
            blocked_values={"aio": ["threads"]},
        )
        plan = {"webserver": {}}
        validation = {"add": {"aio": "threads"}}
        result = apply_validation_result(plan, validation, cfg)
        assert "aio" not in result.get("webserver", {})

    def test_add_unknown_param_ignored(self):
        plan = {"webserver": {}, "kernel": {}}
        validation = {"add": {"unknown_param": "value"}}
        result = apply_validation_result(plan, validation, _config())
        for cat_changes in result.values():
            assert "unknown_param" not in cat_changes

    def test_empty_validation(self):
        plan = {"webserver": {"a": "1"}}
        result = apply_validation_result(plan, {}, _config())
        assert result["webserver"]["a"] == "1"

    def test_none_remove_and_add(self):
        plan = {"webserver": {"a": "1"}}
        result = apply_validation_result(plan, {"remove": None, "add": None}, _config())
        assert result["webserver"]["a"] == "1"

    def test_does_not_mutate_original(self):
        plan = {"webserver": {"a": "1", "b": "2"}}
        original_web = dict(plan["webserver"])
        apply_validation_result(plan, {"remove": ["a"]}, _config())
        assert plan["webserver"] == original_web

    def test_remove_from_multiple_categories(self):
        plan = {
            "webserver": {"worker_processes": "auto"},
            "kernel": {"worker_processes": "auto"},  # unusual but tests cross-cat removal
        }
        validation = {"remove": ["worker_processes"]}
        result = apply_validation_result(plan, validation, _config())
        assert "worker_processes" not in result["webserver"]
        assert "worker_processes" not in result["kernel"]


# ═══════════════════════════════════════════════════════════════════════════
# Integration-style tests
# ═══════════════════════════════════════════════════════════════════════════

class TestIntegration:
    """End-to-end tests combining multiple functions."""

    def test_full_pipeline_deterministic(self):
        """Simulate the deterministic planner pipeline."""
        inspection = _inspection()
        config = _config()

        plan = build_apply_plan(inspection, config)
        rca = build_rca_records(inspection)
        recs = build_recommendations(inspection, config)
        summary = build_summary(inspection, plan)
        text = compact_plan_text(plan)

        assert len(plan["webserver"]) >= 2
        assert len(plan["kernel"]) >= 2
        assert len(rca) > 0
        assert len(recs) > 0
        assert "misconfigured" in summary
        assert "[webserver]" in text

    def test_full_pipeline_with_validation(self):
        """Simulate the hybrid planner pipeline."""
        inspection = _inspection()
        config = _config(
            webserver_targets={"aio": "threads", "worker_processes": "auto", "sendfile": "on"},
            blocked_values={"aio": ["threads"]},
        )

        plan = build_apply_plan(inspection, config)
        # aio should not appear (blocked)
        assert "aio" not in plan["webserver"]

        # Simulate LLM validator removing sendfile
        validation = {"remove": ["sendfile"], "add": {}, "reasoning": "sendfile not needed"}
        plan = apply_validation_result(plan, validation, config)
        assert "sendfile" not in plan["webserver"]
        assert plan["webserver"]["worker_processes"] == "auto"

    def test_real_world_config_shape(self):
        """Test with a config shaped like the actual config.yaml."""
        config = {
            "tuning": {
                "webserver_targets": {
                    "worker_processes": "auto",
                    "worker_connections": "65536",
                    "worker_rlimit_nofile": "200000",
                    "sendfile": "on",
                    "tcp_nodelay": "on",
                    "access_log": "off",
                    "listen_backlog": "65535",
                    "limit_rate": "0",
                },
                "kernel_targets": {
                    "net.core.somaxconn": "65535",
                    "vm.swappiness": "10",
                    "vm.vfs_cache_pressure": "50",
                    "transparent_hugepage": "never",
                    "irqbalance": "active",
                },
                "resource_limits_targets": {
                    "systemd_nofile": "524288",
                    "cgroup_io_weight": "100",
                    "kill_background_hogs": "true",
                },
                "network_targets": {
                    "iptables_drop_rules": "flush",
                    "tc_rules": "remove",
                },
                "storage_targets": {
                    "io_scheduler": "none",
                    "readahead": "256",
                },
                "blocked_values": {
                    "aio": ["threads", "on"],
                },
            }
        }
        inspection = {
            "webserver": {
                "needs_fixing": {
                    "worker_processes": {"current": "8", "target": "auto"},
                    "worker_connections": {"current": "2048", "target": "65536"},
                    "tcp_nodelay": {"current": "off", "target": "on"},
                    "access_log": {"current": "/var/log/nginx/access.log", "target": "off"},
                    "limit_rate": {"current": "10m", "target": "0"},
                },
                "ok_count": 3,
            },
            "kernel": {
                "needs_fixing": {
                    "net.core.somaxconn": {"current": "1024", "target": "65535"},
                    "vm.swappiness": {"current": "80", "target": "10"},
                    "vm.vfs_cache_pressure": {"current": "500", "target": "50"},
                    "transparent_hugepage": {"current": "madvise", "target": "never"},
                },
                "ok_count": 1,
            },
            "resource_limits": {
                "needs_fixing": {},
                "problems": [
                    {"param": "cgroup_io_weight", "detail": "weight=10"},
                ],
            },
            "network": {
                "needs_fixing": {},
                "problems": [
                    {"param": "tc_rules", "detail": "htb shaping"},
                ],
            },
            "storage": {
                "needs_fixing": {},
                "problems": [],
            },
            "summary": {"total_issues": 9, "by_category": {"webserver": 5, "kernel": 4}},
        }

        plan = build_apply_plan(inspection, config)

        # All needs_fixing should be in the plan
        assert plan["webserver"]["worker_processes"] == "auto"
        assert plan["webserver"]["worker_connections"] == "65536"
        assert plan["kernel"]["vm.swappiness"] == "10"
        assert plan["kernel"]["transparent_hugepage"] == "never"

        # Problem-based fixes
        assert plan["resource_limits"]["cgroup_io_weight"] == "100"
        assert plan["network"]["tc_rules"] == "remove"

        # Fill-missing targets
        assert plan["webserver"]["sendfile"] == "on"
        assert plan["webserver"]["listen_backlog"] == "65535"
        assert plan["kernel"]["irqbalance"] == "active"
        assert plan["storage"]["io_scheduler"] == "none"
        assert plan["storage"]["readahead"] == "256"

        # RCA records
        rca = build_rca_records(inspection)
        assert len(rca) == 9  # 5 webserver + 4 kernel

        # Recommendations
        recs = build_recommendations(inspection, config)
        assert len(recs) == 9

        # Summary
        summary = build_summary(inspection, plan)
        assert "9 misconfigured" in summary

        # Compact text should be much shorter than JSON
        text = compact_plan_text(plan)
        json_text = str(plan)
        assert len(text) < len(json_text)
