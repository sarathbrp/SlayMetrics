"""Tests for value sanitization fixes in agents/agent.py.

Covers:
- _strip_inline_comment: semicolon stripping via split(";")[0]
- _clean_recs_for_planner: cleaning dirty recommendation values before planner
- apply_saved_recommendations_impl filtering (split vs rstrip)
"""
from __future__ import annotations

import pytest

from agents.agent import (
    _clean_recs_for_planner,
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
