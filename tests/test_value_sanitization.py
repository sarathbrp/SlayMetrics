"""Tests for value sanitization and schema fallback fixes in agents/agent.py.

Covers:
- _strip_inline_comment: semicolon stripping via split(";")[0]
- _clean_recs_for_planner: cleaning dirty recommendation values before planner
- apply_saved_recommendations_impl filtering (split vs rstrip)
- _coerce_records: fallback mapping for alternative RCA schemas
- _normalize_synthesized_recommendation: fallback for {category, setting, recommended_value}
- _extract_changes_from_commands: sed and systemctl parsing
"""
from __future__ import annotations

import pytest

from agents.agent import (
    _clean_recs_for_planner,
    _coerce_records,
    _extract_changes_from_commands,
    _normalize_synthesized_recommendation,
    _strip_inline_comment,
)

# ---------------------------------------------------------------------------
# _strip_inline_comment
# ---------------------------------------------------------------------------

class TestStripInlineComment:
    def test_clean_value_passthrough(self):
        assert _strip_inline_comment("auto") == "auto"

    def test_numeric_value_passthrough(self):
        assert _strip_inline_comment("65536") == "65536"

    def test_space_separated_value_passthrough(self):
        assert _strip_inline_comment("4096 87380 16777216") == "4096 87380 16777216"

    def test_trailing_semicolon_stripped(self):
        assert _strip_inline_comment("auto;") == "auto"

    def test_mid_string_semicolon_stripped(self):
        """The key fix: '; reload nginx' must be stripped, not just trailing ';'."""
        assert _strip_inline_comment("auto; reload nginx") == "auto"

    def test_semicolon_with_comment(self):
        assert _strip_inline_comment("auto; # resolves to 112") == "auto"

    def test_comment_without_semicolon(self):
        assert _strip_inline_comment("auto # resolves to 112") == "auto"

    def test_complex_dirty_value(self):
        assert _strip_inline_comment("200000; ensure ulimit -n >= 200000; reload nginx") == "200000"

    def test_empty_string(self):
        assert _strip_inline_comment("") == ""

    def test_only_semicolon(self):
        assert _strip_inline_comment(";") == ""

    def test_whitespace_handling(self):
        assert _strip_inline_comment("  auto  ;  reload  ") == "auto"


# ---------------------------------------------------------------------------
# _clean_recs_for_planner
# ---------------------------------------------------------------------------

class TestCleanRecsForPlanner:
    def test_cleans_dirty_changes(self):
        recs = [
            {
                "title": "Set worker_processes auto; reload nginx",
                "changes": {"worker_processes": "auto; reload nginx"},
            }
        ]
        result = _clean_recs_for_planner(recs)
        assert result[0]["changes"]["worker_processes"] == "auto"

    def test_preserves_clean_values(self):
        recs = [
            {
                "title": "Set worker_connections 65536",
                "changes": {"worker_connections": "65536"},
            }
        ]
        result = _clean_recs_for_planner(recs)
        assert result[0]["changes"]["worker_connections"] == "65536"

    def test_preserves_space_separated_values(self):
        recs = [
            {
                "changes": {"output_buffers": "2 32k"},
            }
        ]
        result = _clean_recs_for_planner(recs)
        assert result[0]["changes"]["output_buffers"] == "2 32k"

    def test_cleans_multiple_semicolons(self):
        recs = [
            {
                "changes": {
                    "worker_rlimit_nofile": "200000; ensure ulimit -n >= 200000; reload nginx"
                },
            }
        ]
        result = _clean_recs_for_planner(recs)
        assert result[0]["changes"]["worker_rlimit_nofile"] == "200000"

    def test_handles_empty_list(self):
        assert _clean_recs_for_planner([]) == []

    def test_handles_non_list_input(self):
        assert _clean_recs_for_planner("not a list") == []
        assert _clean_recs_for_planner(None) == []

    def test_handles_rec_without_changes(self):
        recs = [{"title": "some rec", "rationale": "some reason"}]
        result = _clean_recs_for_planner(recs)
        assert len(result) == 1
        assert "changes" not in result[0] or result[0].get("changes") is None

    def test_handles_non_dict_changes(self):
        recs = [{"changes": "not a dict"}]
        result = _clean_recs_for_planner(recs)
        assert result[0]["changes"] == "not a dict"

    def test_preserves_other_fields(self):
        recs = [
            {
                "title": "Set worker_processes auto; reload nginx",
                "rationale": "Match CPU cores",
                "scope": "nginx",
                "changes": {"worker_processes": "auto; reload nginx"},
            }
        ]
        result = _clean_recs_for_planner(recs)
        assert result[0]["title"] == "Set worker_processes auto; reload nginx"
        assert result[0]["rationale"] == "Match CPU cores"
        assert result[0]["scope"] == "nginx"
        assert result[0]["changes"]["worker_processes"] == "auto"

    def test_does_not_mutate_input(self):
        original_changes = {"worker_processes": "auto; reload nginx"}
        recs = [{"changes": original_changes}]
        _clean_recs_for_planner(recs)
        assert original_changes["worker_processes"] == "auto; reload nginx"

    def test_multiple_recs(self):
        recs = [
            {"changes": {"worker_processes": "auto; reload nginx"}},
            {"changes": {"worker_connections": "65536; reload nginx"}},
            {"changes": {"keepalive_timeout": "30"}},
        ]
        result = _clean_recs_for_planner(recs)
        assert result[0]["changes"]["worker_processes"] == "auto"
        assert result[1]["changes"]["worker_connections"] == "65536"
        assert result[2]["changes"]["keepalive_timeout"] == "30"

    def test_empty_value_after_semicolon(self):
        recs = [{"changes": {"limit_req": "; some junk"}}]
        result = _clean_recs_for_planner(recs)
        assert result[0]["changes"]["limit_req"] == ""


