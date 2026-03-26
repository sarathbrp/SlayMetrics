from __future__ import annotations

import importlib

from adapters.base import ServiceAdapter
from tools.ssh import LocalClient, SSHClient


def load_adapter(cfg: dict, ssh: LocalClient | SSHClient) -> ServiceAdapter:
    name = cfg["service"]["name"].lower()
    module = importlib.import_module(f"adapters.{name}")
    cls_name = name.capitalize() + "Adapter"
    cls = getattr(module, cls_name)
    return cls(cfg, ssh)
