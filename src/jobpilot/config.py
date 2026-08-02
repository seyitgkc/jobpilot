"""Configuration loading.

The config file is TOML (see config.example.toml). Resolution order for the
path: explicit argument, JOBPILOT_CONFIG env var, ./config.toml. A missing
file yields pure defaults. Unknown sections or keys raise ConfigError so
typos surface immediately instead of silently using a default.

Secrets never live in config values; keys like mail.imap_pass_env name the
environment variable holding the secret.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class GatesConfig:
    resume_glance: bool = True
    submit_approval: bool = True
    auto_submit: bool = False


@dataclass(frozen=True)
class BrowserConfig:
    tab_cap: int = 8
    focus_policy: str = "notify_only"
    settle_ms: int = 1500


@dataclass(frozen=True)
class AIConfig:
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    temperature: float = 0.2
    max_tokens: int = 8192


@dataclass(frozen=True)
class NotifyConfig:
    channels: tuple[str, ...] = ("cowork", "ntfy")
    ntfy_topic: str = ""
    email_to: str = ""


@dataclass(frozen=True)
class MailConfig:
    imap_host: str = ""
    imap_user: str = ""
    imap_pass_env: str = "JOBPILOT_IMAP_PASS"
    poll_seconds: int = 300
    match_threshold: float = 0.75
    mark_seen: bool = False


@dataclass(frozen=True)
class ValidatorConfig:
    token_overlap_min: float = 0.35


@dataclass(frozen=True)
class PathsConfig:
    data_root: str = "./data"


@dataclass(frozen=True)
class Config:
    gates: GatesConfig = field(default_factory=GatesConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    mail: MailConfig = field(default_factory=MailConfig)
    validator: ValidatorConfig = field(default_factory=ValidatorConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)


_SECTIONS = {
    "gates": GatesConfig,
    "browser": BrowserConfig,
    "ai": AIConfig,
    "notify": NotifyConfig,
    "mail": MailConfig,
    "validator": ValidatorConfig,
    "paths": PathsConfig,
}


def _build_section(cls: type, table: dict, section: str):
    known = {f.name for f in fields(cls)}
    unknown = set(table) - known
    if unknown:
        raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    values = dict(table)
    if "channels" in values and isinstance(values["channels"], list):
        values["channels"] = tuple(values["channels"])
    return cls(**values)


def load_config(path: str | os.PathLike | None = None) -> Config:
    if path is None:
        path = os.environ.get("JOBPILOT_CONFIG", "config.toml")
    file = Path(path)
    if not file.exists():
        return Config()
    data = tomllib.loads(file.read_text())
    unknown = set(data) - set(_SECTIONS)
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")
    kwargs = {
        name: _build_section(cls, data.get(name, {}), name)
        for name, cls in _SECTIONS.items()
    }
    return Config(**kwargs)


@dataclass(frozen=True)
class DataPaths:
    """Filesystem layout under the data root. artifact.path is stored relative
    to data_root; use relative_to()/absolute() to convert."""

    data_root: Path

    @classmethod
    def from_config(cls, config: Config) -> "DataPaths":
        return cls(data_root=Path(config.paths.data_root))

    @property
    def db_path(self) -> Path:
        return self.data_root / "jobpilot.db"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_root / "browser_profile"

    @property
    def mail_dir(self) -> Path:
        return self.data_root / "mail"

    @property
    def resume_master_dir(self) -> Path:
        return self.data_root / "resume_master"

    def job_dir(self, job_id: int) -> Path:
        return self.data_root / "jobs" / str(job_id)

    def relative_to_root(self, path: Path) -> str:
        return str(path.relative_to(self.data_root))

    def absolute(self, relative: str) -> Path:
        return self.data_root / relative
