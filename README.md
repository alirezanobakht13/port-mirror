# port-mirror

Mirror TCP ports from the machine it runs on onto a remote server, over a
reverse SSH tunnel that reconnects on its own.

The traffic direction is the opposite of the SSH direction. Server A opens the
SSH connection to server B, and afterwards clients connect to **B**:

```
client ──▶ B:6006 ──(ssh, opened by A)──▶ A:6006
```

That is what makes it useful when A can reach B but B cannot reach A: a
TensorBoard on `A:6006` or an API on `A:5000` becomes reachable at
`B:6006` and `B:5000`, and stays reachable across network drops, B reboots and
SSH timeouts, because the tunnel is rebuilt automatically until you stop it.

Authentication is by password, read from standard input, so no key has to be
installed on B.

## Requirements

- Python 3.12 or newer on server A. Nothing has to be installed on server B.
- `GatewayPorts yes` in `/etc/ssh/sshd_config` on **server B**, so that the
  mirrored ports listen on all of B's interfaces instead of only on B's
  loopback. This is the one change that needs root on B:

  ```bash
  echo 'GatewayPorts yes' | sudo tee -a /etc/ssh/sshd_config
  sudo systemctl reload ssh     # or: sudo systemctl reload sshd
  ```

  Without it, `B:6006` answers only to processes running on B itself.
  `port-mirror` probes the first mirrored port after every connection and warns
  when it cannot be reached.
- Ports below 1024 on B can only be bound by an SSH user that is root on B.

## Install

On server A:

```bash
git clone <this repo> port-mirror
cd port-mirror
uv sync
```

The command is then `uv run port-mirror`, or `.venv/bin/port-mirror` if you
prefer to call it directly. `uv tool install .` puts it on `PATH`.

## Quick start

```bash
port-mirror --host server-b.example.com -p 6006 -p 5000
```

It prompts for B's password, and `A:6006` and `A:5000` are then served from
`B:6006` and `B:5000`. Press Ctrl-C to stop; everything else is retried.

The SSH user defaults to `root`; pass `--user` for anything else.

The same thing from a config file:

```bash
port-mirror --example-config > config.toml
$EDITOR config.toml
port-mirror --config config.toml
```

## Ports

`-p/--port` (repeatable) and the `ports` list in the config file accept the same
four forms, which follow `ssh -R`:

| Form | Meaning |
| --- | --- |
| `6006` | `B:6006` serves `127.0.0.1:6006` on A |
| `9006:6006` | `B:9006` serves `127.0.0.1:6006` on A |
| `9006:127.0.0.1:5000` | `B:9006` serves `127.0.0.1:5000` on A |
| `0.0.0.0:9006:10.0.0.4:5000` | bind address on B, and a target A can reach |

The third form is what to use when the service listens on a different local
port, and the fourth when the service runs on another machine on A's network
rather than on A itself.

## Configuration

TOML and YAML are both accepted; the file extension decides. Every setting can
be overridden on the command line, and `--help` lists the flags.

```toml
host = "server-b.example.com"
user = "root"
port = 22

ports = [6006, 5000]
bind_address = "0.0.0.0"

keepalive_interval = 30
keepalive_timeout = 90
connect_timeout = 15

host_key_policy = "auto-add"
known_hosts = "~/.ssh/known_hosts"
check_reachability = true

[retry]
initial_delay = 1.0
max_delay = 60.0
multiplier = 2.0
jitter = 0.2
stable_after = 30.0
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `host`, `user`, `port` | — , `root`, `22` | how to reach B over SSH |
| `password` | — | the SSH password, in the clear |
| `password_file` | — | file whose first line holds it instead |
| `ports` | — | what to mirror |
| `bind_address` | `0.0.0.0` | address the forwards bind on B |
| `keepalive_interval` | `30` | seconds between keepalive requests |
| `keepalive_timeout` | `90` | silence after which the link counts as dead |
| `connect_timeout` | `15` | seconds allowed for connecting and authenticating |
| `local_connect_timeout` | `10` | seconds allowed for reaching the local service |
| `host_key_policy` | `auto-add` | `auto-add`, `warn` or `reject` for an unknown host key |
| `known_hosts` | `~/.ssh/known_hosts` | where host keys are stored |
| `check_reachability` | `true` | probe the first mirrored port after connecting |
| `reconnect` | `true` | rebuild the tunnel after a failure |
| `retry.*` | see above | exponential backoff between attempts |

## The password

`port-mirror` reads B's password from standard input. At a terminal that is a
prompt; otherwise it is the first line piped in:

```bash
echo 'the ssh password' | port-mirror --config config.toml
port-mirror --config config.toml < /etc/port-mirror/password
```

A `password` or `password_file` in the config file is used instead when either
is set, and `--ask-password` ignores both and goes back to stdin. Nothing is
read from the environment, so the secret stays out of `/proc/<pid>/environ` and
out of the shell history that an `export` would leave behind.

## Staying up

A keepalive request goes out every `keepalive_interval` seconds. When no answer
arrives within `keepalive_timeout`, the session is treated as dead and rebuilt —
this is what catches a network that vanishes without closing the TCP connection,
which a plain `ssh -R` would sit on indefinitely.

Reconnection backs off exponentially from `retry.initial_delay` to
`retry.max_delay`, with jitter, and the delay resets once a connection has held
for `retry.stable_after` seconds. This continues until the process is stopped,
with two exceptions that retrying cannot fix: a rejected password and a changed
host key both exit with status 1.

If B's sshd has not yet noticed that the old session died, it still holds the
mirrored port and the next attempt is refused with *"the port may still be held
by a previous session"*. The retries ride this out. To make B give the port up
faster, add to its `sshd_config`:

```
ClientAliveInterval 30
ClientAliveCountMax 3
```

## Running as a service

To survive reboots and crashes as well as dropped connections, run it under
systemd on server A:

```bash
sudo install -m 600 /dev/stdin /etc/port-mirror/password <<< 'the ssh password'
port-mirror --config /etc/port-mirror/config.toml --systemd-unit \
  | sudo tee /etc/systemd/system/port-mirror.service
sudo systemctl daemon-reload
sudo systemctl enable --now port-mirror
journalctl -u port-mirror -f
```

The generated unit feeds that file to the service on stdin with
`StandardInput=file:/etc/port-mirror/password`, so keep it at mode `600` and
owned by the user the unit runs as.

The unit restarts on failure but not on a clean stop, so `systemctl stop` stays
stopped, and `StartLimitBurst` stops a rejected password from retrying forever.

## Troubleshooting

**Clients outside B cannot connect, but `curl localhost:6006` on B works.**
`GatewayPorts yes` is missing from B's `sshd_config`, or a firewall on B blocks
the port.

**`no local service at 127.0.0.1:6006`.** The tunnel is up and B received a
connection, but nothing is listening on A. Note that a service bound to
`127.0.0.1` on A is reachable, since `port-mirror` connects to it from A.

**`the port may still be held by a previous session`.** B has not released the
port from the previous connection yet; the retries will get it. See
*Staying up*.

**Everything looks fine but nothing arrives.** Run with `--log-level DEBUG` to
log each forwarded connection as it is accepted.

## Development

```bash
uv sync
uv run pytest
```

The tests run a real SSH server in-process (`tests/sshserver.py`), so the
forwarding, the reconnect loop and the password handling are exercised end to
end without needing a second machine.
