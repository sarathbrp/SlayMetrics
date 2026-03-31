"""Slack notifier — sends structured messages at key agent lifecycle events.

Configuration is in config.yaml under `slack:`. The bot token is read
from the env var specified by `token_env` (default: SLACK_AUTH_TOKEN).
"""

from __future__ import annotations

import os
import traceback
from typing import Any

import requests

from core import log as logger


class SlackNotifier:
    """Sends Slack messages at agent lifecycle events."""

    def __init__(self, config: dict[str, Any]):
        slack_cfg = config.get("slack") or {}
        self.enabled = bool(slack_cfg.get("enabled", False))
        self.channel = slack_cfg.get("channel", "")
        self.notify_on = set(slack_cfg.get("notify_on") or [])
        token_env = slack_cfg.get("token_env", "SLACK_AUTH_TOKEN")
        self.token = os.environ.get(token_env, "")

        if self.enabled and not self.token:
            logger.status("slack", f"WARNING: {token_env} not set — Slack disabled")
            self.enabled = False
        if self.enabled and not self.channel:
            logger.status("slack", "WARNING: no channel configured — Slack disabled")
            self.enabled = False

    def _post(self, blocks: list[Any], text: str = "") -> bool:
        """Post a message to Slack. Returns True on success."""
        if not self.enabled:
            return False
        try:
            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "channel": self.channel,
                    "text": text or "SlayMetrics notification",
                    "blocks": blocks,
                },
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.status("slack", f"API error: {data.get('error', 'unknown')}")
                return False
            return True
        except Exception:
            logger.status("slack", f"Send failed: {traceback.format_exc()[:100]}")
            return False

    # ── Event methods ────────────────────────────────────────────────────

    def notify_run_start(
        self,
        session_id: str,
        dut_host: str,
        llm_profile: str,
        cpu_cores: int,
        ram_gb: int,
        issue_count: dict[str, int] | None = None,
    ) -> None:
        if "run_start" not in self.notify_on:
            return
        issues_text = ""
        if issue_count:
            issues_text = " | ".join(f"{k}: {v}" for k, v in issue_count.items() if v)
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": ":wrench: SlayMetrics Run Started"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Session:*\n`{session_id}`"},
                    {"type": "mrkdwn", "text": f"*DUT:*\n`{dut_host}`"},
                    {"type": "mrkdwn", "text": f"*LLM:*\n`{llm_profile}`"},
                    {"type": "mrkdwn", "text": f"*System:*\n{cpu_cores} cores, {ram_gb} GB RAM"},
                ],
            },
        ]
        if issues_text:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Issues detected:* {issues_text}",
                    },
                }
            )
        self._post(blocks, f"SlayMetrics run started: {session_id}")

    def notify_baseline_complete(
        self,
        session_id: str,
        baselines: dict[str, Any],
    ) -> None:
        if "baseline_complete" not in self.notify_on:
            return
        rows = []
        for wl in ("homepage", "small", "medium", "large", "mixed"):
            data = baselines.get(wl, {})
            rps = data.get("rps", 0)
            p99 = data.get("p99", 0)
            if rps or p99:
                rows.append(f"`{wl:10s}` {rps:>12,.0f} req/s  p99: {p99:>8.1f} ms")
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": ":bar_chart: Baseline Benchmark Complete"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Session:* `{session_id}`\n```\n" + "\n".join(rows) + "\n```",
                },
            },
        ]
        self._post(blocks, f"Baseline complete: {session_id}")

    def notify_iteration_complete(
        self,
        session_id: str,
        iteration: int,
        baselines: dict[str, Any],
        results: dict[str, Any],
        applied_params: dict[str, dict[str, str]] | None = None,
        failed_params: list[str] | None = None,
        guardrails_triggered: list[str] | None = None,
        eval_results: dict[str, Any] | None = None,
        tokens_used: int = 0,
        decision: str = "",
    ) -> None:
        if "iteration_complete" not in self.notify_on:
            return

        # Build benchmark comparison
        rows = []
        for wl in ("homepage", "small", "medium", "large", "mixed"):
            base_rps = baselines.get(wl, {}).get("rps", 0)
            curr_rps = results.get(wl, {}).get("rps", 0)
            if not base_rps and not curr_rps:
                continue
            pct = ((curr_rps - base_rps) / base_rps * 100) if base_rps else 0
            status = ":white_check_mark:" if pct >= 0 else ":x:"
            rows.append(
                f"{status} `{wl:10s}` {base_rps:>10,.0f} -> {curr_rps:>10,.0f}  ({pct:+.1f}%)"
            )

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f":gear: Iteration {iteration} Complete",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Session:* `{session_id}` | *Tokens:* {tokens_used:,}\n" + "\n".join(rows)
                    ),
                },
            },
        ]

        # Applied params by category
        if applied_params:
            param_lines = []
            for cat, params in applied_params.items():
                if not params:
                    continue
                param_strs = ", ".join(f"`{k}={v}`" for k, v in list(params.items())[:8])
                overflow = len(params) - 8
                if overflow > 0:
                    param_strs += f" +{overflow} more"
                param_lines.append(f"*{cat}:* {param_strs}")
            if param_lines:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Applied:*\n" + "\n".join(param_lines),
                        },
                    }
                )

        # Failed params
        if failed_params:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":warning: *Failed:* {', '.join(f'`{p}`' for p in failed_params)}",
                    },
                }
            )

        # Guardrails
        if guardrails_triggered:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":shield: *Guardrails:* "
                            + ", ".join(f"`{g}`" for g in guardrails_triggered)
                        ),
                    },
                }
            )

        # Eval results
        if eval_results and eval_results.get("findings"):
            eval_lines = []
            for finding in eval_results["findings"][:10]:
                if not isinstance(finding, dict):
                    continue
                severity = finding.get("severity", "info")
                check = finding.get("check_id", finding.get("agent", "?"))
                score = finding.get("score", "")
                icon = (
                    ":white_check_mark:"
                    if severity == "pass"
                    else (":warning:" if severity == "warn" else ":x:")
                )
                score_str = f" ({score:.2f})" if isinstance(score, (int, float)) else ""
                msg = finding.get("message", "")[:60]
                eval_lines.append(f"{icon} `{check}`{score_str} {msg}")
            if eval_lines:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Eval:*\n" + "\n".join(eval_lines),
                        },
                    }
                )

        # Decision
        if decision:
            ctx_block: dict[str, Any] = {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"*Decision:* {decision}"}],
            }
            blocks.append(ctx_block)

        self._post(blocks, f"Iteration {iteration} complete: {session_id}")

    def notify_run_complete(
        self,
        session_id: str,
        baselines: dict[str, Any],
        finals: dict[str, Any],
        total_tokens: int,
        leaderboard_position: int | None = None,
        report_path: str = "",
        applied_params: dict[str, dict[str, str]] | None = None,
    ) -> None:
        if "run_complete" not in self.notify_on:
            return

        rows = []
        for wl in ("small", "medium", "large"):
            base_rps = baselines.get(wl, {}).get("rps", 0)
            final_rps = finals.get(wl, {}).get("rps", 0)
            pct = ((final_rps - base_rps) / base_rps * 100) if base_rps else 0
            icon = ":rocket:" if pct > 1000 else (":white_check_mark:" if pct >= 0 else ":x:")
            rows.append(
                f"{icon} `{wl:8s}` {base_rps:>10,.0f} -> {final_rps:>10,.0f}  ({pct:+,.1f}%)"
            )

        leaderboard_text = ""
        if leaderboard_position is not None:
            medal = {1: ":first_place_medal:", 2: ":second_place_medal:", 3: ":third_place_medal:"}
            icon = medal.get(leaderboard_position, ":chart_with_upwards_trend:")
            leaderboard_text = f"\n{icon} *Leaderboard:* #{leaderboard_position}"

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": ":checkered_flag: SlayMetrics Run Complete"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Session:* `{session_id}` | *Total tokens:* {total_tokens:,}"
                        f"{leaderboard_text}\n" + "\n".join(rows)
                    ),
                },
            },
        ]

        # Top applied params summary
        if applied_params:
            param_lines = []
            for cat, params in applied_params.items():
                if params:
                    top = ", ".join(f"`{k}={v}`" for k, v in list(params.items())[:5])
                    param_lines.append(f"*{cat}:* {top}")
            if param_lines:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Key params:*\n" + "\n".join(param_lines),
                        },
                    }
                )

        if report_path:
            report_block: dict[str, Any] = {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f":page_facing_up: Report: `{report_path}`"}
                ],
            }
            blocks.append(report_block)

        self._post(blocks, f"SlayMetrics run complete: {session_id}")

    def notify_error(
        self,
        session_id: str,
        error: str,
        context: str = "",
    ) -> None:
        if "error" not in self.notify_on:
            return
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": ":rotating_light: SlayMetrics Error"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (f"*Session:* `{session_id}`\n*Error:*\n```\n{error[:1500]}\n```"),
                },
            },
        ]
        if context:
            err_ctx: dict[str, Any] = {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": context[:300]}],
            }
            blocks.append(err_ctx)
        self._post(blocks, f"SlayMetrics error: {session_id}")
