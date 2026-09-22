from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from collections.abc import Mapping

@dataclass(frozen=True)
class Config:
    default_context_limit: int
    default_context_content_chars: int
    default_context_max_chars: int
    default_status: str


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ValueError(f"Config file not found: {path}")
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Invalid config: {exc}") from exc

    limit = raw.get("default_context_limit")
    content_chars = raw.get("default_context_content_chars")
    max_chars = raw.get("default_context_max_chars")
    status = raw.get("default_status")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("default_context_limit must be an integer from 1 to 1000")
    if type(content_chars) is not int or not 1 <= content_chars <= 100_000:
        raise ValueError("default_context_content_chars must be an integer from 1 to 100000")
    if type(max_chars) is not int or not 500 <= max_chars <= 1_000_000:
        raise ValueError("default_context_max_chars must be an integer from 500 to 1000000")
    if status != "candidate":
        raise ValueError('default_status must be "candidate"')
    return Config(limit, content_chars, max_chars, status)


def resolve_recallry_root(
    explicit_root: Path | None, environment: Mapping[str, str], *, home: Path | None = None,
) -> Path:
    if explicit_root is not None:
        return explicit_root.expanduser().resolve()
    configured = environment.get("RECALLRY_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return ((home or Path.home()) / "Recallry").resolve()
