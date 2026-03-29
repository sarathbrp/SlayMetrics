"""Simulate real LLM outputs from production logs through the fixed pipeline.

Feeds the exact data from debug logs into _coerce_records,
_coerce_recommendations, and _clean_recs_for_planner to verify
that records/recommendations survive instead of being dropped.
"""
from __future__ import annotations

from agents.agent import (
    _clean_recs_for_planner,
    _coerce_recommendations,
    _coerce_records,
    _normalize_synthesized_recommendation,
)

# ---------------------------------------------------------------------------
# Simulated LLM outputs — exact data from production debug logs
# ---------------------------------------------------------------------------

# Run 1: LLM used {component, setting, current, target, impact} for RCA
RCA_RUN1_RAW = [
    {
        "component": "nginx",
        "setting": "worker_processes",
        "current": "8",
        "target": "auto (\u2248112)",
        "impact": "Insufficient workers to utilize all CPU cores, "
        "leading to under-utilisation and request backlog.",
    },
    {
        "component": "nginx",
        "setting": "worker_connections",
        "current": "2048",
        "target": "65536",
        "impact": "Caps total concurrent connections per worker, limiting overall concurrency.",
    },
    {
        "component": "nginx",
        "setting": "worker_rlimit_nofile",
        "current": "4096",
        "target": "200000",
        "impact": 'Restricts open file descriptors, causing "Too many open files" under load.',
    },
    {
        "component": "kernel.network",
        "setting": "net.core.somaxconn",
        "current": "1024",
        "target": "65535",
        "impact": "Limits pending connections, causing refusals under load",
    },
    {
        "component": "kernel.network",
        "setting": "net.ipv4.tcp_max_syn_backlog",
        "current": "1024",
        "target": "65535",
        "impact": "SYN queue overflow during bursts",
    },
    {
        "component": "kernel.vm",
        "setting": "vm.swappiness",
        "current": "80",
        "target": "10",
        "impact": "Aggressive swapping despite abundant RAM, increasing I/O latency",
    },
    {
        "component": "kernel.vm",
        "setting": "transparent_hugepage",
        "current": "madvise",
        "target": "never",
        "impact": "Potential latency spikes from THP allocation/defragmentation",
    },
    {
        "component": "kernel.selinux",
        "setting": "SELinux mode",
        "current": "enforcing",
        "target": "permissive (for testing)",
        "impact": "Policy checks add overhead to high-frequency syscalls",
    },
    {
        "component": "systemd.resource_limits",
        "setting": "cgroup IOWeight",
        "current": "10",
        "target": "100",
        "impact": "I/O scheduler gives minimal weight, throttling disk throughput",
    },
    {
        "component": "systemd.resource_limits",
        "setting": "cgroup CPUWeight",
        "current": "50",
        "target": "100",
        "impact": "CPU scheduler allocates less CPU time relative to other cgroups",
    },
]

# Run 2: LLM used correct schema for some RCA records
RCA_RUN2_CORRECT = [
    {
        "symptom": "Fixed at 8 on a 112-core system",
        "root_cause": "Leaves most CPU cores idle, limiting parallel request handling",
        "confidence": 0.0,
        "recommendation": "Leaves most CPU cores idle, limiting parallel request handling",
        "evidence": [],
    },
    {
        "symptom": "2048 per worker (\u224816 K total)",
        "root_cause": "Restricts concurrent connections far below hardware capability",
        "confidence": 0.0,
        "recommendation": "Restricts concurrent connections far below hardware capability",
        "evidence": [],
    },
]