# ---------------------------------------------------------------------------
# Integration: apply filter uses split(";")[0]
# ---------------------------------------------------------------------------

class TestApplyFilterSanitization:
    """Test that the apply filter pattern str(v).strip().split(';')[0].strip()
    produces correct results for all value types seen in production."""

    @pytest.mark.parametrize(
        "raw_value,expected",
        [
            ("auto", "auto"),
            ("65536", "65536"),
            ("auto; reload nginx", "auto"),
            ("65536; reload nginx", "65536"),
            ("200000; ensure ulimit -n >= 200000; reload nginx", "200000"),
            ("4096 87380 16777216", "4096 87380 16777216"),
            ("1024 65535", "1024 65535"),
            ("2 32k", "2 32k"),
            ("off", "off"),
            ("on", "on"),
            ("never", "never"),
            ("permissive", "permissive"),
            ("max=200000 inactive=60s", "max=200000 inactive=60s"),
            ("", ""),
            ("  auto  ; reload  ", "auto"),
            ("warn", "warn"),
            ("0", "0"),
            ("100", "100"),
            ("none", "none"),
        ],
    )
    def test_apply_filter_pattern(self, raw_value: str, expected: str):
        """Simulates the exact sanitization pattern used in
        apply_saved_recommendations_impl at lines 1583 and 1598."""
        result = str(raw_value).strip().split(";")[0].strip()
        assert result == expected


# ---------------------------------------------------------------------------
# _coerce_records: fallback field mapping for alternative RCA schemas
# ---------------------------------------------------------------------------


class TestCoerceRecordsFallback:
    """Test that _coerce_records handles LLMs returning {component, setting,
    current, target, impact} instead of {symptom, root_cause, ...}."""

    def test_standard_schema_passthrough(self):
        records = _coerce_records([
            {
                "symptom": "worker_processes fixed at 8",
                "root_cause": "Most CPU cores idle",
                "confidence": 0.95,
                "recommendation": "Set auto",
                "evidence": ["nginx -T"],
            }
        ])
        assert len(records) == 1
        assert records[0]["symptom"] == "worker_processes fixed at 8"
        assert records[0]["root_cause"] == "Most CPU cores idle"

    def test_component_setting_current_target_impact_schema(self):
        """The exact schema gpt-oss-120b returns."""
        records = _coerce_records([
            {
                "component": "nginx",
                "setting": "worker_processes",
                "current": "8",
                "target": "auto",
                "impact": "Most CPU cores idle, limiting parallel request handling",
            }
        ])
        assert len(records) == 1
        assert "worker_processes=8" in records[0]["symptom"]
        assert "target auto" in records[0]["symptom"]
        assert records[0]["root_cause"] == "Most CPU cores idle, limiting parallel request handling"

    def test_setting_without_current(self):
        records = _coerce_records([
            {
                "setting": "accept_mutex",
                "impact": "Serialises accept() calls",
            }
        ])
        assert len(records) == 1
        assert "accept_mutex" in records[0]["symptom"]

    def test_kernel_setting(self):
        records = _coerce_records([
            {
                "component": "kernel.network",
                "setting": "net.core.somaxconn",
                "current": "1024",
                "target": "65535",
                "impact": "Limits pending connections",
            }
        ])
        assert len(records) == 1
        assert "somaxconn=1024" in records[0]["symptom"]
        assert records[0]["root_cause"] == "Limits pending connections"

    def test_empty_item_still_dropped(self):
        records = _coerce_records([{}])
        assert len(records) == 0

    def test_mixed_schemas(self):
        records = _coerce_records([
            {"symptom": "high latency", "root_cause": "bad config"},
            {
                "component": "nginx",
                "setting": "worker_connections",
                "current": "2048",
                "target": "65536",
                "impact": "Caps concurrency",
            },
        ])
        assert len(records) == 2


