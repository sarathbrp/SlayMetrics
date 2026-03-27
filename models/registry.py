from __future__ import annotations

import os
import sys

from core import log as logger


def create_model(cfg: dict):
    profile_name = cfg["llm"]["active_profile"]
    profile = cfg["llm"]["profiles"][profile_name]
    backend = profile["backend"]
    model_name = os.environ.get(profile.get("model_env", ""), "").strip() or profile["model"]
    logger.status("main", f"LLM profile: {profile_name} ({backend} / {model_name})")

    if backend == "claude":
        from langchain_anthropic import ChatAnthropic

        api_key = os.environ.get(profile.get("api_key_env", "ANTHROPIC_API_KEY"))
        if not api_key:
            logger.log(
                "main", f"{profile.get('api_key_env', 'ANTHROPIC_API_KEY')} not set", "error"
            )
            sys.exit(1)
        return ChatAnthropic(
            model=model_name,
            anthropic_api_key=api_key,
            temperature=0,
            max_retries=1,
        )

    if backend == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=model_name,
            base_url=profile.get("base_url", "http://localhost:11434"),
            temperature=0,
        )

    if backend == "openai":
        from langchain_openai import ChatOpenAI

        api_key_env = profile.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_env)
        base_url = profile.get("base_url", "").strip()
        if not api_key:
            logger.log("main", f"{api_key_env} not set", "error")
            sys.exit(1)
        if not base_url:
            logger.log("main", "OpenAI-compatible base_url not set", "error")
            sys.exit(1)
        return ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=0,
            max_retries=1,
        )

    logger.log("main", f"Unknown backend: {backend}", "error")
    sys.exit(1)