# Recommendations: LLM used {category, description, command} — no changes dict
RECS_COMMAND_STYLE = [
    {
        "category": "nginx",
        "description": "Set worker_processes to auto so one worker runs per CPU core.",
        "command": "sed -i 's/^worker_processes .*/worker_processes auto;/' /etc/nginx/nginx.conf",
    },
    {
        "category": "nginx",
        "description": "Increase worker_connections to at least 65536.",
        "command": "sed -i 's/^worker_connections .*/worker_connections 65536;/' "
        "/etc/nginx/nginx.conf",
    },
    {
        "category": "nginx",
        "description": "Enable worker_cpu_affinity auto for cache-friendly core binding.",
        "command": "sed -i 's/^#*worker_cpu_affinity.*/worker_cpu_affinity auto;/' "
        "/etc/nginx/nginx.conf",
    },
    {
        "category": "system",
        "description": "Apply sysctl tunings for networking and memory.",
        "command": "sysctl -w net.core.somaxconn=65535 net.ipv4.tcp_max_syn_backlog=65535",
    },
    {
        "category": "cgroup",
        "description": "Raise I/O and CPU weights for the NGINX service slice.",
        "command": "systemctl set-property nginx.service IOWeight=100 && "
        "systemctl set-property nginx.service CPUWeight=100",
    },
    {
        "category": "system",
        "description": "Disable THP.",
        "command": "echo never > /sys/kernel/mm/transparent_hugepage/enabled",
    },
    {
        "category": "system",
        "description": "Set SELinux to permissive.",
        "command": "setenforce 0",
    },
    {
        "category": "system",
        "description": "Enable irqbalance.",
        "command": "systemctl enable --now irqbalance",
    },
]

# Recommendations: LLM used {component, setting, recommended_value, action} — no changes dict
RECS_RECOMMENDED_VALUE_STYLE = [
    {
        "component": "nginx",
        "setting": "access_log",
        "recommended_value": "off",
        "justification": "Eliminates per-request disk I/O; enable only for debugging",
        "action": "Comment out or set access_log off; reload nginx",
    },
    {
        "component": "kernel.network",
        "setting": "sysctls",
        "recommended_value": {
            "net.core.somaxconn": "65535",
            "net.ipv4.tcp_max_syn_backlog": "65535",
            "net.core.netdev_max_backlog": "65535",
        },
        "justification": "Tune network stack for high concurrency",
    },
    {
        "component": "kernel.vm",
        "setting": "vm.swappiness",
        "recommended_value": "10",
        "justification": "Reduces unnecessary swapping on a system with abundant RAM",
        "action": "sysctl -w vm.swappiness=10",
    },
    {
        "component": "kernel.vm",
        "setting": "transparent_hugepage",
        "recommended_value": "never",
        "justification": "Avoids latency spikes from THP allocation/defrag",
        "action": "echo never > /sys/kernel/mm/transparent_hugepage/enabled",
    },
    {
        "component": "kernel.selinux",
        "setting": "SELinux mode",
        "recommended_value": "permissive (testing only)",
        "justification": "Removes policy-check overhead for benchmarking",
        "action": "setenforce 0",
    },
    {
        "component": "systemd.resource_limits",
        "setting": "cgroup IOWeight",
        "recommended_value": "100",
        "justification": "Gives the NGINX service full I/O scheduler weight",
        "action": "systemctl set-property nginx.service IOWeight=100",
    },
]

# Recommendations: LLM used correct schema but dirty values
RECS_DIRTY_VALUES = [
    {
        "title": "Set worker_processes auto; reload nginx",
        "recommendation": "Set worker_processes auto; reload nginx",
        "rationale": "Matches the 112 CPU cores",
        "scope": "nginx",
        "changes": {"worker_processes": "auto; reload nginx"},
    },
    {
        "title": "Set worker_connections 65536; reload nginx",
        "recommendation": "Set worker_connections 65536; reload nginx",
        "rationale": "Allows ~7 M concurrent connections",
        "scope": "nginx",
        "changes": {"worker_connections": "65536; reload nginx"},
    },
    {
        "title": "Set worker_rlimit_nofile 200000; ensure ulimit",
        "recommendation": "Set worker_rlimit_nofile 200000; ensure ulimit -n >= 200000",
        "rationale": "Sufficient file descriptors",
        "scope": "nginx",
        "changes": {"worker_rlimit_nofile": "200000; ensure ulimit -n >= 200000; reload nginx"},
    },
]


