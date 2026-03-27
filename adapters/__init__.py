from __future__ import annotations

import importlib

from adapters.base import ServiceAdapter
from tools.ssh import LocalClient, SSHClient


def load_adapter(cfg: dict, ssh: LocalClient | SSHClient,
                  bench: LocalClient | SSHClient | None = None) -> ServiceAdapter:
    name = cfg["service"]["name"].lower()
    module = importlib.import_module(f"adapters.{name}")
    cls_name = name.capitalize() + "Adapter"
    cls = getattr(module, cls_name)
    # Pass bench executor if the adapter supports it
    import inspect
    if "bench" in inspect.signature(cls.__init__).parameters:
        return cls(cfg, ssh, bench=bench)
    return cls(cfg, ssh)
