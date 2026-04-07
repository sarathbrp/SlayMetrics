"""Service Profile registry — loads service-specific knowledge per service type.

The adapter handles I/O (SSH, config editing).
The service profile provides knowledge (what params exist, what to tune, expert prompt).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ServiceProfile:
    """All service-specific knowledge needed by the agent."""

    name: str  # "nginx", "postgres", "redis"
    type: str  # "webserver", "database", "cache"
    process_name: str  # "nginx", "postgres", "redis-server"
    binary_path: str  # "/usr/sbin/nginx"
    default_config: str  # default config file content for reset
    service_targets: dict[str, str] = field(default_factory=dict)
    optimization_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    degrade_scenarios: list[dict[str, str]] = field(default_factory=list)
    eval_weights: dict[str, float] = field(
        default_factory=lambda: {"service": 0.4, "system": 0.4, "synthesizer": 0.2}
    )
    expert_prompt_builder: Callable[..., str] | None = None


def load_profile(name: str) -> ServiceProfile:
    """Load a ServiceProfile by service name.

    Imports services.{name} and calls get_profile().
    Falls back to a minimal profile if the module doesn't exist.
    """
    try:
        module = importlib.import_module(f"services.{name}")
        return module.get_profile()
    except (ModuleNotFoundError, AttributeError):
        return ServiceProfile(
            name=name,
            type="unknown",
            process_name=name,
            binary_path=f"/usr/sbin/{name}",
            default_config="",
        )
