"""Every environment variable the application reads.

Host runs see the same .env compose uses; already-set variables win, so inside the
container compose stays authoritative. Values are read per call, not at import, so
a test can change one without reimporting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(REPO_ROOT / ".env", override=False)


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name}={value!r} is not an integer") from exc


def env_bool(name: str, default: bool = False) -> bool:
    """python-dotenv loads strings only, so the parsing lives here."""
    value = os.getenv(name)
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name}={value!r} is not a boolean")


@dataclass(frozen=True)
class ClickHouseSettings:
    host: str
    port: int
    user: str
    password: str


def clickhouse() -> ClickHouseSettings:
    """Cube connection. Defaults to localhost for the documented host-run mode;
    compose overrides the host with the service name."""
    return ClickHouseSettings(
        host=env_str("CLICKHOUSE_HOST", "localhost"),
        port=env_int("CLICKHOUSE_PORT", 8123),
        user=env_str("CLICKHOUSE_USER", "default"),
        password=env_str("CLICKHOUSE_PASSWORD", "fpa"),
    )


def claude_api_model() -> str:
    return env_str("FPA_CLAUDE_MODEL", "claude-sonnet-5")


def claude_code_model() -> str | None:
    return env_str("FPA_CLAUDE_CODE_MODEL") or None


def gemini_model() -> str:
    return env_str("FPA_MODEL_ID", "gemini-2.5-flash")


def external_audit_log() -> str:
    return env_str("FPA_EXTERNAL_AUDIT_LOG", "logs/fpa_external_audit.jsonl")
