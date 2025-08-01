# Remote Server Utils

A comprehensive Python utility module for managing and interacting with remote servers and virtual machines.

## Overview

The `remote_server_utils.py` module provides a robust `RemoteServer` class that handles SSH connections, command execution, and file operations on remote servers. It's designed for automation scenarios involving remote hosts, virtual machines, and containerized environments.

## Features

- **SSH Connection Management**: Secure SSH connections with retry logic and connection pooling
- **Command Execution**: Execute commands both in remote servers and within EVE services
- **File Operations**: Read remote files using SFTP
- **Connection Caching**: Instance caching with TTL to optimize performance
- **Error Handling**: Comprehensive error handling with detailed logging
- **ANSI Escape Sequence Cleaning**: Automatic cleaning of terminal output
- **Hard Reboot Support**: Remote server reboot with automatic reconnection
- **Ping Testing**: Network connectivity verification

## Installation

### Prerequisites

```bash
pip install paramiko scp
```

### Dependencies

- `paramiko`: SSH protocol implementation
- `scp`: SCP client for file transfers
- `threading`: For background data processing
- `socket`: Network operations
- `subprocess`: Local command execution
- `re`: Regular expressions for output parsing
- `time`: Timing and delays
- `os`: Operating system interface

## Usage

### Basic Usage

```python
import logging
from z_components.eve.eve_utils.edge_node_utils import RemoteServer
import os

# Initialize logger
logger = logging.getLogger("remote_server_utils")
logger.setLevel(logging.INFO)
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
# Now you can use logger.info(), logger.warning(), logger.error()

# Define remote server connection parameters
app_vars = {
    'server_ip': '192.168.0.55',
    'port': 22,
    'username': 'admin',
    'password': 'password'
}

# Get RemoteServer instance (uses caching)
app_ssh_client = RemoteServer.get_instance(app_vars, logger)

# Create SSH session
app_ssh_client.create_device_session()

# Execute remote command
output = app_ssh_client.execute_command_in_remote_server("cat /tmp/test_file.txt")
print(output)
```

### Advanced Usage

```python
# Test connectivity
if app_ssh_client.remote_server_ping_test():
    print("Remote server is reachable")


# Read remote file
file_content = app_ssh_client.read_remote_file("/etc/hostname")
print(f"Hostname: {file_content}")

# Hard reboot server
app_ssh_client.node_hard_reboot()
```

## API Reference

### RemoteServer Class

#### Class Methods

##### `get_instance(server_vars, zlogger, ttl_minutes=5)`

Returns a cached instance of RemoteServer or creates a new one.

**Parameters:**
- `server_vars` (dict): Device connection parameters
- `zlogger`: Logger instance
- `ttl_minutes` (int): Time to live for cached instances (default: 5)

**Returns:**
- `RemoteServer`: Instance of RemoteServer class

#### Instance Methods

##### `create_device_session(priv_key=None)`

Establishes SSH connection to the remote server.

**Parameters:**
- `priv_key` (str, optional): Path to private key file

**Features:**
- Automatic retry logic (10 attempts with 15-second delays)
- Support for password and key-based authentication
- Automatic host key policy handling

##### `remote_server_ping_test()`

Tests network connectivity to the remote server.

**Returns:**
- `bool`: True if reachable, False otherwise

##### `execute_command_in_remote_server(command)`

Executes a command on the remote server.

**Parameters:**
- `command` (str): Command to execute

**Returns:**
- `str`: Command output

**Parameters:**
- `service` (str): Service/container name (e.g., "docker", "kube")
- `command` (str): Command to execute

**Returns:**
- `str`: Command output

##### `read_remote_file(remote_file_path)`

Reads contents of a file on the remote host.

**Parameters:**
- `remote_file_path` (str): Path to remote file

**Returns:**
- `str`: File contents or None if error

##### `node_hard_reboot()`

Performs a hard reboot of the remote server with automatic reconnection.

**Returns:**
- `tuple`: (return_out, client) - typically (None, None)

##### `close_connection()`

Closes the SSH connection.

#### Static Methods

##### `escape_ansi(input_string)`

Removes ANSI escape sequences from terminal output.

**Parameters:**
- `input_string` (str): Input string with ANSI codes

**Returns:**
- `str`: Cleaned string

##### `dump_command_output_into_txt(output)`

Saves command output to a text file.

**Parameters:**
- `output` (str): Command output to save

**Returns:**
- `str`: Path to saved file

## Configuration

### Connection Parameters

The `server_vars` dictionary should contain:

```python
{
    'server_ip': '192.168.0.55',    # Remote server IP address
    'port': 22,                     # SSH port (default: 22)
    'username': 'admin',            # SSH username
    'password': 'password'          # SSH password (optional if using key)
}
```

### SSH Key Authentication

To use SSH key authentication:

1. Place your private key in the resources directory
2. Use the `priv_key` parameter in `create_device_session()`

```python
app_ssh_client.create_device_session(priv_key="/path/to/private/key")
```

## Error Handling

The module includes comprehensive error handling for:

- Network connectivity issues
- Authentication failures
- SSH connection timeouts
- File operation errors
- Command execution failures

