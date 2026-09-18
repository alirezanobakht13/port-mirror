import pytest

from port_mirror.config import ConfigError, load_config_file

TOML = """\
host = "b.example.com"
user = "ubuntu"
ports = [6006, "9006:127.0.0.1:5000"]

[retry]
max_delay = 30.0
"""

YAML = """\
host: b.example.com
user: ubuntu
ports:
  - 6006
  - 9006:127.0.0.1:5000
retry:
  max_delay: 30.0
"""


@pytest.mark.parametrize(
    ("name", "body"), [("config.toml", TOML), ("config.yaml", YAML)]
)
def test_both_formats_load_to_the_same_settings(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body)
    data = load_config_file(path)
    assert data["host"] == "b.example.com"
    assert data["ports"] == [6006, "9006:127.0.0.1:5000"]
    assert data["retry"]["max_delay"] == 30.0


def test_a_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config_file(tmp_path / "absent.toml")


def test_malformed_toml_is_reported(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("host = ")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config_file(path)
