"""Configuration loading for slaigpus.

Config is a small TOML file.  Nothing here is mandatory — every field can also
be supplied on the command line, so the tool works with no config at all.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional
from urllib.parse import urlparse

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.9/3.10
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        _toml = None  # type: ignore[assignment]


class ConfigError(RuntimeError):
    """Raised when the config file is missing, malformed, or inconsistent."""


_SECRET_CONFIG_KEYS = {
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
    "username",
}


def _reject_secret_config_keys(raw: object) -> None:
    """Keep login secrets out of the ordinary site TOML by construction."""
    if not isinstance(raw, dict):
        return
    if any(str(key).strip().lower() in _SECRET_CONFIG_KEYS for key in raw):
        raise ConfigError(
            "SenseCore credentials are not allowed in config.toml; use "
            "`slaigpus credentials set` to create the private credentials JSON"
        )


def _xdg_home(value: str, fallback: Path) -> Path:
    """Return an absolute XDG directory, ignoring invalid relative values."""
    if value:
        # XDG variables contain filesystem paths, not shell syntax.  In
        # particular, ``~`` must not be expanded behind the caller's back.
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
    return fallback


def default_state_root(
    *,
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """Return slaigpus's platform-appropriate persistent state directory.

    macOS keeps the original ``~/Library/Application Support/slaigpus`` layout.
    Other platforms follow ``XDG_STATE_HOME`` and fall back to the location
    required by the XDG base-directory specification.
    """
    selected_platform = sys.platform if platform is None else str(platform)
    selected_environ = os.environ if environ is None else environ
    selected_home = Path.home() if home is None else Path(home)
    if selected_platform == "darwin":
        return selected_home / "Library" / "Application Support" / "slaigpus"
    state_home = _xdg_home(
        str(selected_environ.get("XDG_STATE_HOME", "")),
        selected_home / ".local" / "state",
    )
    return state_home / "slaigpus"


def default_config_root(
    *,
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """Return the primary config directory without changing macOS behavior."""
    selected_platform = sys.platform if platform is None else str(platform)
    selected_environ = os.environ if environ is None else environ
    selected_home = Path.home() if home is None else Path(home)
    if selected_platform == "darwin":
        return selected_home / ".config" / "slaigpus"
    config_home = _xdg_home(
        str(selected_environ.get("XDG_CONFIG_HOME", "")),
        selected_home / ".config",
    )
    return config_home / "slaigpus"


def config_search_paths(
    *,
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    cwd: Optional[Path] = None,
) -> List[Path]:
    """Build the implicit config search list for one platform environment."""
    selected_platform = sys.platform if platform is None else str(platform)
    selected_home = Path.home() if home is None else Path(home)
    selected_cwd = Path.cwd() if cwd is None else Path(cwd)
    primary = default_config_root(
        platform=selected_platform,
        environ=environ,
        home=selected_home,
    ) / "config.toml"
    paths = [primary]
    if selected_platform == "darwin":
        # This secondary location was part of slaigpus's original macOS layout.
        paths.append(default_state_root(platform="darwin", home=selected_home) / "config.toml")
    paths.append(selected_cwd / "slaigpus.toml")
    return paths


# Backwards-compatible name used throughout the project.  On macOS it remains
# the same Application Support path; elsewhere it denotes the XDG state root.
APP_SUPPORT = default_state_root()
DEFAULT_PROFILE_ROOT = APP_SUPPORT / "profiles"

# The project has one deliberately useful zero-configuration path. SenseCore
# is reached directly unless the user explicitly selects an SSH destination in
# config.toml or on the command line.
DEFAULT_SITE_NAME = "sensecore"
DEFAULT_SSH_HOST = ""
DEFAULT_URL = "https://zhicheng.signin.sensecore.cn/"
NETWORK_MODES = frozenset({"direct", "ssh"})
SENSECORE_ACCOUNT_TYPES = frozenset({"student", "ra"})

CONFIG_SEARCH_PATH: List[Path] = config_search_paths()


@dataclass
class Site:
    """One destination and its direct or SSH-backed network policy."""

    name: str
    ssh_host: str = ""
    url: str = ""
    socks_port: int = 0          # 0 -> pick a free port automatically
    cdp_port: int = 9222         # explicit `open --cdp`; auto watcher uses port 0
    chrome_binary: str = ""      # "" -> autodetect
    profile_dir: Optional[Path] = None
    ssh_args: List[str] = field(default_factory=list)
    block_local_dns: bool = True
    chrome_args: List[str] = field(default_factory=list)
    network_mode: str = ""       # "" -> infer ssh when ssh_host is present

    # ---------------------------------------------------------------- helpers

    @property
    def probe_target(self) -> Optional["tuple[str, int]"]:
        """Host/port to use for an end-to-end network reachability check.

        Derived from ``url`` so that ``slaigpus probe`` verifies the thing you
        actually care about, not just that ssh bound a local port.
        """
        if not self.url:
            return None
        parsed = urlparse(self.url if "://" in self.url else "https://" + self.url)
        if not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.hostname, port

    def resolved_profile_dir(self) -> Path:
        """Where Chrome keeps cookies/logins for this site.

        Persistent on purpose: an automation agent that has to log in every
        run is a bad agent.  Each site gets its own directory so profiles
        never collide.
        """
        if self.profile_dir is not None:
            return Path(self.profile_dir).expanduser()
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.name)
        return DEFAULT_PROFILE_ROOT / (safe or "default")

    def resolved_automation_profile_dir(self) -> Path:
        """Persistent sibling profile reserved for the automation Chrome.

        It is intentionally never the working Chrome profile.  Authentication
        must be established in this profile explicitly; slaigpus does not copy
        cookies or any other browser data between the two directories.
        """
        work_profile = self.resolved_profile_dir()
        sibling_name = f"{work_profile.name or 'profile'}-automation"
        return work_profile.parent / sibling_name

    @property
    def mode(self) -> str:
        """Return the normalized network mode, preserving old named sites."""
        return self.network_mode or ("ssh" if self.ssh_host else "direct")

    @property
    def uses_ssh(self) -> bool:
        return self.mode == "ssh"

    def validate(self) -> None:
        if self.mode not in NETWORK_MODES:
            raise ConfigError(
                f"site '{self.name}' has invalid network mode {self.mode!r}; "
                "expected 'direct' or 'ssh'"
            )
        if self.uses_ssh:
            validate_ssh_alias(self.ssh_host)
        elif self.ssh_host:
            raise ConfigError(
                f"site '{self.name}' selects direct mode but also sets ssh_host"
            )


def validate_ssh_alias(value: str) -> str:
    """Validate one safe OpenSSH alias or ``user@host`` destination."""
    destination = str(value)
    if not destination or destination != destination.strip():
        raise ConfigError("SSH destination must not be empty or padded")
    component = r"[A-Za-z0-9][A-Za-z0-9._-]*"
    if re.fullmatch(rf"(?:{component}@)?{component}", destination) is None:
        raise ConfigError(
            "SSH destination must be a Host alias or user@host using only "
            "letters, digits, dot, underscore, or hyphen"
        )
    return destination


def default_site() -> Site:
    """Return the built-in site used by a bare ``slaigpus open``."""
    return Site(name=DEFAULT_SITE_NAME, url=DEFAULT_URL, network_mode="direct")


def is_default_sensecore_site(site: Site) -> bool:
    """True only for the managed SenseCore identity, independent of routing."""
    return (
        site.name == DEFAULT_SITE_NAME
        and site.url.rstrip("/") == DEFAULT_URL.rstrip("/")
    )


@dataclass
class Config:
    sites: Dict[str, Site] = field(default_factory=dict)
    default_site: str = ""
    sensecore: Optional[Site] = None
    sensecore_account_type: str = ""
    source: Optional[Path] = None

    def get(self, name: Optional[str]) -> Site:
        if not self.sites:
            raise ConfigError(
                "no sites configured — pass --ssh-host/--url, or create a "
                "config file (see `slaigpus list` for the search path)"
            )
        key = name or self.default_site or (
            next(iter(self.sites)) if len(self.sites) == 1 else ""
        )
        if not key:
            raise ConfigError(
                "multiple sites configured and no default — name one of: "
                + ", ".join(sorted(self.sites))
            )
        if key not in self.sites:
            raise ConfigError(
                f"unknown site '{key}' — known sites: "
                + (", ".join(sorted(self.sites)) or "(none)")
            )
        return self.sites[key]


def _coerce_site(name: str, raw: dict, defaults: dict) -> Site:
    merged = {**defaults, **raw}
    known = {f.name for f in Site.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(merged) - known - {"name", "mode"}
    if unknown:
        raise ConfigError(
            f"site '{name}': unknown option(s) {', '.join(sorted(unknown))}"
        )

    def _as_list(value, label):
        if value is None:
            return []
        if isinstance(value, str):
            raise ConfigError(f"site '{name}': {label} must be a list of strings")
        return [str(v) for v in value]

    profile = merged.get("profile_dir")
    return Site(
        name=name,
        ssh_host=str(merged.get("ssh_host", "")),
        url=str(merged.get("url", "")),
        socks_port=int(merged.get("socks_port", 0)),
        cdp_port=int(merged.get("cdp_port", 9222)),
        chrome_binary=str(merged.get("chrome_binary", "")),
        profile_dir=Path(str(profile)).expanduser() if profile else None,
        ssh_args=_as_list(merged.get("ssh_args"), "ssh_args"),
        block_local_dns=bool(merged.get("block_local_dns", True)),
        chrome_args=_as_list(merged.get("chrome_args"), "chrome_args"),
        network_mode=str(merged.get("mode", merged.get("network_mode", ""))),
    )


def _coerce_sensecore(raw: object, *, path: Path) -> tuple[Site, str]:
    """Read the allowlisted account and network settings from [sensecore]."""
    if raw is None:
        return default_site(), ""
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: [sensecore] must be a table")
    _reject_secret_config_keys(raw)
    unknown = set(raw) - {"account_type", "network"}
    if unknown:
        raise ConfigError(
            f"{path}: unknown [sensecore] option(s) "
            + ", ".join(sorted(str(value) for value in unknown))
        )
    account_type = str(raw.get("account_type", "") or "").strip().lower()
    if account_type and account_type not in SENSECORE_ACCOUNT_TYPES:
        raise ConfigError(
            f"{path}: [sensecore].account_type must be 'student' or 'ra'"
        )
    network = raw.get("network", {}) or {}
    if not isinstance(network, dict):
        raise ConfigError(f"{path}: [sensecore.network] must be a table")
    _reject_secret_config_keys(network)
    unknown_network = set(network) - {"mode", "ssh_host"}
    if unknown_network:
        raise ConfigError(
            f"{path}: unknown [sensecore.network] option(s) "
            + ", ".join(sorted(str(value) for value in unknown_network))
        )
    ssh_host = str(network.get("ssh_host", "") or "")
    mode = str(network.get("mode", "") or ("ssh" if ssh_host else "direct"))
    site = Site(
        name=DEFAULT_SITE_NAME,
        url=DEFAULT_URL,
        ssh_host=ssh_host,
        network_mode=mode,
    )
    site.validate()
    return site, account_type


_TOML_TABLE_HEADER = re.compile(r"^\s*\[\[?.*?\]\]?\s*(?:#.*)?(?:\r?\n)?$")
_SENSECORE_TABLE_HEADER = re.compile(
    r"^\s*\[\s*sensecore\s*\]\s*(?:#.*)?(?:\r?\n)?$"
)
_SENSECORE_NETWORK_TABLE_HEADER = re.compile(
    r"^\s*\[\s*sensecore\s*\.\s*network\s*\]\s*(?:#.*)?(?:\r?\n)?$"
)


def config_path_for_write(explicit: Optional[Path] = None) -> Path:
    """Choose the durable config file edited by the guided assistant."""
    if explicit is not None:
        return Path(explicit).expanduser()
    configured = os.environ.get("SLAIGPUS_CONFIG", "")
    if configured:
        return Path(configured).expanduser()
    return CONFIG_SEARCH_PATH[0]


def update_sensecore_config(
    path: Path,
    *,
    account_type: str,
    network_mode: str,
    ssh_host: str = "",
) -> Path:
    """Atomically update only the managed SenseCore TOML tables.

    Other tables and comments are retained. Existing unusual inline or quoted
    encodings of ``sensecore`` are rejected instead of risking a lossy rewrite.
    """
    selected = Path(path).expanduser()
    normalized_account_type = str(account_type).strip().lower()
    if normalized_account_type not in SENSECORE_ACCOUNT_TYPES:
        raise ConfigError("SenseCore account type must be 'student' or 'ra'")
    normalized_mode = str(network_mode).strip().lower()
    if normalized_mode not in NETWORK_MODES:
        raise ConfigError("SenseCore network mode must be 'direct' or 'ssh'")
    normalized_host = str(ssh_host).strip()
    if normalized_mode == "ssh":
        normalized_host = validate_ssh_alias(normalized_host)
    elif normalized_host:
        raise ConfigError("direct SenseCore mode must not set an SSH destination")

    original = ""
    had_sensecore = False
    if selected.exists() or selected.is_symlink():
        try:
            details = selected.lstat()
        except OSError as exc:
            raise ConfigError(f"could not inspect config file: {selected}") from exc
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ConfigError(f"config file must be a regular single-link file: {selected}")
        # Validate the complete current document before attempting a surgical
        # update, including the project's allowlists for site configuration.
        current = load_config(selected)
        had_sensecore = current.sensecore is not None
        try:
            original = selected.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"could not read config file: {selected}") from exc
        if "'''" in original or '\"\"\"' in original:
            raise ConfigError(
                "existing config contains multiline TOML strings; refusing a "
                "potentially lossy automatic rewrite"
            )

    kept: List[str] = []
    removing = False
    recognized = False
    for line in original.splitlines(keepends=True):
        if _SENSECORE_TABLE_HEADER.fullmatch(line) or (
            _SENSECORE_NETWORK_TABLE_HEADER.fullmatch(line)
        ):
            recognized = True
            removing = True
            continue
        if _TOML_TABLE_HEADER.fullmatch(line):
            removing = False
        if not removing:
            kept.append(line)
    if had_sensecore and not recognized:
        raise ConfigError(
            "existing [sensecore] settings use an unsupported TOML layout; "
            "rewrite them as [sensecore] and [sensecore.network] tables first"
        )

    prefix = "".join(kept).rstrip("\r\n")
    managed = (
        "[sensecore]\n"
        f'account_type = "{normalized_account_type}"\n\n'
        "[sensecore.network]\n"
        f'mode = "{normalized_mode}"\n'
    )
    if normalized_mode == "ssh":
        managed += f'ssh_host = "{normalized_host}"\n'
    document = (prefix + "\n\n" if prefix else "") + managed

    if _toml is None:
        raise ConfigError(
            "writing TOML requires Python 3.11+ or the 'tomli' package "
            "(pip install tomli)"
        )
    try:
        _toml.loads(document)
    except Exception as exc:  # pragma: no cover - values are tightly constrained
        raise ConfigError(f"could not construct config file: {exc}") from exc

    parent = selected.parent
    temporary = ""
    descriptor = -1
    try:
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{selected.name}.tmp-",
            dir=str(parent),
        )
        os.fchmod(descriptor, 0o600)
        payload = document.encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short config write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, selected)
        temporary = ""
    except OSError as exc:
        raise ConfigError(f"could not write config file: {selected}") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return selected


def find_config(explicit: Optional[Path] = None) -> Optional[Path]:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    env = os.environ.get("SLAIGPUS_CONFIG")
    if env:
        path = Path(env).expanduser()
        if not path.is_file():
            raise ConfigError(f"SLAIGPUS_CONFIG points at a missing file: {path}")
        return path
    for candidate in CONFIG_SEARCH_PATH:
        if candidate.is_file():
            return candidate
    return None


def load_config(explicit: Optional[Path] = None) -> Config:
    path = find_config(explicit)
    if path is None:
        return Config()
    if _toml is None:
        raise ConfigError(
            "reading TOML requires Python 3.11+ or the 'tomli' package "
            "(pip install tomli)"
        )
    try:
        with open(path, "rb") as fh:
            data = _toml.load(fh)
    except Exception as exc:  # noqa: BLE001 - surface the parser message as-is
        raise ConfigError(f"could not parse {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a table")
    _reject_secret_config_keys(data)
    unknown_top_level = set(data) - {"defaults", "sites", "sensecore"}
    if unknown_top_level:
        raise ConfigError(
            f"{path}: unknown top-level option(s) "
            + ", ".join(sorted(str(value) for value in unknown_top_level))
        )

    defaults = data.get("defaults", {}) or {}
    if not isinstance(defaults, dict):
        raise ConfigError(f"{path}: [defaults] must be a table")
    _reject_secret_config_keys(defaults)
    default_site = str(defaults.pop("site", "") or "")

    raw_sites = data.get("sites", {}) or {}
    if not isinstance(raw_sites, dict):
        raise ConfigError(f"{path}: [sites] must be a table")

    sites = {}
    for name, raw in raw_sites.items():
        selected = raw or {}
        if not isinstance(selected, dict):
            raise ConfigError(f"{path}: [sites.{name}] must be a table")
        _reject_secret_config_keys(selected)
        site = _coerce_site(name, selected, defaults)
        site.validate()
        sites[name] = site
    sensecore: Optional[Site] = None
    sensecore_account_type = ""
    if "sensecore" in data:
        sensecore, sensecore_account_type = _coerce_sensecore(
            data.get("sensecore"), path=path
        )
    return Config(
        sites=sites,
        default_site=default_site,
        sensecore=sensecore,
        sensecore_account_type=sensecore_account_type,
        source=path,
    )


def apply_overrides(site: Site, **overrides) -> Site:
    """Return a copy of *site* with non-empty CLI overrides applied."""
    clean = {k: v for k, v in overrides.items() if v not in (None, "", 0)}
    return replace(site, **clean) if clean else site
