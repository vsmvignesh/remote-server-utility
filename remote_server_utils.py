"""
Remote Server SSH Utility
=========================

A standalone SSH utility for managing remote server connections,
executing commands, caching outputs, and transferring files over SCP/SFTP.

Features:
    - Singleton SSH client per server (connection reuse with configurable TTL)
    - Class-level LRU command output caching with per-entry TTL
    - Password and private-key authentication
    - Interactive shell sessions with background output reader
    - Nested SSH / jump-host command execution
    - SCP file upload/download
    - SFTP remote file reading
    - Ping reachability test
    - ANSI escape sequence stripping
    - Credential redaction in logs

Dependencies:
    pip install paramiko
    pip install scp          # optional — only needed for upload_file / download_file
"""

# pylint: disable=too-many-instance-attributes

import os
import re
import shlex
import socket
import subprocess
import threading
import time
from collections import OrderedDict
from subprocess import Popen, PIPE
import paramiko


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _command_likely_contains_secrets(command: str) -> bool:
    """Return True if the command string may contain credentials (e.g. sshpass -p)."""
    if not command:
        return False
    lower = command.strip().lower()
    patterns = ("sshpass", "-p '", '-p "', "password=", "passwd=")
    return any(p in lower for p in patterns)


def _redact_command_for_log(command: str) -> str:
    """Return a redacted version of the command safe for logging."""
    if not command or not _command_likely_contains_secrets(command):
        return command
    # Redact sshpass -p '...' or -p "..."
    out = re.sub(r"(-p\s+)['\"][^'\"]*['\"]", r"\g<1>'***'", command)
    # Redact password=value and passwd=value
    out = re.sub(r"(password|passwd)=[^\s&]+", r"\g<1>=***", out, flags=re.IGNORECASE)
    return out


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class RemoteServer:
    """
    An SSH client for remote server operations.

    Public methods
    --------------
    - get_instance          Singleton factory (class method)
    - connect               Establish an SSH session (with retry)
    - disconnect            Close the SSH session
    - open_shell            Open an interactive shell channel
    - execute_command       Execute a command via exec_command
    - execute_via_jump_host Execute a command on a target host through this server
    - server_hard_reboot    Reboot the server and wait for reconnection
    - ping                  ICMP reachability test
    - read_remote_file      Read a remote file via SFTP
    - upload_file           Upload a local file via SCP
    - download_file         Download a remote file via SCP
    - get_cached_output     Retrieve cached output for a command
    - clear_cache           Clear command output cache
    - parse_output          Parse raw shell output into readable text
    - escape_ansi           Strip ANSI escape sequences (static)
    - print_lines           Debug helper for streamed data (static)
    """

    _instances = {}  # {frozenset(server_config.items()): (instance, created_time)}
    _command_cache = {}  # {frozenset(server_config.items()): OrderedDict({cache_key: (output, ts)})}
    _CACHE_ENTRY_TTL_SECONDS = 300  # TTL for individual cache entries (5 minutes)
    _CACHE_MAX_ENTRIES_PER_SERVER = 256  # LRU limit per connection bucket

    # ------------------------------------------------------------------
    # Singleton factory
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls, server_config, logger, ttl_minutes=5):
        """
        Return an existing instance for *server_config* if still within TTL,
        otherwise create a new one.

        Args:
            server_config (dict): Must include ``server_ip``, ``port``,
                ``username``, and either ``password`` or rely on a private key.
            logger: Any logger exposing ``.info()``, ``.error()``, etc.
            ttl_minutes (int): Lifetime of a cached instance in minutes.

        Returns:
            RemoteServer: A connected (or ready-to-connect) instance.
        """
        key = frozenset(server_config.items())
        current_time = time.time()
        instance_info = cls._instances.get(key)

        if instance_info:
            instance, created_time = instance_info
            if current_time - created_time <= ttl_minutes * 60:
                logger.info(f"Reusing existing RemoteServer instance for "
                            f"{server_config['server_ip']}")
                instance.fulldata = ''
                instance.strdata = ''
                return instance
            logger.info(f"Recreating RemoteServer instance for "
                        f"{server_config['server_ip']} (expired)")
            instance.disconnect()
            del cls._instances[key]
            cls._command_cache.pop(key, None)

        new_instance = cls(server_config, logger)
        cls._instances[key] = (new_instance, current_time)
        logger.info(f"Created new RemoteServer instance for "
                    f"{server_config['server_ip']}")
        return new_instance

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self, server_config, logger, default_private_key=None):
        """
        Args:
            server_config (dict): Connection parameters::

                {
                    'server_ip': '192.168.1.10',
                    'port': 22,
                    'username': 'admin',
                    'password': 's3cret'       # optional if using key auth
                }

            logger: Logger instance (Python ``logging.Logger`` or compatible).
            default_private_key (str | None): Path to a default private key
                file used when no password is provided and no explicit key is
                passed to :meth:`connect`.
        """
        self.logger = logger
        self.shell = None
        self.client = None
        self.transport = None
        self.fulldata = ''
        self.strdata = ''
        self.server_config = server_config
        self.default_private_key = default_private_key
        self.scp_client = None
        self.reachable = False
        self._process_stop_event = threading.Event()
        self._process_thread = None

        # Class-level cache bucket keyed by full connection identity
        self._cache_key = frozenset(server_config.items())
        if self._cache_key not in RemoteServer._command_cache:
            RemoteServer._command_cache[self._cache_key] = OrderedDict()

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    @property
    def _server_cache(self):
        """Access the class-level cache bucket for this connection."""
        return RemoteServer._command_cache[self._cache_key]

    def _get_cached(self, cache_key):
        """Return cached output if present and not expired; else ``None``."""
        bucket = self._server_cache
        if cache_key not in bucket:
            return None
        output, ts = bucket[cache_key]
        if time.time() - ts > RemoteServer._CACHE_ENTRY_TTL_SECONDS:
            del bucket[cache_key]
            return None
        bucket.move_to_end(cache_key)
        return output

    def _set_cached(self, cache_key, output):
        """Store output with current timestamp and enforce LRU max size."""
        bucket = self._server_cache
        bucket[cache_key] = (output, time.time())
        bucket.move_to_end(cache_key)
        while len(bucket) > RemoteServer._CACHE_MAX_ENTRIES_PER_SERVER:
            bucket.popitem(last=False)

    def get_cached_output(self, command):
        """
        Retrieve cached output for a previously executed command.

        Searches across all cache-key types (``remote``, ``nested_ssh``).

        Args:
            command (str): The command string to look up.

        Returns:
            str | None: Cached output, or ``None`` if not found.
        """
        log_cmd = _redact_command_for_log(command)
        for key in list(self._server_cache):
            if key.endswith(f":{command}"):
                output = self._get_cached(key)
                if output is not None:
                    self.logger.info(
                        f"[CACHE] [{self.server_config['server_ip']}] "
                        f"Found cached output for: {log_cmd}"
                    )
                    return output
        return None

    def clear_cache(self, command=None):
        """
        Clear the command output cache.

        Args:
            command (str | None): If provided, only clear entries matching
                this command.  If ``None``, clear the entire cache.
        """
        if command is None:
            count = len(self._server_cache)
            self._server_cache.clear()
            self.logger.info(
                f"[CACHE] [{self.server_config['server_ip']}] "
                f"Cleared entire cache ({count} entries)"
            )
        else:
            keys_to_remove = [k for k in self._server_cache
                              if k.endswith(f":{command}")]
            for key in keys_to_remove:
                del self._server_cache[key]
            log_cmd = _redact_command_for_log(command)
            self.logger.info(
                f"[CACHE] [{self.server_config['server_ip']}] "
                f"Cleared {len(keys_to_remove)} cache entries for: {log_cmd}"
            )

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self, private_key=None):
        """
        Establish an SSH session with the remote server.

        Tries up to 10 times with a 15-second delay between retries.

        Args:
            private_key (str | None): Path to a private key file.  Falls back
                to ``default_private_key`` (from ``__init__``) when both
                *private_key* and *password* are absent.
        """
        retries = 0
        delay = 15
        max_retries = 10
        while retries <= max_retries:
            try:
                self.logger.info(
                    f"Connecting to server at {self.server_config['server_ip']}"
                )
                # Stop any existing reader thread to avoid leak
                if self._process_thread is not None and self._process_thread.is_alive():
                    self._process_stop_event.set()
                    self._process_thread.join(timeout=2.0)
                    if self._process_thread.is_alive():
                        self.logger.warning(
                            "Previous shell reader thread did not stop in time"
                        )
                    self._process_thread = None

                self.client = paramiko.client.SSHClient()
                self.client.set_missing_host_key_policy(paramiko.client.AutoAddPolicy())
                port = self.server_config.get('port', 22)

                if private_key is None:
                    if 'password' in self.server_config:
                        self.client.connect(
                            str(self.server_config['server_ip']),
                            port=port,
                            username=self.server_config['username'],
                            password=self.server_config['password'],
                            timeout=60,
                        )
                    elif self.default_private_key:
                        if not os.path.exists(self.default_private_key):
                            raise FileNotFoundError(
                                f"Default private key file "
                                f"{self.default_private_key} does not exist"
                            )
                        self.client.connect(
                            self.server_config['server_ip'],
                            port=port,
                            username=self.server_config['username'],
                            key_filename=self.default_private_key,
                            timeout=60,
                        )
                    else:
                        # Let paramiko fall back to ~/.ssh keys / agent
                        self.client.connect(
                            self.server_config['server_ip'],
                            port=port,
                            username=self.server_config['username'],
                            timeout=60,
                        )
                else:
                    if not os.path.exists(private_key):
                        raise FileNotFoundError(
                            f"Private key file {private_key} does not exist"
                        )
                    self.client.connect(
                        self.server_config['server_ip'],
                        port=port,
                        username=self.server_config['username'],
                        key_filename=private_key,
                        timeout=60,
                    )

                # Set up SCP client (lazy — only if `scp` is installed)
                try:
                    from scp import SCPClient  # pylint: disable=import-outside-toplevel
                    self.scp_client = SCPClient(self.client.get_transport())
                except ImportError:
                    self.scp_client = None

                self.shell = self.client.invoke_shell(
                    term='xterm', width=120, height=40
                )
                self._process_stop_event.clear()
                self._process_thread = threading.Thread(
                    target=self._read_shell_output
                )
                self._process_thread.daemon = True
                self._process_thread.start()
                self.logger.info(
                    f"Connected to server at "
                    f"'{self.server_config['server_ip']}' "
                    f"via port '{self.server_config.get('port', 22)}'"
                )
                break

            except (
                paramiko.ssh_exception.BadHostKeyException,
                paramiko.ssh_exception.AuthenticationException,
                paramiko.ssh_exception.SSHException,
                paramiko.ssh_exception.NoValidConnectionsError,
                socket.timeout,
                TimeoutError,
                OSError,
            ) as e:
                self.logger.error(
                    f"Connection attempt {retries + 1} failed: {e}"
                )
                retries += 1
                if retries > max_retries:
                    self.logger.error(
                        f"Failed to connect to server at "
                        f"'{self.server_config['server_ip']}' after "
                        f"{max_retries} retries"
                    )
                    self.client = None
                    break
                self.logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)

    def disconnect(self):
        """
        Close the SSH session.

        .. note::
            The command cache is **preserved** at the class level across
            reconnections.  Call :meth:`clear_cache` explicitly if you want
            to discard cached outputs.
        """
        self.logger.info(
            f"Closing connection to {self.server_config['server_ip']}"
        )
        if self._process_thread is not None and self._process_thread.is_alive():
            self._process_stop_event.set()
            self._process_thread.join(timeout=2.0)
            self._process_thread = None
        if self.scp_client is not None:
            try:
                self.scp_client.close()
            except Exception:  # pylint: disable=broad-except
                pass
            self.scp_client = None
        if self.client is not None:
            self.client.close()

    def open_shell(self):
        """
        Open an interactive shell channel on the remote server.

        Typically called automatically by :meth:`connect`, but can be
        invoked manually if the shell was closed.
        """
        try:
            self.logger.info(
                f"Opening shell on '{self.server_config['server_ip']}' "
                f"port '{self.server_config.get('port', 22)}'"
            )
            if not self.shell:
                self.shell = self.client.invoke_shell()
        except paramiko.ssh_exception.SSHException as e:
            self.logger.error(f"Failed to open shell: {e}")
            raise

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def execute_command(self, command, use_cache=False):
        """
        Execute a command on the remote server via ``exec_command``.

        Args:
            command (str): The command to execute.
            use_cache (bool): If ``True``, return cached output when
                available.  Caching is automatically disabled for commands
                that appear to contain credentials.

        Returns:
            str: The command's stdout output.
        """
        log_cmd = _redact_command_for_log(command)
        skip_cache = _command_likely_contains_secrets(command)
        if skip_cache:
            use_cache = False

        if use_cache:
            cache_key = f"remote:{command}"
            cached = self._get_cached(cache_key)
            if cached is not None:
                self.logger.info(
                    f"[CACHE HIT] [{self.server_config['server_ip']}] "
                    f"Returning cached output for: {log_cmd}"
                )
                return cached

        try:
            self.logger.info(f"Executing command: {log_cmd}")
            _, stdout, _ = self.client.exec_command(command)
            output = stdout.read().decode('utf-8')
            if use_cache:
                cache_key = f"remote:{command}"
                self._set_cached(cache_key, output)
            return output
        except subprocess.SubprocessError as e:
            self.logger.error(f"A subprocess error occurred: {e}")
            raise

    def execute_via_jump_host(self, target_config: dict, command: str,
                              use_cache=False) -> str:
        """
        Execute a command on a *target* host by SSH-ing through this server
        as a jump host.

        This method uses ``sshpass`` on the intermediate server, installing
        it automatically if possible.

        Args:
            target_config (dict): Target host connection details::

                {
                    'target_ip': '10.0.0.5',       # required
                    'target_username': 'user',      # required
                    'target_password': 'pass',      # required
                    'target_port': 22               # optional, default 22
                }

            command (str): Command to execute on the target host.
            use_cache (bool): Return cached output when available.

        Returns:
            str: Cleaned command output.

        Raises:
            ConnectionError: If the jump-host connection is not established.
            ValueError: If required target_config keys are missing.
            RuntimeError: If ``sshpass`` is unavailable and installation fails.
        """
        # pylint: disable=too-many-locals,too-many-branches
        if self.client is None:
            self.logger.error(
                "SSH connection is not established. Call connect() first."
            )
            raise ConnectionError("SSH connection is not established")

        required = ['target_ip', 'target_username', 'target_password']
        missing = [p for p in required if p not in target_config]
        if missing:
            self.logger.error(f"Missing required target_config keys: {missing}")
            raise ValueError(f"Missing required target_config keys: {missing}")

        target_ip = target_config['target_ip']
        target_username = target_config['target_username']
        target_password = target_config['target_password']
        target_port = target_config.get('target_port', 22)

        self.logger.info(
            f"Executing via jump host: "
            f"{self.server_config['server_ip']} -> {target_ip}:{target_port}"
        )
        self.logger.info(f"Target username: {target_username}")
        self.logger.info(f"Command: {command}")

        cache_key = f"nested_ssh:{target_ip}:{target_port}:{command}"
        if use_cache:
            cached = self._get_cached(cache_key)
            if cached is not None:
                self.logger.info(
                    f"[CACHE HIT] [{self.server_config['server_ip']}] "
                    f"Returning cached output for nested SSH command: {command}"
                )
                return cached

        try:
            # Check / install sshpass on the jump host
            self.logger.info("Checking for sshpass on jump host...")
            check_cmd = ("which sshpass 2>/dev/null || "
                         "command -v sshpass 2>/dev/null")
            check_output = self.execute_command(check_cmd,
                                                use_cache=False).strip()

            if not check_output:
                self.logger.warning(
                    "sshpass not found. Attempting installation..."
                )
                install_attempts = [
                    "apk add -q sshpass 2>&1",
                    "apt-get update -qq && apt-get install -y -qq sshpass 2>&1",
                    "yum install -y -q sshpass 2>&1",
                    "dnf install -y -q sshpass 2>&1",
                ]
                installed = False
                for install_cmd in install_attempts:
                    try:
                        self.execute_command(install_cmd, use_cache=False)
                        verify = self.execute_command(
                            check_cmd, use_cache=False
                        ).strip()
                        if verify:
                            self.logger.info("sshpass successfully installed")
                            installed = True
                            break
                    except Exception as install_err:  # pylint: disable=broad-except
                        self.logger.debug(
                            f"Installation attempt failed: {install_err}"
                        )
                        continue
                if not installed:
                    error_msg = (
                        "sshpass is not available and installation failed. "
                        "Please install sshpass manually on the jump host."
                    )
                    self.logger.error(error_msg)
                    raise RuntimeError(error_msg)
            else:
                self.logger.info(f"sshpass found at: {check_output}")

            escaped_password = shlex.quote(target_password)
            escaped_command = shlex.quote(command)

            ssh_command = (
                f"sshpass -p '{escaped_password}' "
                f"ssh -o StrictHostKeyChecking=no "
                f"-o UserKnownHostsFile=/dev/null "
                f"-o ConnectTimeout=30 "
                f"-o LogLevel=ERROR "
                f"-p {target_port} "
                f"{target_username}@{target_ip} "
                f"'{escaped_command}'"
            )

            self.logger.info(
                "Executing SSH command through jump host to target..."
            )
            output = self.execute_command(ssh_command, use_cache=False)

            if output is None:
                self.logger.warning("Command execution returned None")
                return ""

            cleaned_output = output.strip()
            self.logger.info(
                f"Command executed successfully. "
                f"Output length: {len(cleaned_output)} characters"
            )

            if len(cleaned_output) <= 500:
                self.logger.debug(f"Command output: {cleaned_output}")
            else:
                self.logger.debug(
                    f"Command output (first 500 chars): "
                    f"{cleaned_output[:500]}..."
                )

            if use_cache:
                self._set_cached(cache_key, cleaned_output)
            return cleaned_output

        except (ValueError, ConnectionError):
            raise
        except Exception as e:
            error_msg = f"Failed to execute nested SSH command: {e}"
            self.logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    # ------------------------------------------------------------------
    # Server reboot
    # ------------------------------------------------------------------

    def server_hard_reboot(self, reboot_command='reboot',
                           reconnect_timeout=300):
        """
        Send a reboot command and wait for the server to come back online.

        Args:
            reboot_command (str): The shell command used to trigger a reboot
                (default ``'reboot'``).  Override if your environment needs
                ``sudo reboot``, ``shutdown -r now``, etc.
            reconnect_timeout (int): Maximum seconds to wait for
                reconnection (default 300 — i.e. 5 minutes).

        Returns:
            bool: ``True`` if reconnection succeeded, ``False`` otherwise.
        """
        self.execute_command(reboot_command)
        self.logger.info(
            f"Reboot command sent to {self.server_config['server_ip']}"
        )

        elapsed = 0
        while elapsed < reconnect_timeout:
            try:
                self.connect()
                self.logger.info("Reconnected to server after reboot")
                return True
            except (
                paramiko.ssh_exception.SSHException,
                paramiko.ssh_exception.NoValidConnectionsError,
                socket.timeout,
                OSError,
            ):
                self.logger.info("Reconnection failed, retrying...")
                time.sleep(5)
                elapsed += 5

        self.logger.error(
            f"Reconnection failed after {reconnect_timeout}s. "
            f"Manual intervention may be required."
        )
        return False

    # ------------------------------------------------------------------
    # Reachability
    # ------------------------------------------------------------------

    def ping(self):
        """
        Test connectivity to the remote server using ICMP ping.

        Returns:
            bool: ``True`` if the server is reachable, ``False`` otherwise.
        """
        if self.reachable:
            self.logger.info(
                f"Server {self.server_config['server_ip']} already verified "
                f"as reachable. Skipping ping test..."
            )
            return True

        cmd = f"ping -c 4 {self.server_config['server_ip']}"
        with Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE) as process:
            stdout, stderr = process.communicate()
            if stderr:
                self.logger.error(
                    f"Ping test error: {stderr.decode('utf-8')}"
                )
                return False
            out = self.parse_output(stdout.decode("utf-8"))
            self.logger.info(f"Ping output:\n{out}")
            if process.returncode != 0:
                self.logger.info(
                    f"Server {self.server_config['server_ip']} is not reachable"
                )
                return False
            self.logger.info(
                f"Server {self.server_config['server_ip']} is reachable"
            )
            self.reachable = True
            return True

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def read_remote_file(self, remote_file_path):
        """
        Read the contents of a file on the remote server via SFTP.

        Args:
            remote_file_path (str): Absolute path on the remote host.

        Returns:
            bytes | None: File contents, or ``None`` on failure.
        """
        sftp = None
        try:
            sftp = self.client.open_sftp()
            self.logger.info(f"Reading remote file '{remote_file_path}'...")
            with sftp.open(remote_file_path, 'r') as remote_file:
                file_content = remote_file.read()
                self.logger.info(
                    f"Successfully read {len(file_content)} bytes "
                    f"from '{remote_file_path}'"
                )
            return file_content
        except paramiko.AuthenticationException:
            self.logger.error(
                "Authentication failed. Verify your credentials."
            )
        except socket.timeout:
            self.logger.error("Connection timed out.")
        except FileNotFoundError:
            self.logger.error(
                f"File {remote_file_path} not found on remote host."
            )
        except IOError as io_error:
            self.logger.error(f"Failed to read the file: {io_error}")
        except paramiko.SSHException as e:
            self.logger.error(f"An SSH error occurred: {e}")
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except paramiko.SSHException as e:
                    self.logger.error(f"Error closing SFTP: {e}")
        return None

    def upload_file(self, local_path, remote_path):
        """
        Upload a local file to the remote server via SCP.

        Args:
            local_path (str): Path to the local file.
            remote_path (str): Destination path on the remote server.

        Raises:
            RuntimeError: If the ``scp`` package is not installed.
        """
        if self.scp_client is None:
            try:
                from scp import SCPClient  # pylint: disable=import-outside-toplevel
                self.scp_client = SCPClient(self.client.get_transport())
            except ImportError as exc:
                raise RuntimeError(
                    "The 'scp' package is required for file transfers. "
                    "Install it with: pip install scp"
                ) from exc

        self.logger.info(f"Uploading '{local_path}' -> '{remote_path}'")
        self.scp_client.put(local_path, remote_path)
        self.logger.info("Upload complete")

    def download_file(self, remote_path, local_path):
        """
        Download a file from the remote server via SCP.

        Args:
            remote_path (str): Path on the remote server.
            local_path (str): Local destination path.

        Raises:
            RuntimeError: If the ``scp`` package is not installed.
        """
        if self.scp_client is None:
            try:
                from scp import SCPClient  # pylint: disable=import-outside-toplevel
                self.scp_client = SCPClient(self.client.get_transport())
            except ImportError as exc:
                raise RuntimeError(
                    "The 'scp' package is required for file transfers. "
                    "Install it with: pip install scp"
                ) from exc

        self.logger.info(f"Downloading '{remote_path}' -> '{local_path}'")
        self.scp_client.get(remote_path, local_path)
        self.logger.info("Download complete")

    # ------------------------------------------------------------------
    # Shell output helpers
    # ------------------------------------------------------------------

    def _read_shell_output(self):
        """
        Background thread that reads from the interactive shell channel and
        appends data to ``fulldata`` / ``strdata``.
        """
        while not self._process_stop_event.is_set():
            if self.shell is not None and self.shell.recv_ready():
                alldata = self.shell.recv(1024)
                while self.shell.recv_ready():
                    alldata += self.shell.recv(1024)
                self.strdata = self.strdata + str(alldata)
                self.fulldata = self.fulldata + str(alldata)
            else:
                self._process_stop_event.wait(timeout=0.1)

    @staticmethod
    def print_lines(data):
        """
        Debug helper — return the last line from streamed data.

        Args:
            data (str): Raw streamed output.

        Returns:
            str: The last line of the data.
        """
        last_line = data
        if '\n' in data:
            lines = data.splitlines()
            last_line = lines[-1]
            if data.endswith('\n'):
                last_line = ''
        return last_line

    @staticmethod
    def escape_ansi(input_string):
        """
        Remove ANSI escape sequences and cursor-position reports from a string.

        Args:
            input_string (str): Raw string potentially containing ANSI codes.

        Returns:
            str: Cleaned string.
        """
        ansi_escape = re.compile(r'''
            \x1B
            (?:
                [@-Z\\-_]
            |
                \[
                [0-?]*
                [ -/]*
                [@-~]
            )
        ''', re.VERBOSE)

        device_status_report = re.compile(r'\x1B\[\d*;\d*[A-Za-z]')
        simple_dsr = re.compile(r'\x1B\[\d*[A-Za-z]')

        cleaned = ansi_escape.sub('', input_string)
        cleaned = device_status_report.sub('', cleaned)
        cleaned = simple_dsr.sub('', cleaned)
        return cleaned.strip()

    def parse_output(self, output):
        """
        Parse raw shell output into a human-readable format.

        Strips byte-string artefacts, ANSI codes, and carriage returns.

        Args:
            output (str): Raw output from the shell.

        Returns:
            str: Cleaned, readable output.
        """
        out_string = output.replace('\r', '')
        result = re.sub('[\"]', '', out_string)
        result = '\n'.join(result.split("\\n"))
        for item in ["'b", "b'", "'b'"]:
            result = result.replace(item, '')
        out_list = []
        for line in result.split('\n'):
            if line != "\r":
                out = self.escape_ansi(line.replace('\\r', ''))
                out_list.append(out)
        return '\n'.join(out_list)


# ---------------------------------------------------------------------------
# Usage example
# ---------------------------------------------------------------------------
#
# import logging
#
# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger("ssh_utility")
#
# config = {
#     'server_ip': '192.168.0.55',
#     'port': 22,
#     'username': 'admin',
#     'password': 'admin',
# }
#
# server = RemoteServer.get_instance(config, logger)
# server.connect()
#
# # Execute a command (cached)
# output = server.execute_command("cat /etc/hostname", use_cache=True)
# print(output)
#
# # Execute again — served from cache
# output = server.execute_command("cat /etc/hostname", use_cache=True)
#
# # Jump-host execution
# target = {
#     'target_ip': '10.0.0.5',
#     'target_username': 'user',
#     'target_password': 'pass',
# }
# result = server.execute_via_jump_host(target, "whoami")
#
# # File transfer
# server.upload_file("/tmp/local_file.txt", "/tmp/remote_file.txt")
# server.download_file("/tmp/remote_file.txt", "/tmp/downloaded.txt")
#
# server.disconnect()
