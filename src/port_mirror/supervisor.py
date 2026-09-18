"""Keeps a tunnel alive across failures until it is asked to stop."""

from __future__ import annotations

import logging
import random
import threading
import time

import paramiko

from .config import Config
from .tunnel import Tunnel, TunnelError

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1


class FatalTunnelError(RuntimeError):
    """A failure that retrying cannot fix, such as a rejected credential."""


def supervise(config: Config, stop_event: threading.Event) -> int:
    """Run the tunnel, reconnecting with backoff until `stop_event` is set.

    Returns a process exit status: zero for a requested stop, non-zero when the
    tunnel gave up.
    """
    retry = config.retry
    delay = retry.initial_delay

    while not stop_event.is_set():
        started = time.monotonic()
        try:
            Tunnel(config, stop_event).run()
        except (paramiko.AuthenticationException, paramiko.BadHostKeyException) as exc:
            log.error("%s", _describe_fatal(exc))
            return EXIT_FAILED
        except (TunnelError, paramiko.SSHException, OSError) as exc:
            reason = str(exc) or exc.__class__.__name__
        else:
            reason = "ssh session ended"

        if stop_event.is_set():
            break

        uptime = time.monotonic() - started
        if uptime >= retry.stable_after:
            delay = retry.initial_delay

        if not config.reconnect:
            log.error("tunnel lost: %s", reason)
            return EXIT_FAILED

        wait_for = _with_jitter(delay, retry.jitter)
        log.warning(
            "tunnel lost after %.0fs: %s; reconnecting in %.1fs",
            uptime,
            reason,
            wait_for,
        )
        stop_event.wait(wait_for)
        delay = min(delay * retry.multiplier, retry.max_delay)

    log.info("stopped")
    return EXIT_OK


def _with_jitter(delay: float, jitter: float) -> float:
    if jitter <= 0:
        return delay
    spread = delay * jitter
    return max(0.1, delay + random.uniform(-spread, spread))


def _describe_fatal(exc: Exception) -> str:
    if isinstance(exc, paramiko.BadHostKeyException):
        return (
            f"host key for the remote server changed; remove the stale entry from "
            f"known_hosts or set host_key_policy if this is expected ({exc})"
        )
    return f"authentication failed: {exc}"
