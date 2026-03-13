# Remote Server SSH Utility

A standalone, reusable Python SSH utility for managing remote server connections, executing commands with session-level caching, and transferring files — built on top of [Paramiko](https://www.paramiko.org/).

## Features

- **Singleton SSH Client** — One connection per server, automatically reused within a configurable TTL.
- **Command Output Caching** — LRU cache with per-entry TTL so repeated commands are instant. Cache persists across reconnections.
- **Password & Key Authentication** — Supports password, explicit private key, default private key, or SSH agent fallback.
- **Interactive Shell** — Background thread continuously reads shell output for interactive workflows.
- **Jump-Host / Nested SSH** — Execute commands on a target server by hopping through the current server.
- **SCP File Transfer** — Upload and download files via SCP (optional `scp` dependency).
- **SFTP File Reading** — Read remote files without downloading them.
- **Reboot & Reconnect** — Send a reboot command and automatically wait for the server to come back online.
- **Ping Test** — ICMP reachability check before attempting SSH.
- **Credential Safety** — Commands containing passwords are automatically excluded from cache and redacted in logs.
- **ANSI Stripping** — Clean raw terminal output into readable text.

## Installation

```bash
pip install paramiko
pip install scp  # optional — only needed for upload_file / download_file
```

Copy `remote_server_ssh_utility.py` into your project.

## Quick Start

```python
import logging
from remote_server_ssh_utility import RemoteServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ssh")

config = {
    'server_ip': '192.168.1.10',
    'port': 22,
    'username': 'admin',
    'password': 's3cret',
}

# Get or create a singleton instance
server = RemoteServer.get_instance(config, logger)
server.connect()

# Run a command
output = server.execute_command("uname -a")
print(output)

# Run with caching — second call is served from cache
output = server.execute_command("cat /etc/os-release", use_cache=True)
output = server.execute_command("cat /etc/os-release", use_cache=True)  # cache hit

# Disconnect when done
server.disconnect()
```

## API Reference

### Class: `RemoteServer`

#### Factory

| Method | Description |
|---|---|
| `RemoteServer.get_instance(server_config, logger, ttl_minutes=5)` | Return an existing instance if within TTL, otherwise create a new one. |

#### Constructor

```python
RemoteServer(server_config, logger, default_private_key=None)
```

| Parameter | Type | Description |
|---|---|---|
| `server_config` | `dict` | `server_ip`, `port`, `username`, and optionally `password`. |
| `logger` | logger | Any logger with `.info()`, `.error()`, `.debug()`, `.warning()` methods. |
| `default_private_key` | `str \| None` | Fallback private key path when no password is provided. |

#### Connection

| Method | Description |
|---|---|
| `connect(private_key=None)` | Establish SSH session (up to 10 retries). |
| `disconnect()` | Close SSH session. Cache is preserved. |
| `open_shell()` | Manually (re)open an interactive shell channel. |
| `ping()` | ICMP ping test. Returns `True` / `False`. |

#### Command Execution

| Method | Description |
|---|---|
| `execute_command(command, use_cache=False)` | Execute via `exec_command`. Returns stdout as string. |
| `execute_via_jump_host(target_config, command, use_cache=False)` | SSH through this server to a target host and execute a command. |
| `server_hard_reboot(reboot_command='reboot', reconnect_timeout=300)` | Send reboot and wait for reconnection. Returns `True` / `False`. |

**`target_config` dict for jump-host:**

```python
{
    'target_ip': '10.0.0.5',
    'target_username': 'user',
    'target_password': 'pass',
    'target_port': 22,  # optional, default 22
}
```

#### Caching

| Method | Description |
|---|---|
| `get_cached_output(command)` | Look up cached output across all cache-key types. |
| `clear_cache(command=None)` | Clear cache for a specific command, or the entire cache. |

Cache behaviour:
- Entries expire after **5 minutes** (`_CACHE_ENTRY_TTL_SECONDS`).
- Max **256 entries** per server, evicted LRU (`_CACHE_MAX_ENTRIES_PER_SERVER`).
- Commands containing credentials are **never cached**.
- Cache survives `disconnect()` / reconnection — call `clear_cache()` to reset.

#### File Operations

| Method | Description |
|---|---|
| `read_remote_file(remote_file_path)` | Read a file via SFTP. Returns `bytes` or `None`. |
| `upload_file(local_path, remote_path)` | Upload via SCP (requires `scp` package). |
| `download_file(remote_path, local_path)` | Download via SCP (requires `scp` package). |

#### Output Helpers

| Method | Description |
|---|---|
| `parse_output(output)` | Clean raw shell output (strip ANSI, byte artefacts). |
| `escape_ansi(input_string)` | Static — remove ANSI escape sequences. |
| `print_lines(data)` | Static — return the last line from streamed data. |

## Advanced Usage

### Singleton Pattern

```python
# Both variables point to the same SSH session
server_a = RemoteServer.get_instance(config, logger)
server_b = RemoteServer.get_instance(config, logger)
assert server_a is server_b
```

Instances are keyed by the full `server_config` dict (IP + port + username + password), so different ports or users on the same IP get separate instances.

### Private Key Authentication

```python
# Option 1: Default key in constructor
server = RemoteServer(config, logger, default_private_key="~/.ssh/id_rsa")
server.connect()

# Option 2: Per-connection key
server = RemoteServer(config, logger)
server.connect(private_key="/path/to/key")

# Option 3: No key, no password — falls back to SSH agent / ~/.ssh keys
config_no_pass = {'server_ip': '10.0.0.1', 'port': 22, 'username': 'admin'}
server = RemoteServer(config_no_pass, logger)
server.connect()
```

### Jump-Host / Nested SSH

```python
# Connect to a bastion/jump host
bastion = RemoteServer.get_instance(bastion_config, logger)
bastion.connect()

# Execute on an internal server through the bastion
target = {
    'target_ip': '10.0.0.5',
    'target_username': 'appuser',
    'target_password': 'apppass',
}
output = bastion.execute_via_jump_host(target, "systemctl status nginx")
```

### Reboot & Reconnect

```python
# Default reboot command
success = server.server_hard_reboot()

# Custom reboot command and timeout
success = server.server_hard_reboot(
    reboot_command="sudo shutdown -r now",
    reconnect_timeout=600,
)
```

### SCP File Transfer

```python
server.upload_file("/local/data.csv", "/remote/data.csv")
server.download_file("/remote/results.json", "/local/results.json")
```

> **Note:** Requires `pip install scp`. If not installed, a clear `RuntimeError` is raised.

## Configuration Constants

| Constant | Default | Description |
|---|---|---|
| `_CACHE_ENTRY_TTL_SECONDS` | `300` | Per-entry cache lifetime (seconds). |
| `_CACHE_MAX_ENTRIES_PER_SERVER` | `256` | Max cached commands per server (LRU). |

Override on the class before creating instances:

```python
RemoteServer._CACHE_ENTRY_TTL_SECONDS = 600      # 10 minutes
RemoteServer._CACHE_MAX_ENTRIES_PER_SERVER = 512  # larger cache
```

## License

MIT License. See [LICENSE](LICENSE) for details.
