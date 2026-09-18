"""Configuration model, file loading and validation."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SSH_PORT = 22
DEFAULT_LOCAL_HOST = "127.0.0.1"
DEFAULT_BIND_ADDRESS = "0.0.0.0"
DEFAULT_USER = "root"
DEFAULT_KNOWN_HOSTS = "~/.ssh/known_hosts"

HOST_KEY_POLICIES = ("auto-add", "warn", "reject")

_KNOWN_KEYS = frozenset(
    {
        "host",
        "user",
        "port",
        "password",
        "password_file",
        "ports",
        "bind_address",
        "keepalive_interval",
        "keepalive_timeout",
        "connect_timeout",
        "local_connect_timeout",
        "host_key_policy",
        "known_hosts",
        "check_reachability",
        "reconnect",
        "retry",
    }
)
_KNOWN_RETRY_KEYS = frozenset(
    {"initial_delay", "max_delay", "multiplier", "jitter", "stable_after"}
)


class ConfigError(ValueError):
    """Configuration that cannot be turned into a runnable tunnel."""


@dataclass(frozen=True, slots=True)
class PortMapping:
    """One remote listening port and the local service it is fed from."""

    remote_port: int
    local_port: int
    local_host: str = DEFAULT_LOCAL_HOST
    bind_address: str | None = None

    def describe(self, default_bind: str) -> str:
        bind = self.bind_address or default_bind
        return f"{bind}:{self.remote_port} -> {self.local_host}:{self.local_port}"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff applied between reconnection attempts."""

    initial_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.2
    stable_after: float = 30.0


@dataclass(frozen=True, slots=True)
class Config:
    """Everything needed to publish a set of local ports on the remote host."""

    host: str
    mappings: tuple[PortMapping, ...]
    user: str = DEFAULT_USER
    password: str | None = None
    ssh_port: int = DEFAULT_SSH_PORT
    bind_address: str = DEFAULT_BIND_ADDRESS
    keepalive_interval: float = 30.0
    keepalive_timeout: float = 90.0
    connect_timeout: float = 15.0
    local_connect_timeout: float = 10.0
    host_key_policy: str = "auto-add"
    known_hosts: str | None = DEFAULT_KNOWN_HOSTS
    check_reachability: bool = True
    reconnect: bool = True
    retry: RetryPolicy = field(default_factory=RetryPolicy)


def parse_port_spec(spec: Any) -> PortMapping:
    """Turn one entry of the ``ports`` list into a `PortMapping`.

    Accepted forms mirror ``ssh -R``: ``6006``, ``"9006:6006"``,
    ``"9006:127.0.0.1:5000"``, ``"0.0.0.0:9006:127.0.0.1:5000"`` and the
    explicit table ``{remote = 9006, local_host = "127.0.0.1", local = 5000}``.
    """
    if isinstance(spec, Mapping):
        return _mapping_from_table(spec)
    if isinstance(spec, bool):
        raise ConfigError(f"invalid port entry: {spec!r}")
    if isinstance(spec, int):
        return PortMapping(
            remote_port=_port(spec, "port"), local_port=_port(spec, "port")
        )
    if isinstance(spec, str):
        return _mapping_from_string(spec)
    raise ConfigError(f"invalid port entry: {spec!r}")


def load_config_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a TOML or YAML configuration file into a plain dictionary."""
    file_path = Path(path).expanduser()
    suffix = file_path.suffix.lower()
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {file_path}: {exc}") from exc

    if suffix in (".yaml", ".yml"):
        import yaml

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {file_path}: {exc}") from exc
    else:
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"invalid TOML in {file_path}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"{file_path} must contain a table of settings")
    return dict(data)


def build_config(
    data: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Merge file settings with command line overrides and validate the result."""
    merged = _merge(data or {}, overrides or {})

    unknown = set(merged) - _KNOWN_KEYS
    if unknown:
        raise ConfigError("unknown setting(s): " + ", ".join(sorted(unknown)))

    host = _require_text(merged, "host")
    mappings = _parse_ports(merged.get("ports"))

    policy = str(merged.get("host_key_policy", "auto-add")).lower()
    if policy not in HOST_KEY_POLICIES:
        raise ConfigError(
            f"host_key_policy must be one of {', '.join(HOST_KEY_POLICIES)}, got {policy!r}"
        )

    known_hosts = merged.get("known_hosts", DEFAULT_KNOWN_HOSTS)

    return Config(
        host=host,
        user=_optional_text(merged, "user") or DEFAULT_USER,
        mappings=mappings,
        password=_resolve_password(merged),
        ssh_port=_port(merged.get("port", DEFAULT_SSH_PORT), "port"),
        bind_address=str(merged.get("bind_address", DEFAULT_BIND_ADDRESS)),
        keepalive_interval=_positive(
            merged.get("keepalive_interval", 30.0), "keepalive_interval"
        ),
        keepalive_timeout=_positive(
            merged.get("keepalive_timeout", 90.0), "keepalive_timeout"
        ),
        connect_timeout=_positive(
            merged.get("connect_timeout", 15.0), "connect_timeout"
        ),
        local_connect_timeout=_positive(
            merged.get("local_connect_timeout", 10.0), "local_connect_timeout"
        ),
        host_key_policy=policy,
        known_hosts=None if known_hosts in (None, "") else str(known_hosts),
        check_reachability=_flag(
            merged.get("check_reachability", True), "check_reachability"
        ),
        reconnect=_flag(merged.get("reconnect", True), "reconnect"),
        retry=_build_retry(merged.get("retry")),
    )


