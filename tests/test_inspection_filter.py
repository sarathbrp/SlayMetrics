"""Tests for _filter_inspection_for_llm and _HIGH_IMPACT_PARAMS in agents.agent."""

from __future__ import annotations

from agents.agent import _HIGH_IMPACT_PARAMS, _filter_inspection_for_llm

# ---------------------------------------------------------------------------
# _HIGH_IMPACT_PARAMS sanity checks
# ---------------------------------------------------------------------------

class TestHighImpactParams:
    def test_core_webserver_params_included(self):
        assert "worker_processes" in _HIGH_IMPACT_PARAMS
        assert "worker_connections" in _HIGH_IMPACT_PARAMS
        assert "listen_backlog" in _HIGH_IMPACT_PARAMS
        assert "limit_rate" in _HIGH_IMPACT_PARAMS

    def test_core_kernel_params_included(self):
        assert "net.core.somaxconn" in _HIGH_IMPACT_PARAMS
        assert "vm.swappiness" in _HIGH_IMPACT_PARAMS
        assert "transparent_hugepage" in _HIGH_IMPACT_PARAMS

    def test_low_impact_params_not_included(self):
        assert "sendfile" not in _HIGH_IMPACT_PARAMS
        assert "tcp_nopush" not in _HIGH_IMPACT_PARAMS
        assert "gzip_comp_level" not in _HIGH_IMPACT_PARAMS
        assert "directio" not in _HIGH_IMPACT_PARAMS


# ---------------------------------------------------------------------------
# _filter_inspection_for_llm
# ---------------------------------------------------------------------------

class TestFilterInspectionForLlm:
    def test_current_dict_stripped(self):
        data = {
            "needs_fixing": {"worker_processes": {"current": "8", "target": "auto"}},
            "ok_count": 5,
            "current": {"worker_processes": "8", "sendfile": "on"},
        }
        result = _filter_inspection_for_llm(data)
        assert "current" not in result
        assert "needs_fixing" in result

    def test_high_impact_always_included(self):
        data = {
            "needs_fixing": {
                "worker_processes": {"current": "8", "target": "auto"},
                "worker_connections": {"current": "2048", "target": "65536"},
                "listen_backlog": {"current": "1024", "target": "65535"},
            },
            "ok_count": 0,
        }
        result = _filter_inspection_for_llm(data)
        assert "worker_processes" in result["needs_fixing"]
        assert "worker_connections" in result["needs_fixing"]
        assert "listen_backlog" in result["needs_fixing"]

    def test_low_impact_included_within_limit(self):
        data = {
            "needs_fixing": {
                "worker_processes": {"current": "8", "target": "auto"},
                "sendfile": {"current": "off", "target": "on"},
                "tcp_nopush": {"current": "off", "target": "on"},
            },
            "ok_count": 0,
        }
        result = _filter_inspection_for_llm(data, max_items=20)
        # All 3 should be included (1 high + 2 low, well under limit)
        assert len(result["needs_fixing"]) == 3

    def test_low_impact_trimmed_when_over_limit(self):
        # Create 5 high-impact + 10 low-impact
        high = {p: {"current": "bad", "target": "good"}
                for p in list(_HIGH_IMPACT_PARAMS)[:5]}
        low = {f"custom_param_{i}": {"current": "bad", "target": "good"}
               for i in range(10)}
        data = {"needs_fixing": {**high, **low}, "ok_count": 0}

        result = _filter_inspection_for_llm(data, max_items=8)
        # Should include all 5 high + 3 low = 8 total
        assert len(result["needs_fixing"]) == 8
        # All high-impact should be present
        for p in list(_HIGH_IMPACT_PARAMS)[:5]:
            assert p in result["needs_fixing"]

    def test_ok_count_adjusted_for_trimmed(self):
        high = {"worker_processes": {"current": "8", "target": "auto"}}
        low = {f"low_{i}": {"current": "x", "target": "y"} for i in range(5)}
        data = {"needs_fixing": {**high, **low}, "ok_count": 3}

        result = _filter_inspection_for_llm(data, max_items=3)
        # 1 high + 2 low included, 3 low trimmed
        assert len(result["needs_fixing"]) == 3
        # ok_count should increase by number of trimmed low-impact params
        assert result["ok_count"] == 3 + 3  # original 3 + 3 trimmed

    def test_empty_needs_fixing(self):
        data = {"needs_fixing": {}, "ok_count": 10}
        result = _filter_inspection_for_llm(data)
        assert result["needs_fixing"] == {}
        assert result["ok_count"] == 10

    def test_no_needs_fixing_key(self):
        data = {"ok_count": 5, "problems": [{"param": "tc_rules"}]}
        result = _filter_inspection_for_llm(data)
        assert "problems" in result
        assert result["ok_count"] == 5

    def test_max_items_zero(self):
        data = {
            "needs_fixing": {
                "worker_processes": {"current": "8", "target": "auto"},
                "sendfile": {"current": "off", "target": "on"},
            },
            "ok_count": 0,
        }
        result = _filter_inspection_for_llm(data, max_items=0)
        # High-impact params are always included regardless of max_items
        # (remaining = max(0, 0 - 1) = 0, so no low-impact)
        assert "worker_processes" in result["needs_fixing"]
        assert "sendfile" not in result["needs_fixing"]

    def test_preserves_other_keys(self):
        data = {
            "needs_fixing": {"worker_processes": {"current": "8", "target": "auto"}},
            "ok_count": 5,
            "current": {"a": "b"},
            "category": "webserver",
        }
        result = _filter_inspection_for_llm(data)
        assert result.get("category") == "webserver"
        assert "current" not in result

    def test_all_high_impact_no_trimming(self):
        high = {p: {"current": "bad", "target": "good"}
                for p in list(_HIGH_IMPACT_PARAMS)[:3]}
        data = {"needs_fixing": high, "ok_count": 0}
        result = _filter_inspection_for_llm(data, max_items=2)
        # Even though max_items=2, all 3 high-impact params are included
        assert len(result["needs_fixing"]) == 3

    def test_needs_fixing_not_dict_passthrough(self):
        """If needs_fixing is not a dict (unexpected), pass through as-is."""
        data = {"needs_fixing": "unexpected_string", "ok_count": 0}
        result = _filter_inspection_for_llm(data)
        assert result["needs_fixing"] == "unexpected_string"
