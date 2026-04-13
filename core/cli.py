"""CLI entry point: argument parsing, validation, fleet/single-target dispatch."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime

from .config import Config
from .rca_agent import RCAAgent
from .orchestrator import (
    FleetTarget, InstallerOrchestrator,
    _target_env, _audit_target, _collect_parallel_audits,
    _normalize_filter_values, _select_targets,
)
from .constants import CONFIG_PATH, LOGS_DIR, LOG_FORMAT, LOG_DATEFMT

logger = logging.getLogger("slayMetrics.cli")


def _list_targets(targets: list[FleetTarget]) -> None:
    grouped: dict[str, list[FleetTarget]] = defaultdict(list)
    for t in targets:
        grouped[t["group"]].append(t)
    logger.info("Configured targets:")
    for group in sorted(grouped):
        logger.info("  [%s]", group)
        for t in sorted(grouped[group], key=lambda x: x["name"]):
            logger.info("    - %s (%s)", t["name"], t["host"])


def _check_required_llm_env() -> bool:
    required = ("GPT_OSS_BASE_URL", "GPT_OSS_API_KEY", "GPT_OSS_MODEL")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error(
            "Missing required LLM environment variables: %s. "
            "Set them in shell or .env before running agent workflows.",
            ", ".join(missing),
        )
        return False
    return True


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SlayMetrics RCA agent.")
    parser.add_argument("--audit", action="store_true",
                        help="Audit-only mode: collect RCA and suggested fixes, do not apply.")
    parser.add_argument("--fleet", action="store_true",
                        help="Use config.yaml targets: audit in parallel, benchmark/RCA sequentially.")
    parser.add_argument("--orchestrate", action="store_true",
                        help="Run through installer: audits/benchmarks execute via installer host.")
    parser.add_argument("--installer",
                        help="Installer IP/host used with --orchestrate.")
    parser.add_argument("--confirm-remediation", action="store_true",
                        help="Required for --orchestrate without --audit to allow remediation.")
    parser.add_argument("--target-group", action="append", default=[],
                        help="Run only a target group (repeatable or comma-separated).")
    parser.add_argument("--target", action="append", default=[],
                        help="Run only specific target name(s) or IP(s) (repeatable or comma-separated).")
    parser.add_argument("--target-password",
                        help="Password for installer->target SSH (applied to selected targets).")
    parser.add_argument("--list-targets", action="store_true",
                        help="Print configured target groups/nodes from config.yaml and exit.")
    return parser.parse_args()


def main() -> None:
    args   = _parse_args()
    config = Config(CONFIG_PATH)

    log_level = getattr(logging, config.log_level, logging.INFO)
    logging.getLogger().setLevel(log_level)

    targets: list[FleetTarget] = config.target_specs
    if args.list_targets:
        _list_targets(targets)
        return

    if not _check_required_llm_env():
        return

    if args.orchestrate and not args.fleet:
        logger.error("--orchestrate requires --fleet.")
        return
    if args.orchestrate and not args.installer:
        logger.error("--orchestrate requires --installer <ip_or_host>.")
        return
    if args.orchestrate and not args.audit and not args.confirm_remediation:
        if sys.stdin.isatty():
            try:
                ans = input(
                    "Orchestrate mode without --audit will apply remediation across fleet. "
                    "Continue? [y/N]: "
                ).strip().lower()
            except EOFError:
                ans = ""
            if ans not in {"y", "yes"}:
                logger.info("Cancelled by user (remediation not confirmed).")
                return
        else:
            logger.error(
                "Refusing remediation in orchestrate mode without confirmation. "
                "Add --audit or pass --confirm-remediation."
            )
            return

    LOGS_DIR.mkdir(exist_ok=True)
    log_file     = LOGS_DIR / f"audit_rca_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    logger.info("=" * 60)
    logger.info("slayMetrics Agent starting")
    logger.info("Log level : %s", config.log_level)
    logger.info("Log file  : %s", log_file)
    mode = (
        "orchestrate+fleet+audit"       if args.orchestrate and args.audit else
        "orchestrate+fleet+remediation" if args.orchestrate else
        "fleet+audit"                   if args.fleet and args.audit else
        "fleet+remediation"             if args.fleet else
        "audit-only"                    if args.audit else
        "full remediation"
    )
    logger.info("Mode      : %s", mode)
    if args.orchestrate:
        logger.info("Installer : %s@%s:%s",
                    config.orchestration_installer_user, args.installer,
                    config.orchestration_installer_port)
    logger.info("=" * 60)

    group_filter  = _normalize_filter_values(args.target_group)
    target_filter = _normalize_filter_values(args.target)
    if group_filter or target_filter:
        selected = _select_targets(targets, group_filter, target_filter)
        if not selected:
            logger.error("No targets matched filters. groups=%s targets=%s",
                         sorted(group_filter), sorted(target_filter))
            logger.info("Tip: run with --list-targets to inspect available names/groups.")
            return
        logger.info("Target filters applied: groups=%s targets=%s selected=%d/%d",
                    sorted(group_filter) if group_filter else ["*"],
                    sorted(target_filter) if target_filter else ["*"],
                    len(selected), len(targets))
        targets = selected

    if args.target_password:
        for t in targets:
            t["password"] = args.target_password
        logger.info("Applied --target-password to %d selected target(s).", len(targets))

    if not targets:
        logger.error("No targets configured.")
        return

    if args.fleet:
        _run_fleet(args, config, targets)
    else:
        _run_single(args, config, targets)


def _run_fleet(args: argparse.Namespace, config: Config, targets: list[FleetTarget]) -> None:
    logger.info("Fleet mode enabled for %d targets.", len(targets))
    orchestrator: InstallerOrchestrator | None = None
    audit_fn = _audit_target
    if args.orchestrate:
        orchestrator = InstallerOrchestrator(config, args.installer)
        try:
            orchestrator.setup()
            orchestrator.prepare_target_auth(targets)
        except Exception as e:
            logger.error("Failed to prepare installer workspace: %s", e)
            return
        audit_fn = orchestrator.audit_target

    try:
        audits = _collect_parallel_audits(config, targets, audit_fn=audit_fn)
        if not audits:
            logger.error("Fleet pre-audit failed for all targets — stopping.")
            return

        completed = failed = skipped = 0
        for idx, target in enumerate(targets, 1):
            name = target["name"]
            host = target["host"]
            pre_audit = audits.get(name, "")
            if not pre_audit:
                skipped += 1
                logger.warning("Skipping target %d/%d [%s %s] — no audit output.",
                               idx, len(targets), name, host)
                continue

            logger.info("Starting target %d/%d [%s %s] (benchmark phase is sequential).",
                        idx, len(targets), name, host)
            try:
                with _target_env(target):
                    agent = RCAAgent(config, audit_only=args.audit,
                                     orchestrator=orchestrator, target=target)
                    agent.analyzer.configure()
                    result = agent.run(initial_state={"audit_output": pre_audit})
                    if result.get("error"):
                        failed += 1
                    else:
                        completed += 1
            except Exception as e:  # one target failure must not abort remaining targets
                failed += 1
                logger.error("Unhandled failure for target [%s %s]: %s", name, host, e, exc_info=True)

        logger.info("Fleet run finished: completed=%d failed=%d skipped=%d total=%d",
                    completed, failed, skipped, len(targets))
    finally:
        if orchestrator:
            orchestrator.cleanup()


def _run_single(args: argparse.Namespace, config: Config, targets: list[FleetTarget]) -> None:
    # In single-target mode, always use the target: section (with .env overrides)
    # rather than the fleet targets: list.
    target: FleetTarget = {
        "name": "dut",
        "group": "default",
        "host": config.dut_host,
        "user": config.dut_user,
        "private_key_path": config.dut_key,
        "password": config.orchestration_target_password,
        "port": config.dut_port,
        "connect_timeout_seconds": config.dut_timeout,
    }
    logger.info("Single target mode: [%s %s]", target["name"], target["host"])
    with _target_env(target):
        agent = RCAAgent(config, audit_only=args.audit)
        agent.analyzer.configure()
        agent.run()
