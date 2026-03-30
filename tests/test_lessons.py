"""Tests for core.lessons — leaderboard queries and merge logic."""

from __future__ import annotations

from core.lessons import (
    LEADERBOARD_SIZE,
    QUALIFY_LARGE_RPS,
    QUALIFY_MEDIUM_RPS,
    check_leaderboard,
    merge_targets,
    qualifies,
)

# ---------------------------------------------------------------------------
# qualifies()
# ---------------------------------------------------------------------------


class TestQualifies:
    def test_both_above_threshold(self):
        results = {
            "small": {"rps": 500000},
            "medium": {"rps": 1400},
            "large": {"rps": 186},
        }
        assert qualifies(results) is True

    def test_medium_below_threshold(self):
        results = {
            "small": {"rps": 500000},
            "medium": {"rps": 1200},
            "large": {"rps": 186},
        }
        assert qualifies(results) is False

    def test_large_below_threshold(self):
        results = {
            "small": {"rps": 500000},
            "medium": {"rps": 1400},
            "large": {"rps": 150},
        }
        assert qualifies(results) is False

    def test_both_below_threshold(self):
        results = {
            "small": {"rps": 500000},
            "medium": {"rps": 100},
            "large": {"rps": 50},
        }
        assert qualifies(results) is False

    def test_exact_threshold(self):
        results = {
            "small": {"rps": 1},
            "medium": {"rps": QUALIFY_MEDIUM_RPS},
            "large": {"rps": QUALIFY_LARGE_RPS},
        }
        assert qualifies(results) is True

    def test_empty_results(self):
        assert qualifies({}) is False

    def test_missing_workloads(self):
        results = {"small": {"rps": 500000}}
        assert qualifies(results) is False

    def test_zero_rps(self):
        results = {
            "small": {"rps": 0},
            "medium": {"rps": 0},
            "large": {"rps": 0},
        }
        assert qualifies(results) is False

    def test_none_rps(self):
        results = {
            "small": {"rps": None},
            "medium": {"rps": None},
            "large": {"rps": None},
        }
        assert qualifies(results) is False


# ---------------------------------------------------------------------------
# merge_targets()
# ---------------------------------------------------------------------------


class TestMergeTargets:
    def test_proven_overrides_config(self):
        config_targets = {
            "webserver": {"worker_processes": "4", "sendfile": "off"},
            "kernel": {"vm.swappiness": "60"},
        }
        proven = {
            "webserver.worker_processes": "auto",
            "kernel.vm.swappiness": "10",
        }
        merged = merge_targets(config_targets, proven)
        assert merged["webserver"]["worker_processes"] == "auto"
        assert merged["kernel"]["vm.swappiness"] == "10"

    def test_config_preserved_when_no_override(self):
        config_targets = {
            "webserver": {"sendfile": "off", "tcp_nodelay": "on"},
        }
        proven = {"webserver.sendfile": "on"}
        merged = merge_targets(config_targets, proven)
        assert merged["webserver"]["sendfile"] == "on"
        assert merged["webserver"]["tcp_nodelay"] == "on"

    def test_empty_proven(self):
        config_targets = {"webserver": {"worker_processes": "4"}}
        merged = merge_targets(config_targets, {})
        assert merged["webserver"]["worker_processes"] == "4"

    def test_proven_unknown_category_ignored(self):
        config_targets = {"webserver": {"sendfile": "on"}}
        proven = {"unknown_cat.foo": "bar"}
        merged = merge_targets(config_targets, proven)
        assert "unknown_cat" not in merged

    def test_proven_unknown_param_in_known_category(self):
        config_targets = {"webserver": {"sendfile": "on"}}
        proven = {"webserver.new_param": "value"}
        merged = merge_targets(config_targets, proven)
        assert merged["webserver"]["new_param"] == "value"

    def test_does_not_mutate_original(self):
        config_targets = {"webserver": {"sendfile": "off"}}
        proven = {"webserver.sendfile": "on"}
        merge_targets(config_targets, proven)
        assert config_targets["webserver"]["sendfile"] == "off"

    def test_multiple_categories(self):
        config_targets = {
            "webserver": {"worker_processes": "4"},
            "kernel": {"vm.swappiness": "60"},
            "resource_limits": {"systemd_nofile": "1024"},
        }
        proven = {
            "webserver.worker_processes": "auto",
            "resource_limits.systemd_nofile": "524288",
        }
        merged = merge_targets(config_targets, proven)
        assert merged["webserver"]["worker_processes"] == "auto"
        assert merged["kernel"]["vm.swappiness"] == "60"
        assert merged["resource_limits"]["systemd_nofile"] == "524288"


