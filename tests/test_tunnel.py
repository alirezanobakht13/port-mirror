import socket
import threading
import time

import pytest

from port_mirror.config import build_config
from port_mirror.supervisor import EXIT_FAILED, EXIT_OK, supervise
from port_mirror.tunnel import Tunnel

from .sshserver import EchoServer, FakeSSHServer, free_port

TIMEOUT = 15.0


@pytest.fixture
def echo():
    server = EchoServer().start()
    yield server
    server.stop()


@pytest.fixture
def ssh(tmp_path):
    server = FakeSSHServer().start()
    yield server
    server.stop()


def make_config(ssh, echo, remote_port, tmp_path, **extra):
    settings = {
        "host": "127.0.0.1",
        "port": ssh.port,
        "user": ssh.username,
        "password": ssh.password,
        "ports": [f"{remote_port}:127.0.0.1:{echo.port}"],
        "bind_address": "127.0.0.1",
        "check_reachability": False,
        "known_hosts": str(tmp_path / "known_hosts"),
        "keepalive_interval": 1,
        "keepalive_timeout": 5,
        "connect_timeout": 5,
    }
    settings.update(extra)
    return build_config(settings)


def round_trip(port, payload=b"ping", timeout=TIMEOUT):
    """Send a payload through the mirrored port and return the reply."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                sock.sendall(payload)
                sock.settimeout(2)
                return sock.recv(len(payload))
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    raise AssertionError(f"port {port} never answered: {last_error}")


def test_mirrored_port_reaches_the_local_service(ssh, echo, tmp_path):
    remote_port = free_port()
    stop = threading.Event()
    config = make_config(ssh, echo, remote_port, tmp_path)
    worker = threading.Thread(target=Tunnel(config, stop).run, daemon=True)
    worker.start()
    try:
        assert round_trip(remote_port) == b"ping"
    finally:
        stop.set()
        worker.join(timeout=TIMEOUT)
    assert not worker.is_alive()


def test_several_ports_are_mirrored_at_once(ssh, tmp_path):
    first = EchoServer().start()
    second = EchoServer().start()
    remote_first, remote_second = free_port(), free_port()
    config = build_config(
        {
            "host": "127.0.0.1",
            "port": ssh.port,
            "user": ssh.username,
            "password": ssh.password,
            "ports": [
                f"{remote_first}:127.0.0.1:{first.port}",
                f"{remote_second}:127.0.0.1:{second.port}",
            ],
            "bind_address": "127.0.0.1",
            "check_reachability": False,
            "known_hosts": str(tmp_path / "known_hosts"),
        }
    )
    stop = threading.Event()
    worker = threading.Thread(target=Tunnel(config, stop).run, daemon=True)
    worker.start()
    try:
        assert round_trip(remote_first, b"one") == b"one"
        assert round_trip(remote_second, b"two") == b"two"
    finally:
        stop.set()
        worker.join(timeout=TIMEOUT)
        first.stop()
        second.stop()


def test_larger_payloads_survive_the_tunnel(ssh, echo, tmp_path):
    remote_port = free_port()
    stop = threading.Event()
    config = make_config(ssh, echo, remote_port, tmp_path)
    worker = threading.Thread(target=Tunnel(config, stop).run, daemon=True)
    worker.start()
    payload = bytes(range(256)) * 1024
    try:
        round_trip(remote_port)
        with socket.create_connection(("127.0.0.1", remote_port), timeout=5) as sock:
            sock.sendall(payload)
            sock.settimeout(10)
            received = bytearray()
            while len(received) < len(payload):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                received.extend(chunk)
        assert bytes(received) == payload
    finally:
        stop.set()
        worker.join(timeout=TIMEOUT)


def test_tunnel_reconnects_after_the_link_drops(ssh, echo, tmp_path):
    remote_port = free_port()
    stop = threading.Event()
    config = make_config(
        ssh, echo, remote_port, tmp_path, retry={"initial_delay": 0.2, "max_delay": 1.0}
    )
    worker = threading.Thread(target=supervise, args=(config, stop), daemon=True)
    worker.start()
    try:
        assert round_trip(remote_port) == b"ping"
        ssh.drop_sessions()
        assert round_trip(remote_port, b"back") == b"back"
        assert ssh.sessions >= 2
    finally:
        stop.set()
        worker.join(timeout=TIMEOUT)
    assert not worker.is_alive()


def test_supervisor_gives_up_on_a_bad_password(ssh, echo, tmp_path):
    remote_port = free_port()
    stop = threading.Event()
    config = make_config(ssh, echo, remote_port, tmp_path, password="wrong")
    assert supervise(config, stop) == EXIT_FAILED
    assert ssh.failed_logins >= 1


def test_supervisor_reports_a_clean_stop(ssh, echo, tmp_path):
    remote_port = free_port()
    stop = threading.Event()
    config = make_config(ssh, echo, remote_port, tmp_path)
    worker = threading.Thread(
        target=lambda: results.append(supervise(config, stop)), daemon=True
    )
    results = []
    worker.start()
    round_trip(remote_port)
    stop.set()
    worker.join(timeout=TIMEOUT)
    assert results == [EXIT_OK]


def test_unreachable_local_service_does_not_kill_the_tunnel(ssh, echo, tmp_path):
    remote_port = free_port()
    dead_port = free_port()
    stop = threading.Event()
    config = build_config(
        {
            "host": "127.0.0.1",
            "port": ssh.port,
            "user": ssh.username,
            "password": ssh.password,
            "ports": [
                f"{remote_port}:127.0.0.1:{echo.port}",
                f"{dead_port}:127.0.0.1:{free_port()}",
            ],
            "bind_address": "127.0.0.1",
            "check_reachability": False,
            "known_hosts": str(tmp_path / "known_hosts"),
        }
    )
    worker = threading.Thread(target=Tunnel(config, stop).run, daemon=True)
    worker.start()
    try:
        assert round_trip(remote_port) == b"ping"
        with socket.create_connection(("127.0.0.1", dead_port), timeout=5) as sock:
            sock.settimeout(5)
            assert sock.recv(16) == b""
        assert round_trip(remote_port, b"alive") == b"alive"
    finally:
        stop.set()
        worker.join(timeout=TIMEOUT)


def test_reachability_check_confirms_a_published_port(ssh, echo, tmp_path, caplog):
    remote_port = free_port()
    stop = threading.Event()
    config = make_config(
        ssh,
        echo,
        remote_port,
        tmp_path,
        bind_address="0.0.0.0",
        check_reachability=True,
    )
    worker = threading.Thread(target=Tunnel(config, stop).run, daemon=True)
    worker.start()
    try:
        with caplog.at_level("INFO", logger="port_mirror.tunnel"):
            round_trip(remote_port)
            deadline = time.monotonic() + TIMEOUT
            while time.monotonic() < deadline:
                if any("verified" in record.message for record in caplog.records):
                    break
                time.sleep(0.1)
        assert any(
            f"verified 127.0.0.1:{remote_port}" in record.message
            for record in caplog.records
        )
    finally:
        stop.set()
        worker.join(timeout=TIMEOUT)