All errors are logged with detailed information for debugging.

## Logging

The module uses the provided logger instance for all operations. Log levels include:

- `INFO`: Connection status, command execution
- `ERROR`: Connection failures, authentication errors
- `DEBUG`: Detailed SSH operations (when paramiko debug is enabled)

## Performance Features

### Singleton Pattern Implementation

The module implements a singleton pattern with caching to optimize performance in automated testing scenarios:

#### Key Benefits for Automated Testing:

- **Reduced SSH Handshake Overhead**: Avoids repeated SSH connection establishment
- **Connection Reuse**: Multiple test methods can share the same SSH connection
- **Configurable TTL**: Connections are cached for 5 minutes by default (configurable)
- **Automatic Cleanup**: Expired connections are automatically cleaned up
- **Thread-Safe**: Safe for concurrent test execution

#### How It Works:

```python
# First call - creates new connection
server1 = RemoteServer.get_instance(server_vars, logger)

# Second call with same parameters - reuses existing connection
server2 = RemoteServer.get_instance(server_vars, logger)

# server1 and server2 are the same instance
assert server1 is server2  # True
```

#### TTL Configuration:

```python
# Custom TTL (10 minutes)
server = RemoteServer.get_instance(server_vars, logger, ttl_minutes=10)

# Default TTL (5 minutes)
server = RemoteServer.get_instance(server_vars, logger)
```

### Connection Caching

The module implements instance caching with TTL to avoid repeated connection overhead:

- Instances are cached for 5 minutes by default
- Configurable TTL via `ttl_minutes` parameter
- Automatic cleanup of expired instances
- Cache key based on server connection parameters

### Background Processing

Command output is processed in background threads to prevent blocking operations.

## Security Considerations

- Uses `AutoAddPolicy` for host key management
- Supports both password and key-based authentication
- Automatic cleanup of SSH connections
- No hardcoded credentials

## Examples

### Basic Command Execution

```python
# Simple command execution
output = app_ssh_client.execute_command_in_remote_server("ls -la")
print(output)
```

### File Operations

```python
# Read system configuration
config = app_ssh_client.read_remote_file("/etc/hostname")
print(config)

# Read application logs
logs = app_ssh_client.read_remote_file("/var/log/syslog")
print(logs)

# Read custom configuration files
app_config = app_ssh_client.read_remote_file("/opt/app/config.json")
print(app_config)
```

### Server Management

```python
# Test connectivity before operations
if app_ssh_client.remote_server_ping_test():
    # Perform operations
    app_ssh_client.execute_command_in_remote_server("systemctl status ssh")
else:
    print("Server is not reachable")

# Reboot server if needed
app_ssh_client.node_hard_reboot()
```

## Troubleshooting

### Common Issues

1. **Connection Timeout**
   - Check network connectivity
   - Verify IP address and port
   - Ensure SSH service is running

2. **Authentication Failure**
   - Verify username and password
   - Check SSH key permissions
   - Ensure user has SSH access

3. **Command Execution Issues**
   - Check command syntax
   - Verify user permissions
   - Ensure target service/container exists (for service commands)

### Debug Mode

Enable paramiko debug logging:

```python
import paramiko
paramiko.common.logging.basicConfig(level=paramiko.common.DEBUG)
```

## Best Practices for Automated Testing

### Optimizing Test Performance

1. **Reuse Connections**: Use the singleton pattern to avoid repeated SSH handshakes
2. **Configure Appropriate TTL**: Set TTL based on your test duration
3. **Clean Up Resources**: Explicitly close connections when tests complete
4. **Handle Connection Failures**: Implement retry logic for flaky networks

### Example Test Structure:

```python
import pytest
from z_components.eve.eve_utils.edge_node_utils import RemoteServer

class TestRemoteServer:
    @pytest.fixture(scope="class")
    def remote_server(self):
        """Fixture that provides a shared RemoteServer instance for all tests in the class"""
        server_vars = {
            'server_ip': '192.168.0.55',
            'port': 22,
            'username': 'admin',
            'password': 'password'
        }
        server = RemoteServer.get_instance(server_vars, logger, ttl_minutes=10)
        server.create_device_session()
        yield server
        server.close_connection()
    
    def test_server_connectivity(self, remote_server):
        """Test that server is reachable"""
        assert remote_server.remote_server_ping_test()
    
    def test_command_execution(self, remote_server):
        """Test command execution"""
        output = remote_server.execute_command_in_remote_server("echo 'hello'")
        assert "hello" in output
    
    def test_file_operations(self, remote_server):
        """Test file reading operations"""
        content = remote_server.read_remote_file("/etc/hostname")
        assert content is not None
```

### Performance Comparison:

| Approach | SSH Handshakes | Test Execution Time | Memory Usage |
|----------|----------------|-------------------|--------------|
| New connection per test | High | Slow | High |
| Singleton with caching | Low | Fast | Low |

## Contributing

When contributing to this module:

1. Follow the existing code style
2. Add comprehensive error handling
3. Include logging for all operations
4. Update this README for new features
5. Add unit tests for new functionality

## License

This module follows the project's licensing terms. 