# ---------------------------------------------------------------------------
# check_leaderboard() with mock memory
# ---------------------------------------------------------------------------


class _MockMemory:
    """Minimal mock that returns pre-set rows from _cursor().fetchall()."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def _cursor(self):
        return _MockCursorCtx(self._rows)


class _MockCursorCtx:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return _MockCursor(self._rows)

    def __exit__(self, *args):
        pass


class _MockCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args):
        pass

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class TestCheckLeaderboard:
    def _top3(self):
        return [
            {"session_id": "aaa", "small_rps": 1000000, "medium_rps": 1400, "large_rps": 186, "tokens": 18000, "iterations": 1},
            {"session_id": "bbb", "small_rps": 800000, "medium_rps": 1400, "large_rps": 186, "tokens": 19000, "iterations": 1},
            {"session_id": "ccc", "small_rps": 500000, "medium_rps": 1400, "large_rps": 186, "tokens": 20000, "iterations": 1},
        ]

    def test_beats_best(self):
        memory = _MockMemory(self._top3())
        results = {"small": {"rps": 1200000}, "medium": {"rps": 1500}, "large": {"rps": 190}}
        lb = check_leaderboard(memory, results)
        assert lb["qualifies"] is True
        assert lb["rank"] == 1
        assert lb["beats_best"] is True

    def test_beats_second(self):
        memory = _MockMemory(self._top3())
        results = {"small": {"rps": 900000}, "medium": {"rps": 1400}, "large": {"rps": 186}}
        lb = check_leaderboard(memory, results)
        assert lb["qualifies"] is True
        assert lb["rank"] == 2
        assert lb["beats_best"] is False

    def test_beats_third(self):
        memory = _MockMemory(self._top3())
        results = {"small": {"rps": 600000}, "medium": {"rps": 1400}, "large": {"rps": 186}}
        lb = check_leaderboard(memory, results)
        assert lb["qualifies"] is True
        assert lb["rank"] == 3
        assert lb["beats_best"] is False

    def test_does_not_qualify_below_third(self):
        memory = _MockMemory(self._top3())
        results = {"small": {"rps": 400000}, "medium": {"rps": 1400}, "large": {"rps": 186}}
        lb = check_leaderboard(memory, results)
        assert lb["qualifies"] is False
        assert lb["rank"] is None

    def test_does_not_qualify_medium_low(self):
        memory = _MockMemory(self._top3())
        results = {"small": {"rps": 2000000}, "medium": {"rps": 500}, "large": {"rps": 186}}
        lb = check_leaderboard(memory, results)
        assert lb["qualifies"] is False

    def test_empty_leaderboard_qualifies(self):
        memory = _MockMemory([])
        results = {"small": {"rps": 100000}, "medium": {"rps": 1400}, "large": {"rps": 186}}
        lb = check_leaderboard(memory, results)
        assert lb["qualifies"] is True
        assert lb["rank"] == 1
        assert lb["beats_best"] is True

    def test_partial_leaderboard(self):
        rows = [
            {"session_id": "aaa", "small_rps": 1000000, "medium_rps": 1400, "large_rps": 186, "tokens": 18000, "iterations": 1},
        ]
        memory = _MockMemory(rows)
        results = {"small": {"rps": 500000}, "medium": {"rps": 1400}, "large": {"rps": 186}}
        lb = check_leaderboard(memory, results)
        assert lb["qualifies"] is True
        assert lb["rank"] == 2

    def test_leaderboard_size_constant(self):
        assert LEADERBOARD_SIZE == 3
