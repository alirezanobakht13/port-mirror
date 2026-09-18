"""A reverse SSH tunnel that republishes local ports on the remote host."""

from __future__ import annotations

import logging
import select
import socket
import threading
import time
from pathlib import Path

import paramiko

from .config import Config, PortMapping

log = logging.getLogger(__name__)

BUFFER_SIZE = 32768
POLL_INTERVAL = 1.0

_POLICIES: dict[str, type[paramiko.MissingHostKeyPolicy]] = {
    "auto-add": paramiko.AutoAddPolicy,
    "warn": paramiko.WarningPolicy,
    "reject": paramiko.RejectPolicy,
}


class TunnelError(RuntimeError):
    """The tunnel could not be established or stopped behaving."""


class Tunnel:
    """One SSH session publishing every configured port on the remote host.

    A single instance covers a single connection attempt: `run` returns when the
    link drops or the stop event is set, and reconnection is the supervisor's job.
    """

    def __init__(self, config: Config, stop_event: threading.Event) -> None:
        self._config = config
        self._stop = stop_event
        self._client: paramiko.SSHClient | None = None
        self._transport: paramiko.Transport | None = None
        self._by_remote_port = {
            mapping.remote_port: mapping for mapping in config.mappings
        }
        self._last_response = 0.0
        self._keepalive: threading.Thread | None = None
        self._open_connections = 0
        self._counter_lock = threading.Lock()

    def run(self) -> None:
        """Connect, publish the forwards, and block until the link ends."""
        self._connect()
        try:
            self._request_forwards()
            self._start_keepalive()
            if self._config.check_reachability:
                self._start_reachability_check()
            self._watch()
        finally:
            self._shutdown()

    def _connect(self) -> None:
        config = self._config
        client = paramiko.SSHClient()
        self._load_host_keys(client)
        client.set_missing_host_key_policy(_POLICIES[config.host_key_policy]())

        log.info("connecting to %s@%s:%d", config.user, config.host, config.ssh_port)
        client.connect(
            hostname=config.host,
            port=config.ssh_port,
            username=config.user,
            password=config.password,
            timeout=config.connect_timeout,
            banner_timeout=config.connect_timeout,
            auth_timeout=config.connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )

        transport = client.get_transport()
        if transport is None:
            client.close()
            raise TunnelError("ssh transport was not established")

        try:
            transport.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            log.debug("could not enable TCP keepalive on the ssh socket", exc_info=True)

        self._client = client
        self._transport = transport
        self._last_response = time.monotonic()
        log.info("connected to %s", config.host)

    def _load_host_keys(self, client: paramiko.SSHClient) -> None:
        known_hosts = self._config.known_hosts
        if known_hosts is None:
            return
        path = Path(known_hosts).expanduser()
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(mode=0o600)
            except OSError as exc:
                log.warning("cannot create known_hosts file %s: %s", path, exc)
                return
        try:
            client.load_host_keys(str(path))
        except OSError as exc:
            log.warning("cannot read known_hosts file %s: %s", path, exc)

    def _request_forwards(self) -> None:
        transport = self._require_transport()
        config = self._config
        for mapping in config.mappings:
            bind = mapping.bind_address or config.bind_address
            try:
                transport.request_port_forward(
                    bind, mapping.remote_port, self._handle_channel
                )
            except paramiko.SSHException as exc:
                raise TunnelError(
                    f"{config.host} refused to listen on {bind}:{mapping.remote_port} ({exc}); "
                    "the port may still be held by a previous session"
                ) from exc
            log.info("mirroring %s", mapping.describe(config.bind_address))

    def _start_keepalive(self) -> None:
        self._keepalive = threading.Thread(
            target=self._keepalive_loop, name="port-mirror-keepalive", daemon=True
        )
        self._keepalive.start()

    def _keepalive_loop(self) -> None:
        transport = self._require_transport()
        interval = self._config.keepalive_interval
        while not self._stop.is_set() and transport.is_active():
            transport.global_request("keepalive@openssh.com", wait=True)
            if not transport.is_active():
                break
            self._last_response = time.monotonic()
            self._stop.wait(interval)

    def _start_reachability_check(self) -> None:
        thread = threading.Thread(
            target=self._check_reachable, name="port-mirror-reachability", daemon=True
        )
        thread.start()

    def _check_reachable(self) -> None:
        """Probe one published port from this host to confirm it is externally bound."""
        config = self._config
        mapping = config.mappings[0]
        bind = mapping.bind_address or config.bind_address
        if bind in ("127.0.0.1", "localhost", "::1"):
            return
        try:
            with socket.create_connection(
                (config.host, mapping.remote_port), timeout=config.connect_timeout
            ):
                pass
        except OSError as exc:
            log.warning(
                "%s:%d is not reachable from here (%s); if remote clients cannot connect, "
                "check that 'GatewayPorts yes' is set in sshd_config on %s and that a "
                "firewall is not blocking the port",
                config.host,
                mapping.remote_port,
                exc,
                config.host,
            )
        else:
            log.info(
                "verified %s:%d accepts connections", config.host, mapping.remote_port
            )

    def _handle_channel(
        self,
        channel: paramiko.Channel,
        origin: tuple[str, int],
        server: tuple[str, int],
    ) -> None:
        """Hand a newly forwarded connection to a worker thread.

        Paramiko invokes this from the transport's packet loop, so it has to
        return at once: anything blocking here freezes the whole ssh session,
        including the other forwards and the keepalives.
        """
        mapping = self._by_remote_port.get(server[1])
        if mapping is None:
            log.warning("dropping forwarded connection for unmapped port %d", server[1])
            _close_quietly(channel)
            return
        threading.Thread(
            target=self._serve,
            args=(channel, origin, mapping),
            name=f"port-mirror-{mapping.remote_port}",
            daemon=True,
        ).start()

    def _serve(
        self,
        channel: paramiko.Channel,
        origin: tuple[str, int],
        mapping: PortMapping,
    ) -> None:
        target = (mapping.local_host, mapping.local_port)
        try:
            sock = socket.create_connection(
                target, timeout=self._config.local_connect_timeout
            )
        except OSError as exc:
            log.warning(
                "no local service at %s:%d for remote port %d (%s)",
                mapping.local_host,
                mapping.local_port,
                mapping.remote_port,
                exc,
            )
            _close_quietly(channel)
            return

        count = self._track(1)
        log.debug(
            "forwarding %s:%d -> %s:%d (%d open)",
            origin[0],
            origin[1],
            mapping.local_host,
            mapping.local_port,
            count,
        )
        try:
            _pump(channel, sock, self._stop)
        except Exception:
            log.exception(
                "forwarded connection for port %d failed", mapping.remote_port
            )
        finally:
            _close_quietly(sock)
            _close_quietly(channel)
            self._track(-1)

    def _track(self, delta: int) -> int:
        with self._counter_lock:
            self._open_connections += delta
            return self._open_connections

    def _watch(self) -> None:
        transport = self._require_transport()
        timeout = self._config.keepalive_timeout
        while not self._stop.is_set():
            if not transport.is_active():
                raise TunnelError("ssh connection closed")
            silent_for = time.monotonic() - self._last_response
            if silent_for > timeout:
                raise TunnelError(f"no ssh keepalive response for {silent_for:.0f}s")
            self._stop.wait(POLL_INTERVAL)

    def _shutdown(self) -> None:
        keepalive = self._keepalive
        self._keepalive = None
        if self._transport is not None:
            _close_quietly(self._transport)
            self._transport = None
        if self._client is not None:
            _close_quietly(self._client)
            self._client = None
        if keepalive is not None:
            keepalive.join(timeout=POLL_INTERVAL)

    def _require_transport(self) -> paramiko.Transport:
        if self._transport is None:
            raise TunnelError("ssh transport is not connected")
        return self._transport


def _pump(
    channel: paramiko.Channel, sock: socket.socket, stop: threading.Event
) -> None:
    """Copy bytes in both directions until either side closes or a stop is requested."""
    channel.setblocking(True)
    sock.setblocking(True)
    while not stop.is_set():
        try:
            readable, _, _ = select.select([sock, channel], [], [], POLL_INTERVAL)
        except (OSError, ValueError):
            return
        try:
            if sock in readable:
                data = sock.recv(BUFFER_SIZE)
                if not data:
                    return
                channel.sendall(data)
            if channel in readable:
                data = channel.recv(BUFFER_SIZE)
                if not data:
                    return
                sock.sendall(data)
        except (OSError, EOFError, paramiko.SSHException):
            return


def _close_quietly(resource: object) -> None:
    close = getattr(resource, "close", None)
    if close is None:
        return
    try:
        close()
    except (OSError, EOFError, paramiko.SSHException):
        pass
