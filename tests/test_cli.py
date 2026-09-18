import io
import tomllib

import pytest

from port_mirror import cli
from port_mirror.config import build_config


def parse(argv):
    parser = cli._build_parser()
    return cli._config_from_args(parser.parse_args(argv))


class PipedStdin(io.StringIO):
    """Stdin as it looks when a password is piped in rather than typed."""

    def isatty(self) -> bool:
        return False


def piped_stdin(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", PipedStdin(text))


def capture_supervise(monkeypatch):
    """Run the CLI without a tunnel, returning the config it would have used."""
    captured = {}

    def fake_supervise(config, stop_event):
        captured["config"] = config
        return 0

    monkeypatch.setattr(cli, "supervise", fake_supervise)
    return captured


def test_example_config_is_valid_and_complete(capsys):
    assert cli.main(["--example-config"]) == 0
    data = tomllib.loads(capsys.readouterr().out)
    config = build_config(data)
    assert config.host == "server-b.example.com"
    assert config.password is None
    assert [mapping.remote_port for mapping in config.mappings] == [6006, 5000]


def test_flags_alone_configure_a_tunnel():
    config = parse(
        ["--host", "b.example.com", "--user", "ubuntu", "-p", "6006", "-p", "5000"]
    )
    assert config.host == "b.example.com"
    assert [mapping.remote_port for mapping in config.mappings] == [6006, 5000]


def test_flags_override_the_config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('host = "old"\nuser = "ubuntu"\nports = [6006]\n')
    config = parse(["--config", str(path), "--host", "new", "-p", "7007"])
    assert config.host == "new"
    assert config.user == "ubuntu"
    assert [mapping.remote_port for mapping in config.mappings] == [7007]


def test_ask_password_discards_a_configured_secret(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('host = "b"\nports = [6006]\npassword = "from-file"\n')
    assert parse(["--config", str(path)]).password == "from-file"
    assert parse(["--config", str(path), "--ask-password"]).password is None


def test_no_reconnect_and_no_check_are_applied():
    config = parse(
        ["--host", "b", "--user", "u", "-p", "6006", "--no-reconnect", "--no-check"]
    )
    assert config.reconnect is False
    assert config.check_reachability is False


def test_missing_host_exits_with_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--user", "ubuntu", "-p", "6006"])
    assert exit_info.value.code == cli.EXIT_USAGE
    assert "'host' is required" in capsys.readouterr().err


def test_bad_port_spec_exits_with_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--host", "b", "--user", "u", "-p", "not-a-port"])
    assert exit_info.value.code == cli.EXIT_USAGE
    assert "remote port" in capsys.readouterr().err


def test_password_is_read_from_piped_stdin(monkeypatch):
    piped_stdin(monkeypatch, "from-stdin\n")
    captured = capture_supervise(monkeypatch)
    assert cli.main(["--host", "b", "--user", "u", "-p", "6006"]) == 0
    assert captured["config"].password == "from-stdin"


def test_only_the_first_line_of_stdin_is_the_password(monkeypatch):
    piped_stdin(monkeypatch, "secret\nnoise\n")
    captured = capture_supervise(monkeypatch)
    assert cli.main(["--host", "b", "--user", "u", "-p", "6006"]) == 0
    assert captured["config"].password == "secret"


def test_a_prompt_is_used_when_stdin_is_a_terminal(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "typed-in")
    captured = capture_supervise(monkeypatch)
    assert cli.main(["--host", "b", "--user", "u", "-p", "6006"]) == 0
    assert captured["config"].password == "typed-in"


def test_empty_stdin_is_a_usage_error(capsys, monkeypatch):
    piped_stdin(monkeypatch, "")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--host", "b", "--user", "u", "-p", "6006"])
    assert exit_info.value.code == cli.EXIT_USAGE
    assert "no password on stdin" in capsys.readouterr().err


def test_systemd_unit_names_the_config_and_restarts(capsys, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('host = "b.example.com"\nuser = "ubuntu"\nports = [6006]\n')
    assert cli.main(["--config", str(path), "--systemd-unit"]) == 0
    unit = capsys.readouterr().out
    assert str(path) in unit
    assert "StandardInput=file:/etc/port-mirror/password" in unit
    assert "Restart=on-failure" in unit
    assert "KillSignal=SIGTERM" in unit
    assert "6006" in unit
