"""This module contains the class to handle the remote server operations"""
# pylint: disable=too-many-instance-attributes
import os
import time
import re
import threading
import socket
import subprocess
from subprocess import Popen, PIPE
import paramiko
from scp import SCPClient

paramiko.common.logging.basicConfig(level=paramiko.common.DEBUG)


class RemoteServer:

    """
    A class to handle the remote server operations /
    This class has the following methods:
    - create_device_session
    - close_connection
    - open_shell
    - execute_command_in_node
    - process
    - print_lines
    - node_hard_reboot
    - escape_ansi
    - parse_out
    - dump_command_output_into_txt

    This class has the variables that reference the remote server session, shell object, and /
    the output received from the remote server session socket
    """

    _instances = {}  # Cache: {frozenset(server_vars.items()): (instance, created_time)}

    @classmethod
    def get_instance(cls, server_vars, logger, ttl_minutes=5):
        """
        A class method to get an instance of RemoteServer.

        If an instance already exists for the given server_vars and is not expired,
        it returns the existing instance. Otherwise, it creates a new instance.
        Args:
            server_vars: Dict object of the device variables.
                        Should include: server_ip, port, username, password
            logger: Logger instance for logging
            ttl_minutes: Time to live in minutes for the cached instance
                            Currently, it defaults to 5 minutes.
        Returns:
            RemoteServer instance
        """
        key = frozenset(server_vars.items())
        current_time = time.time()
        instance_info = cls._instances.get(key)

        if instance_info:
            instance, created_time = instance_info
            if current_time - created_time <= ttl_minutes * 60:
                logger.info(f"Reusing existing RemoteServer instance for {server_vars['server_ip']}")
                instance.fulldata = ''
                instance.strdata = ''
                return instance
            logger.info(f"Recreating RemoteServer instance for {server_vars['server_ip']} (expired)")
            instance.close_connection()
            del cls._instances[key]  # Remove expired instance from cache

        # Create new instance
        new_instance = cls(server_vars, logger)
        cls._instances[key] = (new_instance, current_time)
        logger.info(f"Created new RemoteServer instance for {server_vars['server_ip']}")
        return new_instance

    def __init__(self, device_vars, logger):
        """
        Args:
            device_vars: Dict object of the device variables.
                        Should include: server_ip, port, username, password

            Sample dict: {  'server_ip': node_ip_address,
                            'port': 22,
                            'username': username,
                            'password': password
                        }
            logger:
        """
        self.logger = logger
        self.shell = None
        self.client = None
        self.transport = None
        self.fulldata = ''
        self.strdata = ''
        self.node_details = None
        self.device_vars = device_vars
        self.priv_key_path = os.path.join(os.environ.get("ZTAF_HOME"),
                                          'z_components/eve/resources/ztest-ssh-key')
        self.scp = None
        self.reachable = False

    def create_device_session(self, priv_key=None):
        """
        A method to create a session with the remote server
        :return: client, transport
        """
        retries = 0
        delay = 15
        max_retries = 10
        while retries <= max_retries:
            try:
                self.logger.info(f"Connecting to server on ip {self.device_vars['server_ip']}")
                self.logger.info(f"Removing old SSH key entry for {self.device_vars['server_ip']} "
                                 "to avoid conflicts.")
                #subprocess.run(["ssh-keygen", "-R", self.device_vars['server_ip']],
                #               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                self.client = paramiko.client.SSHClient()
                self.client.set_missing_host_key_policy(paramiko.client.AutoAddPolicy())
                port = self.device_vars.get('port', 22)
                if priv_key is None:
                    if 'password' in self.device_vars:
                        self.client.connect(str(self.device_vars['server_ip']),
                                            port=port,
                                            username=self.device_vars['username'],
                                            password=self.device_vars['password'], timeout=60)
                    else:
                        self.client.connect(self.device_vars['server_ip'],
                                            port=port,
                                            username=self.device_vars['username'],
                                            key_filename=self.priv_key_path, timeout=60)
                else:
                    if os.path.exists(priv_key):
                        self.client.connect(self.device_vars['server_ip'],
                                            port=port,
                                            username=self.device_vars['username'],
                                            key_filename=priv_key, timeout=60)
                    else:
                        self.logger.error(f"Private key file {priv_key} does not exist")
                        raise FileNotFoundError(f"Private key file {priv_key} does not exist")
                self.scp = SCPClient(self.client.get_transport())
                self.shell = self.client.invoke_shell(term='xterm', width=120, height=40)\
                    if not self.shell else self.shell
                thread = threading.Thread(target=self.process)
                thread.daemon = True
                thread.start()
                self.logger.info(f"Connected to the host at '{self.device_vars['server_ip']}' "
                                 f"via '{self.device_vars['port']}'")
                break

            except (paramiko.ssh_exception.BadHostKeyException,
                    paramiko.ssh_exception.AuthenticationException,
                    paramiko.ssh_exception.SSHException,
                    paramiko.ssh_exception.NoValidConnectionsError,
                    socket.timeout,
                    TimeoutError,
                    OSError) as e:
                if 'Network is unreachable' in str(e):
                    self.logger.error("Network is unreachable.")
                    retries += 1
                    self.logger.error(f"Attempt '{retries}' failed: {e}. "
                                      f"Retrying again in {delay} seconds...")
                    time.sleep(delay)
                else:
                    self.logger.error(f"Exception occurred while trying to connect to the host: "
                                      f"{e}")
                    retries += 1
                # If the max retries are reached, raise the exception
                if retries == max_retries+1:
                    self.logger.error(f"Failed to connect to the host at "
                                      f"'{self.device_vars['server_ip']}' "
                                      f"via '{self.device_vars['port']}'")
                    self.client = None

    def remote_server_ping_test(self):
        """
        A method to test the connectivity with the remote server using ping
        """
        if self.reachable:
            self.logger.info(f"Already verified that Remote Server {self.device_vars['server_ip']} is reachable. "
                                "Skipping the Ping test...")
            return True
        cmd = f"ping -c 4 {self.device_vars['server_ip']}"
        with Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE) as process:
            stdout, stderr = process.communicate()                                                  #pylint: disable=unused-variable
            if stderr:
                self.logger.error(f"Ping test error: {stderr.decode('utf-8')}")
                return False
            out = self.parse_out(stdout.decode("utf-8"))
            self.logger.info(f"Output of the ping test: \n{out}")
            if process.returncode != 0:
                self.logger.info(f"Remote Server {self.device_vars['server_ip']} is not reachable")
                return False
            self.logger.info(f"Remote Server {self.device_vars['server_ip']} is reachable")
            self.reachable = True
            return True

    def close_connection(self):
        """
        A method to close the session connection with the remote server
        :return: None
        """
        self.logger.info(f"Closing the connection with the host: {self.device_vars['server_ip']}")
        if self.client is not None:
            self.client.close()

    def open_shell(self):
        """
        Invokes shell session on the remote server
        """
        try:
            self.logger.info(f"Opening shell connection to remote host "
                             f"'{self.device_vars['server_ip']}'"
                             f" through port '{self.device_vars['port']}'")
            if not self.shell:
                self.shell = self.client.invoke_shell()
        except paramiko.ssh_exception.SSHException as e:
            self.logger.error(f"Exception occurred while trying to open shell connection: {e}")
            raise e

    def execute_command_in_eve_service(self, service, command):
        """
        Use this method explicitly for any service related ops

        A method to execute a command inside the remote server
        Use this method to run command inside any containers in EVE

        e.g., If you want to run a command inside kube container in EVE,
        say 'kubectl get pods', you can use this method to run the
        following command: 'eve enter kube kubectl get pods'
        :param service: name of the service in which command needs to be executed
        :param command: the command to be executed

        :return: None
        """
        try:
            command = "eve enter " + service + " '" + command + "'"
            self.logger.info(f"Executing command: {command}")
            self.shell.send(command + "\n")
            time.sleep(10)
            return self.get_eve_service_command_output()
        except socket.timeout as e:
            self.logger.error(f"Exception occurred while executing command: {e}")
            raise e

    def get_eve_service_command_output(self):
        """
        A method to get the output of the command executed in the remote server
        """
        return self.parse_out(self.fulldata)

    def execute_command_in_remote_server(self, command):
        """
        A method to execute a command inside the remote server
        :param command: command to be executed

        :return: None
        """

        try:
            self.logger.info(f"Executing command: {command}")
            # Run command
            _, stdout, _ = self.client.exec_command(command)
            output = stdout.read().decode('utf-8')
            return output
        except subprocess.SubprocessError as e:
            self.logger.error(f"A subprocess error occurred: {e}")
            raise e

    def process(self):
        """
        A method to process the data received from the remote server
        This method reads the data from the remote server socket from the self.shell object /
        and stores the data in class variables self.fulldata and self.strdata

        :return: None
        """

        while True:
            # Print data when available
            if self.shell is not None and self.shell.recv_ready():
                alldata = self.shell.recv(1024)
                while self.shell.recv_ready():
                    alldata += self.shell.recv(1024)
                self.strdata = self.strdata + str(alldata)
                self.fulldata = self.fulldata + str(alldata)
                # print all received data except last line
                # self.strdata = self.print_lines(self.strdata)

    @staticmethod
    def print_lines(data):
        """
        Debug method to verify if the data is being received from the remote server
        """
        last_line = data
        if '\n' in data:
            lines = data.splitlines()
            last_line = lines[lines - 1]
            if data.endswith('\n'):
                last_line = ''
        return last_line

    def server_hard_reboot(self):
        """
        A method to hard reboot the remote server /
        This method sends a reboot command to the remote server and waits for the node to reboot /
                and establish connection again

        :return: None
        """

        cmd = 'reboot'
        self.execute_command_in_remote_server(cmd)
        self.logger.info("Reboot requested from remote server")

        total_time = 0
        return_out, client = None, None
        # Wait till node reboot and establish connection again
        while True:
            try:
                self.create_device_session()
                self.logger.info("Reconnected with device after reboot")
                break

            except (paramiko.ssh_exception.SSHException,
                    paramiko.ssh_exception.NoValidConnectionsError,
                    socket.timeout):
                if total_time < 300:
                    self.logger.info("warning", "Reconnection failed, retrying...")
                    time.sleep(5)  # Wait for some time before retrying
                    total_time += 5
                else:
                    self.logger.error("Reconnection failed even after 5 minutes. "
                                      "Manual intervention required. Exiting")
                    break

        return return_out, client

    @staticmethod
    def escape_ansi(input_string):
        """
        Removes ANSI escape sequences and cursor position reports from a string
        """
        # General ANSI escape sequences
        ansi_escape = re.compile(r'''
            \x1B    # ESC
            (?:     # 7-bit C1 Fe (except CSI)
                [@-Z\\-_]
            |       # or [ for CSI, followed by zero or more bytes
                \[
                [0-?]*  # Parameter bytes
                [ -/]*  # Intermediate bytes
                [@-~]   # Final byte
            )
        ''', re.VERBOSE)

        # Also remove cursor position report \x1b[6n etc.
        device_status_report = re.compile(r'\x1B\[\d*;\d*[A-Za-z]')
        simple_dsr = re.compile(r'\x1B\[\d*[A-Za-z]')

        cleaned = ansi_escape.sub('', input_string)
        cleaned = device_status_report.sub('', cleaned)
        cleaned = simple_dsr.sub('', cleaned)
        return cleaned.strip()

    def parse_out(self, output):
        """
        This method parses the output received from the remote server and \
         parses it into a readable format

        :param output: output received from the remote server

        :return: parsed_output
        """
        out_string = output.replace('\r', '')
        result = re.sub("[\"]", '', out_string)
        result = '\n'.join(result.split("\\n"))
        to_be_replaced = ["'b", "b'", "'b'"]
        for item in to_be_replaced:
            result = result.replace(item, '')
        out_list = []

        for line in result.split('\n'):
            if line != "\r":
                out = self.escape_ansi(line.replace('\\r', ''))
                out_list.append(out)
        parsed_output = '\n'.join(out_list)

        return parsed_output

    @staticmethod
    def dump_command_output_into_txt(output):
        """
        This method dumps the output received from the remote server into a text file
        :param output: output received from the remote server
        :return: filepath | Path of the file where the output is dumped
        """
        filename = 'output.txt'
        filepath = os.path.join(os.getcwd(), 'z_components/eve/resources', filename)
        with open(filepath, 'w', encoding="utf8") as file:
            for line in output:
                file.write(line)
        return filepath

    def read_remote_file(self, remote_file_path):
        """
        A method to read the contents of a file on the remote Host
        """
        sftp = None
        try:
            # Use SFTP to open the remote file
            sftp = self.client.open_sftp()

            # Open the file and read its contents
            self.logger.info(f"Reading file from '{remote_file_path}'...")
            with sftp.open(remote_file_path, 'r') as remote_file:
                file_content = remote_file.read()
                self.logger.info(f"File content: {file_content}")
            return file_content

        except paramiko.AuthenticationException:
            self.logger.error("Authentication failed, please verify your credentials.")
        except socket.timeout:
            self.logger.error("Connection timed out.")
        except FileNotFoundError:
            self.logger.error(f"The file {remote_file_path} was not found on the remote host.")
        except IOError as io_error:
            self.logger.error(f"Failed to read the file: {io_error}")
        except paramiko.SSHException as e:
            self.logger.error(f"An unexpected error occurred: {e}")
        finally:
            # Close SFTP connection if it was successfully opened
            if sftp is not None:
                self.logger.info("Closing SFTP connection...")
                try:
                    sftp.close()
                except paramiko.SSHException as e:
                    self.logger.error(f"Error closing SFTP: {e}")
        return None
