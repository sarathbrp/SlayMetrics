"""Prompt for the NGINX/webserver performance expert agent."""

import json
from typing import Any


def build(*, system_line: str, webserver_inspection: dict[str, Any]) -> str:
    return (
        "You are an NGINX/webserver performance expert. "
        "Review the webserver inspection evidence. "
        "Return strict JSON with keys summary, rca_records, recommendations.\n\n"
        f"System: {system_line}\n"
        f"Webserver Inspection:\n{json.dumps(webserver_inspection, ensure_ascii=True)}"
    )
