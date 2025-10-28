import socket
import struct
import ipaddress
import serial
import serial.tools.list_ports
import threading
import time
import json
import sys
import os
import logging
import subprocess
import shlex
from logging.handlers import RotatingFileHandler
from colorama import Fore, Style, init as colorama_init

# --- CONFIGURATION ---
CONFIG_FILE = 'wifi_config.json'
LOG_FILE = 'scan_log.txt'
BAUD_RATE = 115200

# --- STATUS CODES (from ESP32 reports) ---
MAGIC_BYTE = 0xAB
STATUS_MAP = {
    10: "WIFI_CONNECT_SUCCESS",
    11: "WIFI_CONNECT_FAILURE",
    15: "SCANNING_TARGET",
    16: "DEVICE_READY",
    1: "TARGET_UNREACHABLE",
    2: "PORT_OPEN",
    3: "SERVICE_NO_RESPONSE",
    4: "SERVICE_RESPONDED",
    5: "SCAN_CYCLE_START",
    6: "SCAN_CYCLE_END"
}

STATUS_COLORS = {
    "WIFI_CONNECT_SUCCESS": Fore.GREEN,
    "WIFI_CONNECT_FAILURE": Fore.RED,
    "SCANNING_TARGET": Fore.BLUE,
    "DEVICE_READY": Fore.GREEN,
    "TARGET_UNREACHABLE": Fore.RED,
    "PORT_OPEN": Fore.YELLOW,
    "SERVICE_NO_RESPONSE": Fore.LIGHTRED_EX,
    "SERVICE_RESPONDED": Fore.GREEN,
    "SCAN_CYCLE_START": Fore.CYAN,
    "SCAN_CYCLE_END": Fore.CYAN,
}

COMMAND_COLOR = Fore.CYAN
DESC_COLOR = Fore.LIGHTWHITE_EX
HIGHLIGHT_COLOR = Fore.LIGHTYELLOW_EX
WARNING_COLOR = Fore.LIGHTRED_EX

DEFAULT_PROMPT_DELAY = 0.8

def register_status_waiter(statuses):
    event = threading.Event()
    with status_waiters_lock:
        status_waiters.append({"statuses": set(statuses), "event": event})
    return event

def unregister_status_waiter(event):
    with status_waiters_lock:
        status_waiters[:] = [w for w in status_waiters if w["event"] is not event]

def notify_status_waiters(status):
    with status_waiters_lock:
        triggered = [w for w in status_waiters if status in w["statuses"]]
        status_waiters[:] = [w for w in status_waiters if w not in triggered]
    for waiter in triggered:
        waiter["event"].set()

def hold_prompt(delay: float = DEFAULT_PROMPT_DELAY):
    """Temporarily hide the prompt to let device output surface first."""
    prompt_ready_event.clear()

    def _release():
        prompt_ready_event.set()

    timer = threading.Timer(delay, _release)
    timer.daemon = True
    timer.start()

def release_prompt():
    """Immediately re-enable the prompt."""
    prompt_ready_event.set()

def hold_prompt_until_status(statuses, timeout=None, fallback_delay=DEFAULT_PROMPT_DELAY):
    """Hold the prompt until one of the provided statuses arrives or timeout elapses."""
    event = register_status_waiter(statuses)
    prompt_ready_event.clear()

    def wait_and_release():
        triggered = event.wait(timeout)
        unregister_status_waiter(event)
        if not triggered and fallback_delay:
            time.sleep(fallback_delay)
        prompt_ready_event.set()

    threading.Thread(target=wait_and_release, daemon=True).start()

# --- LOGGING ---
colorama_init(autoreset=True)

class ColorFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        color = getattr(record, "color", "")
        reset = Style.RESET_ALL if color else ""
        return f"{color}{message}{reset}"

logger = logging.getLogger("esp32_controller")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fmt = "[%(asctime)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(ColorFormatter(fmt, datefmt))
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_048_576, backupCount=3)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt))
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)

# --- GLOBAL VARIABLES ---
esp32_serial_port = None
stop_event = threading.Event()
prompt_ready_event = threading.Event()
prompt_ready_event.set()
status_waiters = []
status_waiters_lock = threading.Lock()
PROMPT = f"{Fore.GREEN}esp32 > {Style.RESET_ALL}"