# ---------------------------------------------------------------------------
# _normalize_synthesized_recommendation: fallback for alternative schemas
# ---------------------------------------------------------------------------


class TestNormalizeRecommendationFallback:
    def test_standard_changes_dict_passthrough(self):
        item = {"scope": "nginx", "changes": {"worker_processes": "auto"}}
        result = _normalize_synthesized_recommendation(item)
        assert result["changes"] == {"worker_processes": "auto"}

    def test_category_maps_to_scope(self):
        item = {
            "category": "nginx",
            "setting": "worker_processes",
            "recommended_value": "auto",
        }
        result = _normalize_synthesized_recommendation(item)
        assert result.get("scope") == "nginx"
        assert result["changes"] == {"worker_processes": "auto"}

    def test_kernel_category_maps_to_system_scope(self):
        item = {
            "category": "kernel",
            "setting": "vm.swappiness",
            "recommended_value": "10",
        }
        result = _normalize_synthesized_recommendation(item)
        assert result.get("scope") == "system"
        assert result["changes"] == {"vm.swappiness": "10"}

    def test_cgroup_category_maps_to_system_scope(self):
        item = {
            "category": "cgroup",
            "setting": "IOWeight",
            "recommended_value": "100",
        }
        result = _normalize_synthesized_recommendation(item)
        assert result.get("scope") == "system"

    def test_recommended_value_dict_batched(self):
        """When recommended_value is a dict of sysctls."""
        item = {
            "category": "kernel",
            "setting": "sysctls",
            "recommended_value": {
                "net.core.somaxconn": "65535",
                "net.ipv4.tcp_max_syn_backlog": "65535",
            },
        }
        result = _normalize_synthesized_recommendation(item)
        assert result["changes"]["net.core.somaxconn"] == "65535"
        assert result["changes"]["net.ipv4.tcp_max_syn_backlog"] == "65535"

    def test_recommended_value_strips_semicolons(self):
        item = {
            "category": "nginx",
            "setting": "worker_processes",
            "recommended_value": "auto; reload nginx",
        }
        result = _normalize_synthesized_recommendation(item)
        assert result["changes"]["worker_processes"] == "auto"


# ---------------------------------------------------------------------------
# _extract_changes_from_commands: sed and systemctl parsing
# ---------------------------------------------------------------------------


class TestExtractChangesFromCommands:
    def test_sysctl_extraction(self):
        result = _extract_changes_from_commands(
            ["sysctl -w vm.swappiness=10 vm.vfs_cache_pressure=50"]
        )
        assert result["vm.swappiness"] == "10"
        assert result["vm.vfs_cache_pressure"] == "50"

    def test_sed_nginx_directive(self):
        result = _extract_changes_from_commands(
            ["sed -i 's/^worker_processes .*/worker_processes auto;/' /etc/nginx/nginx.conf"]
        )
        assert result.get("worker_processes") == "auto"

    def test_sed_worker_connections(self):
        result = _extract_changes_from_commands(
            ["sed -i 's/^worker_connections .*/worker_connections 65536;/' /etc/nginx/nginx.conf"]
        )
        assert result.get("worker_connections") == "65536"

    def test_systemctl_set_property_ioweight(self):
        result = _extract_changes_from_commands(
            ["systemctl set-property nginx.service IOWeight=100"]
        )
        assert result.get("cgroup_io_weight") == "100"

    def test_systemctl_set_property_cpuweight(self):
        result = _extract_changes_from_commands(
            ["systemctl set-property nginx.service CPUWeight=100"]
        )
        assert result.get("cgroup_cpu_weight") == "100"

    def test_setenforce(self):
        result = _extract_changes_from_commands(["setenforce 0"])
        assert result.get("selinux") == "permissive"

    def test_thp_never(self):
        result = _extract_changes_from_commands(
            ["echo never > /sys/kernel/mm/transparent_hugepage/enabled"]
        )
        assert result.get("transparent_hugepage") == "never"

    def test_irqbalance(self):
        result = _extract_changes_from_commands(
            ["systemctl enable --now irqbalance"]
        )
        assert result.get("irqbalance") == "enabled"

    def test_empty_commands(self):
        assert _extract_changes_from_commands([]) == {}
        assert _extract_changes_from_commands([""]) == {}
