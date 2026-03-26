from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

import yaml
from rich.console import Console

from agents import AgentDeps, TokenCounter
from adapters import load_adapter
from memory.embeddings import from_config as embedder_from_config
from memory.tidb_store import from_config as tidb_from_config
from tools.ssh import from_config as ssh_from_config

console = Console()


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def get_model(cfg: dict):
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.models.openai import OpenAIModel

    profile_name = cfg["llm"]["active_profile"]
    profile = cfg["llm"]["profiles"][profile_name]
    console.print(f"[dim]LLM profile:[/dim] [bold]{profile_name}[/bold] "
                  f"({profile['backend']} / {profile['model']})")

    match profile["backend"]:
        case "claude":
            api_key = os.environ.get(profile.get("api_key_env", "ANTHROPIC_API_KEY"))
            if not api_key:
                console.print(
                    f"[red]ERROR:[/red] {profile.get('api_key_env','ANTHROPIC_API_KEY')} "
                    f"not set in environment."
                )
                sys.exit(1)
            return AnthropicModel(profile["model"])
        case "vllm" | "ollama":
            return OpenAIModel(
                profile["model"],
                base_url=profile["base_url"],
            )
        case _:
            console.print(f"[red]Unknown backend:[/red] {profile['backend']}")
            sys.exit(1)


async def main(config_path: str, session_id: str | None) -> None:
    cfg = load_config(config_path)

    # Wire up dependencies
    embedder = embedder_from_config(cfg)
    memory = tidb_from_config(cfg, embedder)
    memory.connect()

    ssh = ssh_from_config(cfg)
    ssh.connect()

    adapter = load_adapter(cfg, ssh)
    model = get_model(cfg)

    # Session: resume or create new
    if session_id is None:
        session_id = str(uuid.uuid4())[:8]
        console.print(f"[dim]New session:[/dim] {session_id}")
    else:
        console.print(f"[dim]Resuming session:[/dim] {session_id}")

    profile_name = cfg["llm"]["active_profile"]
    if not memory.session_exists(session_id):
        memory.create_session(
            session_id=session_id,
            service=cfg["service"]["name"],
            host=cfg["target"]["host"],
            llm_profile=profile_name,
        )

    token_counter = TokenCounter()
    deps = AgentDeps(
        adapter=adapter,
        memory=memory,
        ssh=ssh,
        session_id=session_id,
        config=cfg,
        token_counter=token_counter,
    )

    try:
        from core.orchestrator import run
        report_path = await run(model, deps)
        console.print(f"\n[bold green]Report:[/bold green] {report_path}")
    finally:
        ssh.disconnect()
        memory.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SlayMetricsAgent")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config file (default: config.yaml)")
    parser.add_argument("--session", default=None,
                        help="Resume an existing session ID")
    args = parser.parse_args()
    asyncio.run(main(args.config, args.session))