def log_message(message, level=logging.INFO, color=None):
    """Logs a message to both stdout and the rotating log file."""
    if color:
        logger.log(level, message, extra={"color": color})
    else:
        logger.log(level, message)

def is_serial_ready():
    return esp32_serial_port is not None and esp32_serial_port.is_open

def send_serial_command(command: str):
    if not is_serial_ready():
        raise RuntimeError("ESP32 is not connected.")
    if not command.endswith("\n"):
        command += "\n"
    esp32_serial_port.write(command.encode())

def print_help():
    commands = [
        ("help", "Show this message."),
        ("join -s <ssid> -p <pass>", "Connect using provided credentials."),
        ("join -i <index>", "Connect to a saved network."),
        ("scan -all", "Scan the current subnet."),
        ("scan -t <ipv4>", "Probe a single IPv4 host."),
        ("networks", "List saved networks."),
        ("randomize_mac", "Randomise MAC before next join."),
        ("status", "Show ESP32 connection status."),
        ("ipconfig", "Display local host interface info."),
        ("reboot", "Reboot the ESP32 device."),
        ("clear", "Clear the console screen."),
        ("exit", "Stop the controller."),
    ]
    print()
    print(f"{HIGHLIGHT_COLOR}Available Commands:{Style.RESET_ALL}")
    for command, description in commands:
        print(f"  {COMMAND_COLOR}{command:<28}{Style.RESET_ALL}{DESC_COLOR}{description}{Style.RESET_ALL}")
    print()

def load_wifi_config():
    """Loads Wi-Fi configurations from a JSON file."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_wifi_config(config):
    """Saves Wi-Fi configurations to a JSON file."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def find_esp32_port():
    """Finds the COM port for the ESP32 device."""
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if "CP210x" in port.description or "CH340" in port.description or "USB-SERIAL" in port.description:
            log_message(f"ESP32 found on {port.device}")
            return port.device
    return None

def serial_reader_thread():
    """Thread to continuously read from the ESP32 serial port."""
    global esp32_serial_port, prompt_ready_event
    report_size = struct.calcsize('<IB')
    buffer = bytearray()
    min_chunk = report_size + 1

    while not stop_event.is_set():
        if esp32_serial_port and esp32_serial_port.is_open:
            try:
                waiting = getattr(esp32_serial_port, "in_waiting", 0) or min_chunk
                chunk = esp32_serial_port.read(waiting)
                if not chunk:
                    continue
                buffer.extend(chunk)

                while buffer:
                    if buffer[0] == MAGIC_BYTE:
                        if len(buffer) < 1 + report_size:
                            break
                        packet = bytes(buffer[1:1 + report_size])
                        del buffer[:1 + report_size]

                        target_ip_int, status_code = struct.unpack('<IB', packet)
                        target_ip_int_swapped = socket.htonl(target_ip_int)
                        ip_str = str(ipaddress.IPv4Address(target_ip_int_swapped))
                        status_str = STATUS_MAP.get(status_code, f"UNKNOWN ({status_code})")

                        color = STATUS_COLORS.get(status_str)

                        if status_code == 10:
                            log_message(f"ESP32 connected to Wi-Fi. IP: {ip_str}", color=color)
                        elif status_code == 11:
                            log_message("ESP32 failed to connect to Wi-Fi.", logging.WARNING, color=color or Fore.RED)
                        elif status_code == 15:
                            log_message(f"Scanning target {ip_str}", color=color or Fore.BLUE)
                        elif ip_str == "0.0.0.0":
                            if status_str == "SCAN_CYCLE_START":
                                log_message("[SCAN] Cycle started", color=color or Fore.CYAN)
                            elif status_str == "SCAN_CYCLE_END":
                                log_message("[SCAN] Cycle completed", color=color or Fore.CYAN)
                            elif status_str == "DEVICE_READY":
                                log_message("ESP32 ready for commands.", color=color or Fore.GREEN)
                                with status_waiters_lock:
                                    waiting = len(status_waiters)
                                if waiting == 0:
                                    release_prompt()
                            else:
                                log_message(f"ESP32 REPORT: {status_str}", color=color)
                        else:
                            message = f"ESP32 REPORT: TARGET {ip_str} -> {status_str}"
                            level = logging.INFO
                            if status_str == "PORT_OPEN":
                                message = f"[SCAN] {ip_str} | TCP/445 open"
                            elif status_str == "SERVICE_RESPONDED":
                                message = f"[SCAN] {ip_str} | SMB negotiation successful"
                            elif status_str == "SERVICE_NO_RESPONSE":
                                message = f"[SCAN] {ip_str} | No SMB response"
                                level = logging.WARNING
                            elif status_str == "TARGET_UNREACHABLE":
                                message = f"[SCAN] {ip_str} | Host unreachable"
                                level = logging.WARNING
                            log_message(message, level=level, color=color)

                        notify_status_waiters(status_str)
                    else:
                        newline_idx = buffer.find(b'\n')
                        if newline_idx == -1:
                            if len(buffer) > 2048:
                                del buffer[:-256]
                            break
                        line = buffer[:newline_idx + 1]
                        del buffer[:newline_idx + 1]
                        message = line.decode('utf-8', errors='ignore').strip()
                        if message:
                            log_message(f"ESP32 DEBUG: {message}")
            except (serial.SerialException, ConnectionResetError):
                log_message("ESP32 disconnected.", logging.WARNING)
                if esp32_serial_port:
                    esp32_serial_port.close()
                esp32_serial_port = None
            except Exception as e:
                log_message(f"Error in serial reader: {e}", logging.ERROR)
        else:
            buffer.clear()
            stop_event.wait(0.5)

