"""Command line entry point."""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import logging
import os
import shutil
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_USER,
    HOST_KEY_POLICIES,
    Config,
    ConfigError,
    build_config,
    load_config_file,
)
from .supervisor import EXIT_OK, supervise

log = logging.getLogger("port_mirror")

EXIT_USAGE = 2

EXAMPLE_CONFIG = """\
# Connection to the server that should expose the ports (server B).
host = "server-b.example.com"
user = "root"
port = 22

# With neither `password` nor `password_file` set, the password is read from
# standard input: piped in as `echo secret | port-mirror ...`, or typed at the
# prompt when port-mirror is run from a terminal.
# password_file = "/etc/port-mirror/password"

# Ports to mirror. Each entry accepts:
#   6006                          -> B:6006 serves 127.0.0.1:6006 on this host
#   "9006:6006"                   -> B:9006 serves 127.0.0.1:6006
#   "9006:127.0.0.1:5000"         -> B:9006 serves 127.0.0.1:5000
#   "0.0.0.0:9006:127.0.0.1:5000" -> per-port bind address on B
ports = [6006, 5000]

# Address the forwards bind on B. 0.0.0.0 needs `GatewayPorts yes` in B's
# sshd_config; 127.0.0.1 keeps the ports reachable only from B itself.
bind_address = "0.0.0.0"

# Liveness: a keepalive request every interval, and the link counts as dead when
# no answer arrives within the timeout.
keepalive_interval = 30
keepalive_timeout = 90
connect_timeout = 15

# auto-add records an unknown host key on first use; warn accepts and logs it;
# reject refuses to connect to an unknown server.
host_key_policy = "auto-add"
known_hosts = "~/.ssh/known_hosts"

# Probe the first mirrored port from this host once per connection and warn when
# it is not reachable, which usually means GatewayPorts is off on B.
check_reachability = true

[retry]
initial_delay = 1.0
max_delay = 60.0
multiplier = 2.0
jitter = 0.2
stable_after = 30.0
"""


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the tunnel until it is stopped."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.example_config:
        print(EXAMPLE_CONFIG, end="")
        return EXIT_OK

    _configure_logging(args.log_level)

    try:
        config = _config_from_args(args)
    except ConfigError as exc:
        parser.exit(EXIT_USAGE, f"{parser.prog}: {exc}\n")

    if args.systemd_unit:
        print(_systemd_unit(config, args.config), end="")
        return EXIT_OK

    if config.password is None:
        config = _read_password(config, parser)

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    log.info(
        "mirroring %d port(s) from this host onto %s",
        len(config.mappings),
        config.host,
    )
    return supervise(config, stop_event)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="port-mirror",
        description=(
            "Publish local ports on a remote server over a reverse SSH tunnel, "
            "reconnecting automatically whenever the link drops."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="FILE",
        help="TOML or YAML configuration file",
    )
    parser.add_argument("--host", help="remote server that will expose the ports")
    parser.add_argument(
        "--user", help=f"ssh username on the remote server (default {DEFAULT_USER})"
    )
    parser.add_argument(
        "--ssh-port", type=int, metavar="N", help="ssh port (default 22)"
    )
    parser.add_argument(
        "-p",
        "--port",
        dest="ports",
        action="append",
        metavar="SPEC",
        help=(
            "port to mirror; repeatable. Forms: PORT, REMOTE:LOCAL, "
            "REMOTE:LOCALHOST:LOCAL, BIND:REMOTE:LOCALHOST:LOCAL"
        ),
    )
    parser.add_argument(
        "--bind",
        dest="bind_address",
        metavar="ADDR",
        help="address the forwards bind on the remote server (default 0.0.0.0)",
    )
    parser.add_argument(
        "--password-file",
        metavar="FILE",
        help="file whose first line is the ssh password",
    )
    parser.add_argument(
        "--ask-password",
        action="store_true",
        help="ignore any configured password and read it from stdin",
    )
    parser.add_argument(
        "--host-key-policy",
        choices=HOST_KEY_POLICIES,
        help="what to do with an unknown remote host key (default auto-add)",
    )
    parser.add_argument("--known-hosts", metavar="FILE", help="known_hosts file to use")
    parser.add_argument(
        "--keepalive-interval",
        type=float,
        metavar="S",
        help="seconds between keepalives",
    )
    parser.add_argument(
        "--keepalive-timeout",
        type=float,
        metavar="S",
        help="seconds without a keepalive answer before the link counts as dead",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        metavar="S",
        help="ssh connection timeout in seconds",
    )
    parser.add_argument(
        "--retry-max-delay",
        type=float,
        metavar="S",
        help="upper bound on the reconnection backoff (default 60)",
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_true",
        help="exit when the tunnel drops instead of reconnecting",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="skip the reachability probe of the first mirrored port",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("PORT_MIRROR_LOG_LEVEL", "INFO"),
        metavar="LEVEL",
        help="DEBUG, INFO, WARNING or ERROR (default INFO)",
    )
    parser.add_argument(
        "--example-config",
        action="store_true",
        help="print a commented configuration file and exit",
    )
    parser.add_argument(
        "--systemd-unit",
        action="store_true",
        help="print a systemd service unit for the current settings and exit",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> Config:
    data = load_config_file(args.config) if args.config else {}
    overrides: dict[str, Any] = {
        "host": args.host,
        "user": args.user,
        "port": args.ssh_port,
        "ports": args.ports,
        "bind_address": args.bind_address,
        "password_file": args.password_file,
        "host_key_policy": args.host_key_policy,
        "known_hosts": args.known_hosts,
        "keepalive_interval": args.keepalive_interval,
        "keepalive_timeout": args.keepalive_timeout,
        "connect_timeout": args.connect_timeout,
        "retry": {"max_delay": args.retry_max_delay},
    }
    if args.no_reconnect:
        overrides["reconnect"] = False
    if args.no_check:
        overrides["check_reachability"] = False
    config = build_config(data, overrides)
    if args.ask_password:
        return _replace_password(config, None)
    return config


def _read_password(config: Config, parser: argparse.ArgumentParser) -> Config:
    """Take the ssh password from stdin, prompting when it is a terminal."""
    prompt = f"Password for {config.user}@{config.host}: "
    try:
        if sys.stdin.isatty():
            password = getpass.getpass(prompt)
        else:
            password = sys.stdin.readline().rstrip("\r\n")
    except (EOFError, KeyboardInterrupt):
        parser.exit(EXIT_USAGE, "\naborted\n")
    except OSError as exc:
        parser.exit(EXIT_USAGE, f"{parser.prog}: cannot read the password: {exc}\n")
    if not password:
        parser.exit(
            EXIT_USAGE,
            f"{parser.prog}: no password on stdin; pipe it in "
            "(echo secret | port-mirror ...) or set password_file\n",
        )
    return _replace_password(config, password)


def _replace_password(config: Config, password: str | None) -> Config:
    return dataclasses.replace(config, password=password)


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, str(level_name).upper(), None)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("paramiko").setLevel(max(level, logging.WARNING))


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(signum: int, _frame: object) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def _systemd_unit(config: Config, config_path: str | None) -> str:
    executable = shutil.which("port-mirror") or f"{sys.executable} -m port_mirror"
    command = executable
    if config_path:
        command = f"{executable} --config {Path(config_path).expanduser().resolve()}"
    user = getpass.getuser()
    password_file = "/etc/port-mirror/password"
    ports = ", ".join(str(mapping.remote_port) for mapping in config.mappings)
    return f"""\
[Unit]
Description=Mirror local ports {ports} onto {config.host}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User={user}
StandardInput=file:{password_file}
ExecStart={command}
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
"""


if __name__ == "__main__":
    sys.exit(main())
