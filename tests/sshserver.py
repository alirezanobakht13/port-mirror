"""An in-process SSH server with reverse forwarding, for exercising the tunnel."""

from __future__ import annotations

import select
import socket
import threading

import paramiko

BUFFER_SIZE = 32768
TICK = 0.2


def free_port() -> int:
    """Return a port that was free a moment ago."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class EchoServer:
    """A TCP service that returns whatever is sent to it."""

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = int(self._listener.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> EchoServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=2)

    def _accept_loop(self) -> None:
        self._listener.settimeout(TICK)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn: socket.socket) -> None:
        with conn:
            while not self._stop.is_set():
                try:
                    data = conn.recv(BUFFER_SIZE)
                except OSError:
                    return
                if not data:
                    return
                conn.sendall(data)


class FakeSSHServer:
    """Accepts password logins and honours ``tcpip-forward`` requests."""

    def __init__(self, username: str = "tester", password: str = "s3cr3t") -> None:
        self.username = username
        self.password = password
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sessions = 0
        self.failed_logins = 0
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = int(self._listener.getsockname()[1])
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._transports: list[paramiko.Transport] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> FakeSSHServer:
        self._thread.start()
        return self

    def drop_sessions(self) -> None:
        """Cut every live session, as a network failure would."""
        with self._lock:
            transports = list(self._transports)
            self._transports.clear()
        for transport in transports:
            transport.close()

    def stop(self) -> None:
        self._stop.set()
        self.drop_sessions()
        self._listener.close()
        self._thread.join(timeout=2)

    def _accept_loop(self) -> None:
        self._listener.settimeout(TICK)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        transport = paramiko.Transport(conn)
        transport.add_server_key(self.host_key)
        handler = _Handler(self, transport)
        try:
            transport.start_server(server=handler)
        except paramiko.SSHException:
            transport.close()
            return
        with self._lock:
            self._transports.append(transport)
            self.sessions += 1
        while transport.is_active() and not self._stop.is_set():
            self._stop.wait(TICK)
        handler.close_forwards()
        transport.close()


class _Handler(paramiko.ServerInterface):
    def __init__(self, server: FakeSSHServer, transport: paramiko.Transport) -> None:
        self._server = server
        self._transport = transport
        self._listeners: dict[int, socket.socket] = {}

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self._server.username and password == self._server.password:
            return paramiko.AUTH_SUCCESSFUL
        self._server.failed_logins += 1
        return paramiko.AUTH_FAILED

    def check_port_forward_request(self, address: str, port: int) -> int:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(8)
        except OSError:
            listener.close()
            return 0
        bound = int(listener.getsockname()[1])
        self._listeners[bound] = listener
        threading.Thread(
            target=self._forward_loop, args=(listener, address, bound), daemon=True
        ).start()
        return bound

    def cancel_port_forward_request(self, address: str, port: int) -> None:
        listener = self._listeners.pop(port, None)
        if listener is not None:
            listener.close()

    def close_forwards(self) -> None:
        for listener in self._listeners.values():
            listener.close()
        self._listeners.clear()

    def _forward_loop(self, listener: socket.socket, address: str, port: int) -> None:
        listener.settimeout(TICK)
        while self._transport.is_active():
            try:
                conn, origin = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                channel = self._transport.open_forwarded_tcpip_channel(
                    origin, (address, port)
                )
            except paramiko.SSHException:
                conn.close()
                continue
            threading.Thread(target=_pump, args=(channel, conn), daemon=True).start()


def _pump(channel: paramiko.Channel, sock: socket.socket) -> None:
    try:
        while True:
            readable, _, _ = select.select([sock, channel], [], [], TICK)
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
    finally:
        _close_quietly(sock)
        _close_quietly(channel)


def _close_quietly(resource: socket.socket | paramiko.Channel) -> None:
    try:
        resource.close()
    except (OSError, EOFError, paramiko.SSHException):
        pass