def handle_user_commands():
    """Handles user input for sending commands to the ESP32."""
    global esp32_serial_port, prompt_ready_event
    wifi_config = load_wifi_config()

    def refresh_wifi_config():
        nonlocal wifi_config
        wifi_config = load_wifi_config()
        return wifi_config

    def clear_console():
        command = "cls" if os.name == "nt" else "clear"
        subprocess.call(command, shell=True)

    def cmd_help(_args):
        print_help()
        hold_prompt()
        return

    def cmd_exit(_args):
        log_message("Exit requested. Stopping server...")
        stop_event.set()
        release_prompt()
        return

    def cmd_clear(_args):
        clear_console()
        hold_prompt(0.3)
        return

    def cmd_status(_args):
        state_lines = []
        if is_serial_ready():
            state_lines.append(f"Serial port: {Fore.GREEN}connected{Style.RESET_ALL} ({esp32_serial_port.port})")
        else:
            state_lines.append(f"Serial port: {Fore.RED}disconnected{Style.RESET_ALL}")
        wifi_state = refresh_wifi_config()
        state_lines.append(f"Saved networks: {Fore.CYAN}{len(wifi_state)}{Style.RESET_ALL}")
        print(f"\n{HIGHLIGHT_COLOR}Status:{Style.RESET_ALL}")
        for line in state_lines:
            print(f"  {line}")
        hold_prompt()
        return

    def cmd_networks(_args):
        saved = refresh_wifi_config()
        if not saved:
            print(f"{WARNING_COLOR}No networks saved.{Style.RESET_ALL}")
            hold_prompt()
            return
        print(f"{HIGHLIGHT_COLOR}Saved networks:{Style.RESET_ALL}")
        for index, ssid in enumerate(saved):
            print(f"  {Fore.MAGENTA}[{index}]{Style.RESET_ALL} {COMMAND_COLOR}{ssid}{Style.RESET_ALL}")
        hold_prompt()
        return

    def cmd_join(args):
        nonlocal wifi_config
        saved = refresh_wifi_config()
        ssid = None
        password = None
        index = None

        it = iter(args)
        for token in it:
            if token in ("-i", "--index"):
                try:
                    index = int(next(it))
                except (StopIteration, ValueError):
                    print(f"{WARNING_COLOR}Usage: join -i <index>{Style.RESET_ALL}")
                    hold_prompt()
                    return
            elif token in ("-s", "--ssid"):
                try:
                    ssid = next(it)
                except StopIteration:
                    print(f"{WARNING_COLOR}Usage: join -s <ssid> -p <password>{Style.RESET_ALL}")
                    hold_prompt()
                    return
            elif token in ("-p", "--password"):
                try:
                    password = next(it)
                except StopIteration:
                    print(f"{WARNING_COLOR}Usage: join -s <ssid> -p <password>{Style.RESET_ALL}")
                    hold_prompt()
                    return
            else:
                print(f"{WARNING_COLOR}Unknown option: {token}{Style.RESET_ALL}")
                hold_prompt()
                return

        if index is not None:
            ssid_list = list(saved.keys())
            if not ssid_list:
                print(f"{WARNING_COLOR}No saved networks available.{Style.RESET_ALL}")
                hold_prompt()
                return
            if 0 <= index < len(ssid_list):
                ssid = ssid_list[index]
                password = saved[ssid]
                log_message(f"Joining saved network [{index}]: {ssid}")
            else:
                print(f"{WARNING_COLOR}Error: Index out of bounds.{Style.RESET_ALL}")
                hold_prompt()
                return
        elif ssid and password:
            if saved.get(ssid) != password:
                saved[ssid] = password
                save_wifi_config(saved)
                wifi_config = saved
                log_message(f"Network '{ssid}' saved.")
            log_message(f"Joining new network: {ssid}")
        else:
            print(f"{WARNING_COLOR}Usage:\n  join -s <ssid> -p <password>\n  join -i <index>{Style.RESET_ALL}")
            hold_prompt()
            return

        try:
            send_serial_command(f"join {ssid} {password}")
        except RuntimeError as exc:
            log_message(str(exc), logging.WARNING)
            hold_prompt()
            return
        hold_prompt_until_status(["WIFI_CONNECT_SUCCESS", "WIFI_CONNECT_FAILURE"], timeout=25, fallback_delay=0.5)
        return

    def cmd_scan(args):
        if not args:
            print(f"{WARNING_COLOR}Usage: scan -all | scan -t <ipv4>{Style.RESET_ALL}")
            hold_prompt()
            return
        if args[0] in ("-all", "--all"):
            try:
                send_serial_command("scan -all")
                log_message("Requested full subnet scan.", color=Fore.CYAN)
            except RuntimeError as exc:
                log_message(str(exc), logging.WARNING)
                hold_prompt()
                return
            hold_prompt_until_status(["SCAN_CYCLE_END"], timeout=180, fallback_delay=1.0)
            return
        if args[0] == "-t" and len(args) > 1:
            target_ip = args[1]
            try:
                ipaddress.IPv4Address(target_ip)
            except ipaddress.AddressValueError:
                print(f"{WARNING_COLOR}Error: Invalid IPv4 address.{Style.RESET_ALL}")
                hold_prompt()
                return
            try:
                send_serial_command(f"scan -t {target_ip}")
                log_message(f"Requested targeted scan: {target_ip}", color=Fore.MAGENTA)
            except RuntimeError as exc:
                log_message(str(exc), logging.WARNING)
                hold_prompt()
                return
            hold_prompt_until_status(["SCAN_CYCLE_END"], timeout=60, fallback_delay=0.8)
            return
        print(f"{WARNING_COLOR}Usage: scan -all | scan -t <ipv4>{Style.RESET_ALL}")
        hold_prompt()
        return

    def cmd_randomize_mac(_args):
        try:
            send_serial_command("randomize_mac")
            log_message("Requested MAC randomisation.", color=Fore.CYAN)
        except RuntimeError as exc:
            log_message(str(exc), logging.WARNING)
            hold_prompt()
            return
        hold_prompt(0.6)
        return

    def cmd_reboot(_args):
        try:
            send_serial_command("reboot")
        except RuntimeError as exc:
            log_message(str(exc), logging.WARNING)
            hold_prompt()
            return
        clear_console()
        log_message("Reboot command sent. Waiting for ESP32 to reconnect...", color=Fore.CYAN)
        hold_prompt_until_status(["DEVICE_READY"], timeout=20, fallback_delay=1.0)
        return

    def cmd_ipconfig(_args):
        print()
        try:
            subprocess.run(["ipconfig"], check=False)
        except FileNotFoundError:
            print(f"{WARNING_COLOR}ipconfig command not found.{Style.RESET_ALL}")
            hold_prompt()
            return
        hold_prompt(1.2)
        return

    command_map = {
        "help": cmd_help,
        "exit": cmd_exit,
        "quit": cmd_exit,
        "q": cmd_exit,
        "clear": cmd_clear,
        "cls": cmd_clear,
        "status": cmd_status,
        "networks": cmd_networks,
        "join": cmd_join,
        "scan": cmd_scan,
        "randomize_mac": cmd_randomize_mac,
        "randomise_mac": cmd_randomize_mac,
        "reboot": cmd_reboot,
        "ipconfig": cmd_ipconfig,
    }

    print_help()
    hold_prompt(1.0)

    while not stop_event.is_set():
        prompt_ready_event.wait()

        try:
            raw_input = input(PROMPT)
        except EOFError:
            stop_event.set()
            break
        except KeyboardInterrupt:
            print()
            continue

        if not raw_input.strip():
            continue

        try:
            tokens = shlex.split(raw_input)
        except ValueError as exc:
            print(f"{WARNING_COLOR}Parse error: {exc}{Style.RESET_ALL}")
            hold_prompt()
            continue

        if not tokens:
            continue

        command = tokens[0].lower()
        handler = command_map.get(command)
        if not handler:
            print(f"{WARNING_COLOR}Unknown command. Type 'help' for a list of commands.{Style.RESET_ALL}")
            hold_prompt()
            continue

        try:
            handler(tokens[1:])
        except Exception as exc:
            log_message(f"Command '{command}' failed: {exc}", logging.ERROR)
            hold_prompt()

    log_message("Command handler stopped.")

