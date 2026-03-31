from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

AGENT_WEIGHTS = {"nginx": 0.4, "rhel": 0.4, "synthesizer": 0.2}
PASS_THRESHOLDS = {"self_correct": 0.5, "recommended_improvements": 1.0}
GOLDEN_RANGES = {
    "net.core.somaxconn": (32768, 65535),
    "net.ipv4.tcp_max_syn_backlog": (32768, 65535),
    "net.core.netdev_max_backlog": (32768, 65535),
}


def load_case_bundle(path: str | Path) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def build_case_bundle_from_session(
    memory,
    session_id: str,
    *,
    iteration: int | None = None,
) -> dict[str, Any]:
    profile = memory.get_profile(session_id) or {}
    contexts = memory.get_contexts(session_id, "command_output", limit=500) or []
    by_source = {str(row.get("source")): row for row in contexts}

    target_iteration = iteration or _detect_latest_iteration(by_source)
    prefix = f"iter{target_iteration}_" if target_iteration else ""

    inspection = _load_context_json(by_source, "compound_inspection") or {}
    nginx_expert = _load_context_json(by_source, f"{prefix}nginx_expert") or {}
    rhel_expert = _load_context_json(by_source, f"{prefix}rhel_expert") or {}
    synthesizer = _load_context_json(by_source, f"{prefix}synthesizer") or {}

    system_from_inspection = inspection.get("system") or {}
    system = {
        "os_cpu_count": system_from_inspection.get("os_cpu_count") or profile.get("cpu_cores"),
        "ram_gb": system_from_inspection.get("ram_gb") or profile.get("ram_gb"),
        "cgroup_cpu_quota_cores": system_from_inspection.get("cgroup_cpu_quota_cores"),
        "cpuset_cpu_count": system_from_inspection.get("cpuset_cpu_count"),
        "host": profile.get("host"),
        "service": profile.get("service"),
    }

    return {
        "session_id": session_id,
        "iteration": target_iteration,
        "system": system,
        "inspection": inspection,
        "nginx_expert": nginx_expert,
        "rhel_expert": rhel_expert,
        "synthesizer": synthesizer,
    }


