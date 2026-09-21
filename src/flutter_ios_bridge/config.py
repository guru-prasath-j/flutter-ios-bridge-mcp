"""Configuration: environment variables override an optional TOML config file.

Config file location:
  Windows : %APPDATA%\\flutter-ios-bridge\\config.toml
  others  : $XDG_CONFIG_HOME/flutter-ios-bridge/config.toml (default ~/.config/...)
Override with FIB_CONFIG=/path/to/config.toml
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "flutter-ios-bridge"


def config_path() -> Path:
    return Path(os.environ["FIB_CONFIG"]) if os.environ.get("FIB_CONFIG") else config_dir() / "config.toml"


def data_dir() -> Path:
    d = Path(os.environ.get("FIB_DATA_DIR", Path.home() / ".flutter-ios-bridge"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class GitHubConfig:
    token: Optional[str] = None
    repo: Optional[str] = None  # "owner/name"
    workflow: str = "flutter-ios-bridge.yml"
    ref: Optional[str] = None


@dataclass
class ASCConfig:
    key_id: Optional[str] = None
    issuer_id: Optional[str] = None
    private_key_path: Optional[str] = None
    private_key: Optional[str] = None  # PEM contents, alternative to the path

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.issuer_id and (self.private_key_path or self.private_key))


@dataclass
class MacConfig:
    host: Optional[str] = None
    user: Optional[str] = None
    port: int = 22
    key_path: Optional[str] = None
    password: Optional[str] = None
    remote_dir: str = "~/flutter-ios-bridge"
    shell: str = "zsh"  # login shell used so flutter/xcrun are on PATH

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user)


@dataclass
class Config:
    project_path: Optional[str] = None
    github: GitHubConfig = field(default_factory=GitHubConfig)
    asc: ASCConfig = field(default_factory=ASCConfig)
    mac: MacConfig = field(default_factory=MacConfig)
    source: str = "defaults"


def _env(name: str) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else None


def _gh_cli_token() -> Optional[str]:
    if not shutil.which("gh"):
        return None
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        tok = out.stdout.strip()
        return tok or None
    except Exception:
        return None


def load_config() -> Config:
    cfg = Config()
    raw: dict[str, Any] = {}
    p = config_path()
    if p.exists():
        with open(p, "rb") as fh:
            raw = tomllib.load(fh)
        cfg.source = str(p)

    proj = raw.get("project", {})
    gh = raw.get("github", {})
    asc = raw.get("asc", {})
    mac = raw.get("mac", {})

    cfg.project_path = _env("FIB_PROJECT_PATH") or proj.get("path")

    cfg.github.token = _env("GITHUB_TOKEN") or _env("GH_TOKEN") or gh.get("token")
    cfg.github.repo = _env("FIB_GITHUB_REPO") or gh.get("repo")
    cfg.github.workflow = _env("FIB_GITHUB_WORKFLOW") or gh.get("workflow") or cfg.github.workflow
    cfg.github.ref = _env("FIB_GITHUB_REF") or gh.get("ref")

    cfg.asc.key_id = _env("ASC_KEY_ID") or asc.get("key_id")
    cfg.asc.issuer_id = _env("ASC_ISSUER_ID") or asc.get("issuer_id")
    cfg.asc.private_key_path = _env("ASC_PRIVATE_KEY_PATH") or asc.get("private_key_path")
    cfg.asc.private_key = _env("ASC_PRIVATE_KEY") or asc.get("private_key")

    cfg.mac.host = _env("FIB_MAC_HOST") or mac.get("host")
    cfg.mac.user = _env("FIB_MAC_USER") or mac.get("user")
    cfg.mac.port = int(_env("FIB_MAC_PORT") or mac.get("port") or 22)
    cfg.mac.key_path = _env("FIB_MAC_KEY_PATH") or mac.get("key_path")
    cfg.mac.password = _env("FIB_MAC_PASSWORD") or mac.get("password")
    cfg.mac.remote_dir = _env("FIB_MAC_REMOTE_DIR") or mac.get("remote_dir") or cfg.mac.remote_dir
    cfg.mac.shell = _env("FIB_MAC_SHELL") or mac.get("shell") or cfg.mac.shell
    return cfg


def github_token(cfg: Config) -> Optional[str]:
    return cfg.github.token or _gh_cli_token()


CONFIG_TEMPLATE = """\
# flutter-ios-bridge configuration. Every value can also be set by environment variable
# (shown in brackets). Delete the sections you don't use.

[project]
# Default Flutter project used when a tool call doesn't pass project_path  [FIB_PROJECT_PATH]
# path = "C:/Users/me/code/my_app"

[github]
# Fine-grained PAT with Actions: read/write, Secrets: read/write, Contents: read/write.
# If omitted, `gh auth token` is used when the GitHub CLI is installed.   [GITHUB_TOKEN]
# token = "github_pat_..."
# "owner/repo". If omitted it is read from the project's git remote.        [FIB_GITHUB_REPO]
# repo = "me/my_app"
# workflow = "flutter-ios-bridge.yml"                                       [FIB_GITHUB_WORKFLOW]

[asc]
# App Store Connect API key (Users and Access -> Integrations -> App Store Connect API).
# key_id = "ABC123XYZ"                                                      [ASC_KEY_ID]
# issuer_id = "00000000-0000-0000-0000-000000000000"                        [ASC_ISSUER_ID]
# private_key_path = "C:/keys/AuthKey_ABC123XYZ.p8"                         [ASC_PRIVATE_KEY_PATH]

[mac]
# Optional: any Mac you can SSH into (a friend's, a Mac mini, MacinCloud, AWS EC2 Mac...)
# host = "192.168.1.20"                                                     [FIB_MAC_HOST]
# user = "builder"                                                          [FIB_MAC_USER]
# port = 22                                                                 [FIB_MAC_PORT]
# key_path = "~/.ssh/id_ed25519"                                            [FIB_MAC_KEY_PATH]
# password = ""                                                             [FIB_MAC_PASSWORD]
# remote_dir = "~/flutter-ios-bridge"                                       [FIB_MAC_REMOTE_DIR]
# shell = "zsh"                                                             [FIB_MAC_SHELL]
"""