# ===================================================================
# Tests
# ===================================================================


class TestSimulateRCACoercion:
    """Simulate _coerce_records with real LLM outputs."""

    def test_run1_alternative_schema_all_survive(self):
        """Run 1: ALL 10 records used {component, setting, current, target, impact}.
        Previously ALL were dropped. Now they should ALL survive."""
        records = _coerce_records(RCA_RUN1_RAW)
        assert len(records) == 10, f"Expected 10, got {len(records)}"
        for r in records:
            assert r["symptom"] != "unknown symptom", f"Bad symptom: {r}"
            assert r["root_cause"] != "unknown root cause", f"Bad root_cause: {r}"

    def test_run1_nginx_worker_processes(self):
        records = _coerce_records(RCA_RUN1_RAW)
        wp = records[0]
        assert "worker_processes" in wp["symptom"]
        assert "8" in wp["symptom"]
        assert "Insufficient workers" in wp["root_cause"]

    def test_run1_kernel_somaxconn(self):
        records = _coerce_records(RCA_RUN1_RAW)
        sc = records[3]
        assert "somaxconn" in sc["symptom"]
        assert "1024" in sc["symptom"]
        assert "pending connections" in sc["root_cause"]

    def test_run1_cgroup_ioweight(self):
        records = _coerce_records(RCA_RUN1_RAW)
        io = records[8]
        assert "IOWeight" in io["symptom"]
        assert "10" in io["symptom"]

    def test_run2_correct_schema_still_works(self):
        """Run 2: correct schema records should still pass through."""
        records = _coerce_records(RCA_RUN2_CORRECT)
        assert len(records) == 2
        assert records[0]["symptom"] == "Fixed at 8 on a 112-core system"

    def test_mixed_schemas(self):
        """Mix of correct and alternative schemas."""
        mixed = RCA_RUN2_CORRECT + RCA_RUN1_RAW[:3]
        records = _coerce_records(mixed)
        assert len(records) == 5


class TestSimulateRecommendationNormalization:
    """Simulate _normalize_synthesized_recommendation with real LLM outputs."""

    def test_command_style_sed_extraction(self):
        """LLM used {category, description, command} with sed commands."""
        item = RECS_COMMAND_STYLE[0]  # sed worker_processes
        result = _normalize_synthesized_recommendation(item)
        changes = result.get("changes", {})
        assert "worker_processes" in changes, f"Missing worker_processes: {result}"
        assert changes["worker_processes"] == "auto"

    def test_command_style_sysctl_extraction(self):
        item = RECS_COMMAND_STYLE[3]  # sysctl -w
        result = _normalize_synthesized_recommendation(item)
        changes = result.get("changes", {})
        assert "net.core.somaxconn" in changes
        assert changes["net.core.somaxconn"] == "65535"

    def test_command_style_systemctl_extraction(self):
        item = RECS_COMMAND_STYLE[4]  # systemctl set-property
        result = _normalize_synthesized_recommendation(item)
        changes = result.get("changes", {})
        assert "cgroup_io_weight" in changes or "IOWeight" in changes

    def test_command_style_thp(self):
        item = RECS_COMMAND_STYLE[5]
        result = _normalize_synthesized_recommendation(item)
        changes = result.get("changes", {})
        assert changes.get("transparent_hugepage") == "never"

    def test_command_style_selinux(self):
        item = RECS_COMMAND_STYLE[6]
        result = _normalize_synthesized_recommendation(item)
        changes = result.get("changes", {})
        assert changes.get("selinux") == "permissive"

    def test_command_style_irqbalance(self):
        item = RECS_COMMAND_STYLE[7]
        result = _normalize_synthesized_recommendation(item)
        changes = result.get("changes", {})
        assert changes.get("irqbalance") == "enabled"

    def test_recommended_value_scalar(self):
        """LLM used {setting, recommended_value} with scalar value."""
        item = RECS_RECOMMENDED_VALUE_STYLE[0]  # access_log=off
        result = _normalize_synthesized_recommendation(item)
        changes = result.get("changes", {})
        assert "access_log" in changes, f"Missing access_log: {result}"
        assert changes["access_log"] == "off"

    def test_recommended_value_dict_batched_sysctls(self):
        """LLM used {setting: 'sysctls', recommended_value: {dict of params}}."""
        item = RECS_RECOMMENDED_VALUE_STYLE[1]
        result = _normalize_synthesized_recommendation(item)
        changes = result.get("changes", {})
        assert "net.core.somaxconn" in changes
        assert "net.ipv4.tcp_max_syn_backlog" in changes
        assert changes["net.core.somaxconn"] == "65535"

    def test_recommended_value_vm_swappiness(self):
        item = RECS_RECOMMENDED_VALUE_STYLE[2]
        result = _normalize_synthesized_recommendation(item)
        changes = result.get("changes", {})
        assert changes.get("vm.swappiness") == "10"

    def test_category_to_scope_mapping(self):
        """Verify category -> scope mapping for all types."""
        for item in RECS_COMMAND_STYLE:
            result = _normalize_synthesized_recommendation(item)
            scope = result.get("scope", "")
            cat = item["category"]
            if cat == "nginx":
                assert scope == "nginx", f"category={cat} got scope={scope}"
            else:
                assert scope == "system", f"category={cat} got scope={scope}"