def _merge(data: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(data)
    for key, value in overrides.items():
        if value is None:
            continue
        if key == "retry" and isinstance(value, Mapping):
            base = merged.get("retry")
            nested = dict(base) if isinstance(base, Mapping) else {}
            nested.update({k: v for k, v in value.items() if v is not None})
            merged["retry"] = nested
        else:
            merged[key] = value
    return merged


def _build_retry(value: Any) -> RetryPolicy:
    if value is None:
        return RetryPolicy()
    if not isinstance(value, Mapping):
        raise ConfigError("retry must be a table of backoff settings")
    unknown = set(value) - _KNOWN_RETRY_KEYS
    if unknown:
        raise ConfigError("unknown retry setting(s): " + ", ".join(sorted(unknown)))

    defaults = RetryPolicy()
    initial = _positive(
        value.get("initial_delay", defaults.initial_delay), "retry.initial_delay"
    )
    maximum = _positive(value.get("max_delay", defaults.max_delay), "retry.max_delay")
    if maximum < initial:
        raise ConfigError(
            "retry.max_delay must be greater than or equal to retry.initial_delay"
        )
    multiplier = _number(
        value.get("multiplier", defaults.multiplier), "retry.multiplier"
    )
    if multiplier < 1.0:
        raise ConfigError("retry.multiplier must be at least 1.0")
    jitter = _number(value.get("jitter", defaults.jitter), "retry.jitter")
    if not 0.0 <= jitter < 1.0:
        raise ConfigError("retry.jitter must be in [0.0, 1.0)")
    stable_after = _number(
        value.get("stable_after", defaults.stable_after), "retry.stable_after"
    )
    if stable_after < 0:
        raise ConfigError("retry.stable_after must not be negative")

    return RetryPolicy(
        initial_delay=initial,
        max_delay=maximum,
        multiplier=multiplier,
        jitter=jitter,
        stable_after=stable_after,
    )


def _parse_ports(value: Any) -> tuple[PortMapping, ...]:
    if value is None:
        raise ConfigError("no ports configured; set 'ports' or pass --port")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        value = [value]
    mappings = tuple(parse_port_spec(entry) for entry in value)
    if not mappings:
        raise ConfigError("no ports configured; set 'ports' or pass --port")

    seen: dict[tuple[str | None, int], PortMapping] = {}
    for mapping in mappings:
        key = (mapping.bind_address, mapping.remote_port)
        if key in seen:
            raise ConfigError(
                f"remote port {mapping.remote_port} is mapped more than once"
            )
        seen[key] = mapping
    return mappings


def _mapping_from_table(table: Mapping[str, Any]) -> PortMapping:
    known = {
        "remote",
        "remote_port",
        "local",
        "local_port",
        "local_host",
        "bind_address",
    }
    unknown = set(table) - known
    if unknown:
        raise ConfigError("unknown port setting(s): " + ", ".join(sorted(unknown)))

    remote = table.get("remote", table.get("remote_port"))
    if remote is None:
        raise ConfigError(f"port entry {dict(table)!r} is missing 'remote'")
    remote_port = _port(remote, "remote")
    local = table.get("local", table.get("local_port", remote_port))
    bind_address = table.get("bind_address")
    return PortMapping(
        remote_port=remote_port,
        local_port=_port(local, "local"),
        local_host=str(table.get("local_host", DEFAULT_LOCAL_HOST)),
        bind_address=None if bind_address is None else str(bind_address),
    )


def _mapping_from_string(spec: str) -> PortMapping:
    parts = [part.strip() for part in spec.split(":")]
    bind: str | None = None
    local_host = DEFAULT_LOCAL_HOST

    if len(parts) == 1:
        remote = local = parts[0]
    elif len(parts) == 2:
        remote, local = parts
    elif len(parts) == 3:
        remote, local_host, local = parts
    elif len(parts) == 4:
        bind, remote, local_host, local = parts
    else:
        raise ConfigError(f"invalid port spec {spec!r}")

    return PortMapping(
        remote_port=_port(remote, "remote port"),
        local_port=_port(local, "local port"),
        local_host=local_host or DEFAULT_LOCAL_HOST,
        bind_address=bind or None,
    )


def _resolve_password(merged: Mapping[str, Any]) -> str | None:
    """Return the configured password, or None when it has to come from stdin."""
    explicit = merged.get("password")
    if explicit:
        return str(explicit)

    password_file = merged.get("password_file")
    if password_file:
        path = Path(str(password_file)).expanduser()
        try:
            return path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise ConfigError(f"cannot read password_file {path}: {exc}") from exc

    return None


def _optional_text(merged: Mapping[str, Any], key: str) -> str | None:
    value = merged.get(key)
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _require_text(merged: Mapping[str, Any], key: str) -> str:
    value = merged.get(key)
    if value is None or str(value).strip() == "":
        raise ConfigError(
            f"'{key}' is required; set it in the config file or pass --{key}"
        )
    return str(value).strip()


def _port(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be an integer, got {value!r}")
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{label} must be an integer, got {value!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"{label} must be between 1 and 65535, got {port}")
    return port


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be a number, got {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{label} must be a number, got {value!r}") from None


def _positive(value: Any, label: str) -> float:
    number = _number(value, label)
    if number <= 0:
        raise ConfigError(f"{label} must be greater than zero, got {number}")
    return number


def _flag(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    raise ConfigError(f"{label} must be true or false, got {value!r}")