def main():
    """Main function to start the server and threads."""
    global esp32_serial_port
    
    log_message("ESP32 controller initializing...")
    
    reader_thread = threading.Thread(target=serial_reader_thread)
    reader_thread.daemon = True
    reader_thread.start()

    command_thread = threading.Thread(target=handle_user_commands)
    command_thread.daemon = True
    command_thread.start()

    try:
        while not stop_event.is_set():
            if not esp32_serial_port or not esp32_serial_port.is_open:
                port_name = find_esp32_port()
                if port_name:
                    try:
                        esp32_serial_port = serial.Serial(port_name, BAUD_RATE, timeout=0.5)
                        esp32_serial_port.reset_input_buffer()
                        esp32_serial_port.reset_output_buffer()
                        log_message(f"Connected to ESP32 on {port_name}.")
                        time.sleep(1.5)
                    except serial.SerialException as e:
                        log_message(f"Failed to connect to {port_name}: {e}", logging.ERROR)
                        esp32_serial_port = None
                if not esp32_serial_port:
                    stop_event.wait(5)
                    continue
            stop_event.wait(1)
    except KeyboardInterrupt:
        log_message("Server shutting down by user command.")
    finally:
        stop_event.set()
        if esp32_serial_port and esp32_serial_port.is_open:
            esp32_serial_port.close()
        command_thread.join(timeout=1.5)
        reader_thread.join(timeout=1.5)
        log_message("Server shut down.")
        sys.exit(0)

if __name__ == '__main__':
    main()
def print_help():
    help_text = (
        "\nAvailable Commands:\n"
        "  help                         - Show this message.\n"
        "  join -s <ssid> -p <pass>     - Connect using provided credentials.\n"
        "  join -i <index>              - Connect to a saved network.\n"
        "  scan -all                    - Scan the current subnet.\n"
        "  scan -t <ipv4>               - Probe a single IPv4 host.\n"
        "  networks                     - List saved networks.\n"
        "  randomize_mac                - Randomise MAC before next join.\n"
        "  status                       - Show ESP32 connection status.\n"
        "  ipconfig                     - Display local host interface info.\n"
        "  clear                        - Clear the console screen.\n"
        "  exit                         - Stop the controller.\n"
    )
    print(help_text)