class TestSimulateCoerceRecommendations:
    """End-to-end: _coerce_recommendations with real LLM outputs."""

    def test_command_style_recs_survive(self):
        """Previously ALL command-style recs were dropped. Now some should survive."""
        recs = _coerce_recommendations(RECS_COMMAND_STYLE)
        # At minimum, sysctl, THP, selinux, irqbalance should survive
        assert len(recs) >= 4, (
            f"Expected >=4 surviving recs, got {len(recs)}: "
            f"{[r.get('title') for r in recs]}"
        )

    def test_recommended_value_recs_survive(self):
        """Previously ALL recommended_value-style recs were dropped."""
        recs = _coerce_recommendations(RECS_RECOMMENDED_VALUE_STYLE)
        assert len(recs) >= 3, (
            f"Expected >=3 surviving recs, got {len(recs)}: "
            f"{[r.get('title') for r in recs]}"
        )

    def test_dirty_value_recs_survive_then_cleaned_by_planner(self):
        """Dirty values like 'auto; reload nginx' pass through _coerce_recommendations
        (which preserves existing changes dicts), then get cleaned by
        _clean_recs_for_planner before reaching the apply_planner."""
        recs = _coerce_recommendations(RECS_DIRTY_VALUES)
        assert len(recs) == 3
        # Values are still dirty at this stage — that's OK
        assert ";" in recs[0]["changes"]["worker_processes"]
        # Cleaning happens in _clean_recs_for_planner
        cleaned = _clean_recs_for_planner(recs)
        for r in cleaned:
            for val in r["changes"].values():
                assert ";" not in val, f"Dirty value survived cleaning: {val}"
                assert "reload" not in val, f"Command fragment survived: {val}"


class TestSimulateCleanRecsForPlanner:
    """Simulate _clean_recs_for_planner with dirty recommendation outputs."""

    def test_dirty_values_cleaned_for_planner(self):
        cleaned = _clean_recs_for_planner(RECS_DIRTY_VALUES)
        assert cleaned[0]["changes"]["worker_processes"] == "auto"
        assert cleaned[1]["changes"]["worker_connections"] == "65536"
        assert cleaned[2]["changes"]["worker_rlimit_nofile"] == "200000"

    def test_clean_values_unchanged(self):
        clean_recs = [
            {"changes": {"worker_processes": "auto", "worker_connections": "65536"}}
        ]
        cleaned = _clean_recs_for_planner(clean_recs)
        assert cleaned[0]["changes"]["worker_processes"] == "auto"
        assert cleaned[0]["changes"]["worker_connections"] == "65536"


