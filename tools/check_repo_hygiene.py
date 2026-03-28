from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path

ALLOWED_LITERAL_URL_HOSTS = {"127.0.0.1", "0.0.0.0"}
PLACEHOLDER_HINTS = (
    "your-",
    "example",
    "changeme",
    "replace-me",
    "<",
    "${",
)
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".ini",
    ".cfg",
    ".conf",
    ".env",
    ".example",
    ".txt",
    ".sh",
}

URL_WITH_IP_RE = re.compile(r"https?://(?P<host>\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:/[^\s\"'`]*)?")
BEARER_TOKEN_RE = re.compile(r"Bearer\s+(sk-[A-Za-z0-9_-]{20,})")
SECRET_VALUE_RE = re.compile(
    r"(?P<key>(?:API|ACCESS|AUTH|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*[:=]\s*"
    r"(?P<quote>[\"']?)(?P<value>[^\"'\s#]+)(?P=quote)",
    re.IGNORECASE,
)


def _is_text_file(path: Path) -> bool:
    if path.suffix in TEXT_SUFFIXES:
        return True
    return path.name in {".env.example", ".pre-commit-config.yaml", "README.md", "config.yaml"}


def _should_skip_secret_value(value: str) -> bool:
    lowered = value.lower()
    return (
        len(value) < 12
        or lowered in {"true", "false", "null", "none"}
        or any(hint in lowered for hint in PLACEHOLDER_HINTS)
    )


def _check_file(path_str: str) -> list[str]:
    path = Path(path_str)
    if not path.exists() or not path.is_file() or not _is_text_file(path):
        return []
    if path.name == "check_repo_hygiene.py":
        return []  # don't scan ourselves — regex patterns trigger false positives

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    errors: list[str] = []
    for match in URL_WITH_IP_RE.finditer(text):
        host = match.group("host")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if str(ip) in ALLOWED_LITERAL_URL_HOSTS:
            continue
        errors.append(f"{path}: raw IP URL blocked: {match.group(0)}")

    for match in BEARER_TOKEN_RE.finditer(text):
        errors.append(f"{path}: bearer token-like value blocked")

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "pragma: allowlist secret" in line:
            continue
        secret_match = SECRET_VALUE_RE.search(line)
        if not secret_match:
            continue
        key = secret_match.group("key")
        if key.lower().endswith("_env"):
            continue
        value = secret_match.group("value").strip()
        if _should_skip_secret_value(value):
            continue
        errors.append(f"{path}:{lineno}: secret-like assignment blocked for {key}")

    return errors


def main(argv: list[str]) -> int:
    errors: list[str] = []
    for path_str in argv[1:]:
        errors.extend(_check_file(path_str))

    if errors:
        print("repo hygiene checks failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