def evaluate_case_bundle(
    bundle: dict[str, Any],
    *,
    synth_judge: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    nginx_findings = evaluate_nginx(bundle)
    rhel_findings = evaluate_rhel(bundle)
    synth_findings = evaluate_synthesizer(bundle, synth_judge=synth_judge)

    nginx_score = _score_findings(nginx_findings)
    rhel_score = _score_findings(rhel_findings)
    synthesizer_score = _score_findings(synth_findings)

    total_score = round(
        (nginx_score * AGENT_WEIGHTS["nginx"])
        + (rhel_score * AGENT_WEIGHTS["rhel"])
        + (synthesizer_score * AGENT_WEIGHTS["synthesizer"]),
        3,
    )
    action = _action_for_score(total_score)
    findings = nginx_findings + rhel_findings + synth_findings
    return {
        "session_id": bundle.get("session_id"),
        "iteration": bundle.get("iteration"),
        "nginx_score": nginx_score,
        "rhel_score": rhel_score,
        "synthesizer_score": synthesizer_score,
        "total_score": total_score,
        "action": action,
        "findings": findings,
        "summary": (
            f"nginx={nginx_score:.2f}, rhel={rhel_score:.2f}, "
            f"synthesizer={synthesizer_score:.2f}, total={total_score:.2f} → {action}"
        ),
    }


def evaluate_nginx(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    inspection = bundle.get("inspection") or {}
    system = bundle.get("system") or {}
    nginx_output = bundle.get("nginx_expert") or {}
    targets = _extract_setting_targets(nginx_output)
    current = (inspection.get("webserver") or {}).get("current") or {}

    effective_cpu_budget = _effective_cpu_budget(system)
    worker_processes_value = targets.get("worker_processes", current.get("worker_processes"))
    worker_connections_value = targets.get("worker_connections", current.get("worker_connections"))
    worker_rlimit_nofile_value = targets.get(
        "worker_rlimit_nofile", current.get("worker_rlimit_nofile")
    )

    effective_worker_processes = _resolve_worker_processes(
        worker_processes_value,
        effective_cpu_budget=effective_cpu_budget,
        os_cpu_count=_parse_int(system.get("os_cpu_count")),
    )
    worker_connections = _parse_int(worker_connections_value)
    worker_rlimit_nofile = _parse_int(worker_rlimit_nofile_value)

    if (
        effective_worker_processes is None
        or worker_connections is None
        or worker_rlimit_nofile is None
    ):
        findings.append(
            _finding(
                "nginx",
                "nginx.recommendation_consistency",
                "fail",
                -0.4,
                "Critical Nginx numeric target is missing or unparsable.",
                evidence_refs=["nginx_expert", "inspection.webserver.current"],
            )
        )
        return findings

    total_connection_budget = effective_worker_processes * worker_connections
    base_headroom = max(1024, math.ceil(total_connection_budget * 0.01))
    required_nofile = (total_connection_budget * 2) + base_headroom
    correction_nofile = _round_up(required_nofile, 4096)
    if worker_rlimit_nofile < required_nofile:
        findings.append(
            _finding(
                "nginx",
                "nginx.fd_capacity",
                "fail",
                -0.5,
                (
                    f"worker_rlimit_nofile={worker_rlimit_nofile} is below required "
                    f"FD capacity {required_nofile}."
                ),
                correction=(
                    "Error: File descriptor limit "
                    f"({worker_rlimit_nofile}) is lower than safe proxy budget "
                    f"({required_nofile}). Recommended Target: {correction_nofile}."
                ),
                evidence_refs=[
                    "nginx_expert.rca_records",
                    "inspection.webserver.current.worker_rlimit_nofile",
                ],
            )
        )

    if effective_cpu_budget is not None:
        if effective_worker_processes > effective_cpu_budget:
            findings.append(
                _finding(
                    "nginx",
                    "nginx.hardware_saturation",
                    "fail",
                    -0.5,
                    (
                        f"Recommended worker_processes={effective_worker_processes} exceeds "
                        f"effective CPU budget {effective_cpu_budget}."
                    ),
                    correction=f"Set worker_processes to {effective_cpu_budget}.",
                    evidence_refs=[
                        "system.os_cpu_count",
                        "system.cgroup_cpu_quota_cores",
                        "system.cpuset_cpu_count",
                    ],
                )
            )
        elif effective_worker_processes < effective_cpu_budget:
            findings.append(
                _finding(
                    "nginx",
                    "nginx.hardware_saturation",
                    "warn",
                    -0.1,
                    (
                        f"worker_processes={effective_worker_processes} is below effective CPU "
                        f"budget {effective_cpu_budget}."
                    ),
                    tuning_hint=f"Raise worker_processes to {effective_cpu_budget}.",
                    evidence_refs=[
                        "system.os_cpu_count",
                        "system.cgroup_cpu_quota_cores",
                        "system.cpuset_cpu_count",
                    ],
                )
            )

    conflicts = _detect_conflicting_settings(nginx_output)
    if conflicts:
        findings.append(
            _finding(
                "nginx",
                "nginx.recommendation_consistency",
                "fail",
                -0.4,
                f"Conflicting Nginx recommendations found: {', '.join(conflicts)}.",
                evidence_refs=["nginx_expert.recommendations", "nginx_expert.rca_records"],
            )
        )

    return findings


def evaluate_rhel(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    inspection = bundle.get("inspection") or {}
    rhel_output = bundle.get("rhel_expert") or {}
    kernel_current = (inspection.get("kernel") or {}).get("current") or {}
    network_findings = (inspection.get("network") or {}).get("findings") or {}
    targets = _extract_setting_targets(rhel_output)
    mentioned_text = _payload_text(rhel_output)

    for param, (lower, upper) in GOLDEN_RANGES.items():
        current_value = _parse_int(kernel_current.get(param))
        target_value = _parse_int(targets.get(param))
        if current_value is None:
            continue
        if current_value <= 0:
            findings.append(
                _finding(
                    "rhel",
                    "rhel.sysctl_range",
                    "fail",
                    -0.5,
                    f"{param} has impossible current value {kernel_current.get(param)}.",
                    evidence_refs=[f"inspection.kernel.current.{param}"],
                )
            )
            continue
        if lower <= current_value <= upper:
            continue
        if target_value is not None and not (lower <= target_value <= upper):
            findings.append(
                _finding(
                    "rhel",
                    "rhel.sysctl_range",
                    "fail",
                    -0.5,
                    f"{param} recommendation target {target_value} is outside the golden range.",
                    evidence_refs=[f"rhel_expert.targets.{param}"],
                )
            )
        else:
            findings.append(
                _finding(
                    "rhel",
                    "rhel.sysctl_range",
                    "warn",
                    -0.1,
                    f"{param} current value {current_value} is outside the golden range.",
                    tuning_hint=f"Target {param} between {lower} and {upper}.",
                    evidence_refs=[f"inspection.kernel.current.{param}"],
                )
            )

    mentions_firewall = any(
        token in mentioned_text for token in ("iptables", "nftables", "firewalld")
    )
    firewall_provenance = network_findings.get("firewall_provenance") or {}
    firewalld_state = network_findings.get("firewalld_state")
    if mentions_firewall and not (firewall_provenance or firewalld_state):
        findings.append(
            _finding(
                "rhel",
                "rhel.firewall_dependency",
                "fail",
                -0.4,
                "Firewall-related recommendation is not grounded in inspection metadata.",
                evidence_refs=["rhel_expert", "inspection.network.findings"],
            )
        )

    claimed_params = _extract_rhel_claimed_params(rhel_output)
    unsupported = [param for param in claimed_params if param not in kernel_current]
    if unsupported:
        unsupported_list = ", ".join(sorted(unsupported))
        findings.append(
            _finding(
                "rhel",
                "rhel.recommendation_consistency",
                "fail",
                -0.5,
                (
                    "RHEL output references unsupported or unobserved "
                    f"parameters: {unsupported_list}."
                ),
                evidence_refs=["rhel_expert", "inspection.kernel.current"],
            )
        )

    return findings


def evaluate_synthesizer(
    bundle: dict[str, Any],
    *,
    synth_judge: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if synth_judge is None:
        return [
            _finding(
                "synthesizer",
                "synthesizer.judge_unavailable",
                "warn",
                -0.1,
                "Synthesizer judge was not provided; synthesis quality was not fully evaluated.",
                evidence_refs=["synthesizer"],
            )
        ]

    from core import log as logger

    logger.status("eval", "Running synthesizer judge...")
    try:
        judged = synth_judge(bundle)
    except TimeoutError:
        logger.log("eval", "Synthesizer judge timed out; falling back to warning.", "warn")
        return [
            _finding(
                "synthesizer",
                "synthesizer.judge_timeout",
                "warn",
                -0.1,
                "Synthesizer judge timed out; synthesis quality was not fully evaluated.",
                evidence_refs=["synthesizer"],
            )
        ]
    except Exception as exc:
        logger.log("eval", f"Synthesizer judge failed: {exc}", "warn")
        return [
            _finding(
                "synthesizer",
                "synthesizer.judge_failed",
                "warn",
                -0.1,
                f"Synthesizer judge failed: {exc}",
                evidence_refs=["synthesizer"],
            )
        ]
    findings: list[dict[str, Any]] = []
    for rule_id, payload in (judged or {}).items():
        passed = bool(payload.get("pass"))
        if passed:
            continue
        score_delta = _default_synth_delta(rule_id)
        findings.append(
            _finding(
                "synthesizer",
                f"synthesizer.{rule_id}",
                "fail" if score_delta <= -0.4 else "warn",
                score_delta,
                payload.get("message", f"Synthesizer failed {rule_id}."),
                evidence_refs=list(payload.get("evidence_refs") or []),
            )
        )
    return findings


def llm_synth_judge(
    model,
    bundle: dict[str, Any],
    *,
    timeout_sec: float = 300.0,
) -> dict[str, Any]:
    prompt = (
        "You are grading a synthesis artifact. Return strict JSON with keys "
        "hallucination, critical_omission, merge_fidelity, format_validity. "
        "Each key must contain {pass: bool, message: str, evidence_refs: list[str]}.\n\n"
        f"NGINX expert:\n{json.dumps(bundle.get('nginx_expert') or {}, ensure_ascii=True)}\n\n"
        f"RHEL expert:\n{json.dumps(bundle.get('rhel_expert') or {}, ensure_ascii=True)}\n\n"
        f"Synthesizer:\n{json.dumps(bundle.get('synthesizer') or {}, ensure_ascii=True)}\n\n"
        f"Requested format: {bundle.get('requested_format') or 'json'}"
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(model.invoke, prompt)
        try:
            response = future.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"synth judge exceeded {timeout_sec:.0f}s") from exc
    content = getattr(response, "content", response)
    if isinstance(content, list):
        text_parts = [
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        ]
        text = "".join(text_parts)
    else:
        text = str(content)
    return _parse_json_payload(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline debate-path eval harness")
    parser.add_argument("--bundle", help="Path to a case bundle JSON")
    parser.add_argument("--session", help="Session id to load from TiDB")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--iteration", type=int, default=None)
    parser.add_argument("--output", help="Write eval report JSON to this path")
    args = parser.parse_args(argv)

    if not args.bundle and not args.session:
        parser.error("Provide either --bundle or --session")

    synth_judge: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    bundle: dict[str, Any]

    if args.bundle:
        bundle = load_case_bundle(args.bundle)
    else:
        from main import load_config, load_dotenv
        from memory.embeddings import from_config as embedder_from_config
        from memory.tidb_store import from_config as tidb_from_config
        from models import create_model

        load_dotenv()
        cfg = load_config(args.config)
        from core import log as logger

        logger.status("eval", f"Loading session {args.session} for offline eval")
        embedder = embedder_from_config(cfg)
        memory = tidb_from_config(cfg, embedder)
        memory.connect()
        bundle = build_case_bundle_from_session(memory, args.session, iteration=args.iteration)
        try:
            model = create_model(cfg)
        except SystemExit:
            from core import log as logger

            logger.log(
                "eval",
                (
                    "Synthesizer judge unavailable; continuing with "
                    "deterministic nginx/rhel evals only."
                ),
                "warn",
            )
        else:
            judge_timeout_sec = float(
                ((cfg.get("agent") or {}).get("eval") or {}).get("synth_timeout_sec", 300.0)
                or 300.0
            )

            def synth_judge(payload: dict[str, Any]) -> dict[str, Any]:
                return llm_synth_judge(model, payload, timeout_sec=judge_timeout_sec)

    result = evaluate_case_bundle(bundle, synth_judge=synth_judge)
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


def _load_context_json(by_source: dict[str, dict[str, Any]], source: str) -> dict[str, Any] | None:
    row = by_source.get(source)
    if not row:
        return None
    content = row.get("content")
    if not isinstance(content, str):
        return None
    return _parse_json_payload(content)


def _detect_latest_iteration(by_source: dict[str, dict[str, Any]]) -> int:
    iterations = []
    for source in by_source:
        match = re.match(r"iter(\d+)_synthesizer$", str(source))
        if match:
            iterations.append(int(match.group(1)))
    return max(iterations) if iterations else 0


def _parse_json_payload(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def _extract_setting_targets(payload: dict[str, Any]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for record in payload.get("rca_records") or []:
        if isinstance(record, dict):
            setting = record.get("setting")
            target = record.get("target")
            if setting and target is not None:
                targets[str(setting)] = str(target)
    for rec in payload.get("recommendations") or []:
        if not isinstance(rec, dict):
            continue
        changes = rec.get("changes")
        if isinstance(changes, dict):
            for key, value in changes.items():
                targets[str(key)] = str(value)
    return targets


def _extract_rhel_claimed_params(payload: dict[str, Any]) -> set[str]:
    claimed: set[str] = set()
    text = _payload_text(payload)
    for param in GOLDEN_RANGES:
        if param in text:
            claimed.add(param)
    for rec in payload.get("recommendations") or []:
        if isinstance(rec, dict):
            changes = rec.get("changes")
            if isinstance(changes, dict):
                claimed.update(str(key) for key in changes)
    return claimed


def _payload_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False).lower()


def _resolve_worker_processes(
    raw_value: Any,
    *,
    effective_cpu_budget: int | None,
    os_cpu_count: int | None,
) -> int | None:
    text = "" if raw_value is None else str(raw_value).strip().lower()
    if not text:
        return None
    if text == "auto":
        return effective_cpu_budget or os_cpu_count
    return _parse_int(text)


def _effective_cpu_budget(system: dict[str, Any]) -> int | None:
    candidates = [
        _parse_int(system.get("os_cpu_count")),
        _parse_int(system.get("cgroup_cpu_quota_cores")),
        _parse_int(system.get("cpuset_cpu_count")),
    ]
    usable = [value for value in candidates if value is not None and value > 0]
    return min(usable) if usable else None


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"-?\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _detect_conflicting_settings(payload: dict[str, Any]) -> list[str]:
    seen: dict[str, str] = {}
    conflicts: list[str] = []
    for record in payload.get("rca_records") or []:
        if not isinstance(record, dict):
            continue
        setting = record.get("setting")
        target = record.get("target")
        if not setting or target is None:
            continue
        key = str(setting)
        value = str(target)
        if key in seen and seen[key] != value:
            conflicts.append(key)
        seen[key] = value
    return sorted(set(conflicts))


def _score_findings(findings: list[dict[str, Any]]) -> float:
    score = 1.0 + sum(float(finding.get("score_delta", 0.0) or 0.0) for finding in findings)
    return round(max(0.0, min(1.0, score)), 3)


def _action_for_score(score: float) -> str:
    if score < PASS_THRESHOLDS["self_correct"]:
        return "self_correct"
    if score < PASS_THRESHOLDS["recommended_improvements"]:
        return "recommended_improvements"
    return "clean_pass"


def _default_synth_delta(rule_id: str) -> float:
    return {
        "hallucination": -0.6,
        "critical_omission": -0.4,
        "merge_fidelity": -0.1,
        "format_validity": -0.4,
    }.get(rule_id, -0.1)


def _round_up(value: int, multiple: int) -> int:
    if multiple <= 0:
        return value
    return int(math.ceil(value / multiple) * multiple)


def _finding(
    agent: str,
    rule_id: str,
    severity: str,
    score_delta: float,
    message: str,
    *,
    correction: str | None = None,
    tuning_hint: str | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    finding = {
        "agent": agent,
        "rule_id": rule_id,
        "severity": severity,
        "score_delta": score_delta,
        "message": message,
        "evidence_refs": evidence_refs or [],
    }
    if correction:
        finding["correction"] = correction
    if tuning_hint:
        finding["tuning_hint"] = tuning_hint
    return finding


if __name__ == "__main__":
    raise SystemExit(main())
