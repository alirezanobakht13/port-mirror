import pytest

from port_mirror.config import ConfigError, PortMapping, build_config, parse_port_spec


def base(**overrides):
    data = {"host": "b.example.com", "ports": [6006]}
    data.update(overrides)
    return data


def test_plain_port_mirrors_the_same_number():
    assert parse_port_spec(6006) == PortMapping(remote_port=6006, local_port=6006)


def test_two_part_spec_maps_remote_to_local():
    assert parse_port_spec("9006:5000") == PortMapping(
        remote_port=9006, local_port=5000
    )


def test_three_part_spec_carries_the_local_host():
    assert parse_port_spec("9006:10.0.0.2:5000") == PortMapping(
        remote_port=9006, local_port=5000, local_host="10.0.0.2"
    )


def test_four_part_spec_carries_the_bind_address():
    assert parse_port_spec("0.0.0.0:9006:10.0.0.2:5000") == PortMapping(
        remote_port=9006, local_port=5000, local_host="10.0.0.2", bind_address="0.0.0.0"
    )


def test_table_spec_defaults_local_to_remote():
    assert parse_port_spec({"remote": 6006}) == PortMapping(
        remote_port=6006, local_port=6006
    )


@pytest.mark.parametrize("spec", ["", "0", "70000", "a:b", "1:2:3:4:5", True, 0])
def test_invalid_specs_are_rejected(spec):
    with pytest.raises(ConfigError):
        parse_port_spec(spec)


def test_defaults_are_applied():
    config = build_config(base())
    assert config.user == "root"
    assert config.ssh_port == 22
    assert config.bind_address == "0.0.0.0"
    assert config.reconnect is True
    assert config.retry.max_delay == 60.0


def test_an_explicit_user_replaces_the_default():
    assert build_config(base(user="ubuntu")).user == "ubuntu"


def test_overrides_win_over_file_values():
    config = build_config(base(port=2222, user="ubuntu"), {"port": 2022, "user": None})
    assert config.ssh_port == 2022
    assert config.user == "ubuntu"


def test_retry_overrides_merge_into_the_file_table():
    config = build_config(
        base(retry={"initial_delay": 5.0, "max_delay": 50.0}),
        {"retry": {"max_delay": 120.0}},
    )
    assert config.retry.initial_delay == 5.0
    assert config.retry.max_delay == 120.0


def test_a_literal_password_is_used_as_given():
    assert build_config(base(password="hunter2")).password == "hunter2"


def test_password_file_is_read_without_its_newline(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("hunter2\n")
    assert build_config(base(password_file=str(secret))).password == "hunter2"


def test_missing_password_is_left_for_the_caller():
    assert build_config(base()).password is None


def test_missing_host_is_an_error():
    with pytest.raises(ConfigError, match="host"):
        build_config({"ports": [6006]})


def test_missing_ports_is_an_error():
    with pytest.raises(ConfigError, match="ports"):
        build_config({"host": "b"})


def test_duplicate_remote_ports_are_an_error():
    with pytest.raises(ConfigError, match="more than once"):
        build_config(base(ports=[6006, "6006:5000"]))


def test_unknown_keys_are_an_error():
    with pytest.raises(ConfigError, match="unknown setting"):
        build_config(base(bnid_address="0.0.0.0"))


def test_unknown_retry_keys_are_an_error():
    with pytest.raises(ConfigError, match="unknown retry setting"):
        build_config(base(retry={"backoff": 2}))


def test_max_delay_below_initial_delay_is_an_error():
    with pytest.raises(ConfigError, match="max_delay"):
        build_config(base(retry={"initial_delay": 10.0, "max_delay": 5.0}))


def test_unknown_host_key_policy_is_an_error():
    with pytest.raises(ConfigError, match="host_key_policy"):
        build_config(base(host_key_policy="ignore"))