# Recommendations with aliased key names (from latest run)
RECS_ALIASED_KEYS = [
    {
        "title": "Set SELinux to permissive",
        "scope": "system",
        "changes": {"selinux_mode": "permissive"},
        "rationale": "Reduces syscall overhead",
        "risk_level": "medium",
    },
    {
        "title": "Raise cgroup I/O and CPU weights",
        "scope": "system",
        "changes": {"cgroup_IOWeight": "100", "cgroup_CPUWeight": "100"},
        "rationale": "Allows services to consume appropriate resources",
        "risk_level": "low",
    },
    {
        "title": "Replace HTB shaping with fq_codel",
        "scope": "system",
        "changes": {"tc_qdisc": "fq_codel"},
        "rationale": "Removes artificial bandwidth limits",
        "risk_level": "low",
    },
]


class TestParamAliases:
    """Test that aliased parameter keys get resolved to config allowlist names.

    Note: _resolve_param_alias is defined inside build() scope, so we test
    via _normalize_synthesized_recommendation which handles changes dicts.
    The actual alias resolution happens in save_recommendations_impl filtering.
    """

    def test_aliased_recs_normalize_preserves_changes(self):
        """Verify normalization doesn't drop these — the issue is in
        the allowlist filter, not normalization."""
        for item in RECS_ALIASED_KEYS:
            result = _normalize_synthesized_recommendation(item)
            assert result.get("changes"), f"Changes lost for {item['title']}"

    def test_selinux_mode_alias_in_changes(self):
        """selinux_mode should be resolved to selinux by the alias map."""
        item = RECS_ALIASED_KEYS[0]
        result = _normalize_synthesized_recommendation(item)
        # Changes dict still has the original key at this stage
        # (alias resolution happens in save_recommendations_impl)
        assert "selinux_mode" in result["changes"]

    def test_cgroup_case_variants_in_changes(self):
        item = RECS_ALIASED_KEYS[1]
        result = _normalize_synthesized_recommendation(item)
        assert "cgroup_IOWeight" in result["changes"]
        assert "cgroup_CPUWeight" in result["changes"]


class TestSimulateFullPipeline:
    """Simulate the full pipeline: coerce -> normalize -> clean for planner."""

    def test_run1_rca_then_recs_pipeline(self):
        """Simulate run 1: alternative schema for both RCA and recommendations."""
        # Step 1: RCA records should survive coercion
        rca = _coerce_records(RCA_RUN1_RAW)
        assert len(rca) == 10, f"RCA: expected 10, got {len(rca)}"

        # Step 2: Recommendations should survive coercion
        recs = _coerce_recommendations(RECS_RECOMMENDED_VALUE_STYLE)
        assert len(recs) >= 3, f"Recs: expected >=3, got {len(recs)}"

        # Step 3: Clean for planner should produce clean values
        cleaned = _clean_recs_for_planner(recs)
        for r in cleaned:
            for v in r.get("changes", {}).values():
                assert ";" not in v, f"Dirty value for planner: {v}"

    def test_run2_dirty_values_pipeline(self):
        """Simulate run 2: correct schema but dirty values."""
        # Step 1: RCA with correct schema — should pass
        rca = _coerce_records(RCA_RUN2_CORRECT)
        assert len(rca) == 2

        # Step 2: Dirty recommendations — should survive with clean values
        recs = _coerce_recommendations(RECS_DIRTY_VALUES)
        assert len(recs) == 3

        # Step 3: Clean for planner
        cleaned = _clean_recs_for_planner(recs)
        assert cleaned[0]["changes"]["worker_processes"] == "auto"
