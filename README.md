# ESP32 SMB Scanner

This project pairs a desktop companion script with ESP32 firmware to locate hosts that expose the SMB service (TCP/445) on your network. The ESP32 performs lightweight probes and streams results back to the Python controller, which presents an interactive command shell and keeps an audit log.

> ⚠️ **Legal Notice**  
> Run these tools only against networks and systems you own or have been explicitly authorised to test. Even seemingly harmless port scans can be considered intrusive in many jurisdictions. You are responsible for complying with local laws, corporate policies, and any agreements in place. Personal modifications and any malicious use fall entirely outside the author’s responsibility.

## Repository Layout

- `python_server.py` – interactive host controller that manages Wi-Fi credentials, triggers scans, and logs ESP32 reports.
- `esp32_frimware.ino` – ESP32 sketch that connects to Wi-Fi, schedules full-subnet or targeted probes, and publishes concise binary status updates.

## Prerequisites

- Python 3.9 or newer with `pip`
- PySerial (`pip install pyserial`)
- An ESP32 development board (tested at 115200 baud)
- Arduino IDE or PlatformIO (or another toolchain capable of flashing `.ino` sketches)

## Getting Started

1. **Clone the project**

   ```shell
   git clone https://github.com/knull-reaper/esp32_smb_scanner.git
   cd esp32_smb_scanner
   ```

2. **Install Python dependencies**

   ```shell
   pip install pyserial
   ```

3. **Flash the ESP32 firmware**

   - Open `esp32_frimware.ino` in your preferred IDE.
   - Select your ESP32 board profile and COM port.
   - Compile and upload the sketch. No third-party libraries are required beyond the default ESP32 SDK.

4. **Start the host controller**
   ```shell
   python python_server.py
   ```
   The shell prints an initial help menu and keeps an eye on USB serial ports. When it detects the ESP32, it attaches automatically and begins listening for reports.

## Shell Commands

All commands are entered at the `esp32 >` prompt.

| Command                        | Description                                                                         |
| ------------------------------ | ----------------------------------------------------------------------------------- |
| `help`                         | Show the command summary.                                                           |
| `join -s <ssid> -p <password>` | Connect the ESP32 to a new Wi-Fi network. Credentials are stored locally for reuse. |
| `join -i <index>`              | Reconnect using a previously saved network (see `networks`).                        |
| `scan -all`                    | Queue a full subnet scan based on the ESP32’s current IP and mask.                  |
| `scan -t <ipv4>`               | Probe a single host.                                                                |
| `networks`                     | List saved Wi-Fi profiles.                                                          |
| `randomize_mac`                | Randomise the station MAC before the next Wi-Fi connection.                         |
| `status`                       | Display controller / serial health information.                                     |
| `ipconfig`                     | Print the host machine’s interface details (Windows `ipconfig`).                    |
| `clear` / `cls`                | Clear the terminal screen.                                                          |
| `exit`                         | Shut down the controller and close the serial link.                                 |

All activity is timestamped to `scan_log.txt` via a rotating log handler (1 MiB per file, three backups).

## Status Codes

The firmware always prefixes reports with `0xAB`, followed by a packed 4-byte IPv4 address and a status byte. The host maps the status byte to friendly messages:

| Code        | Meaning                                                  |
| ----------- | -------------------------------------------------------- |
| `1`         | `TARGET_UNREACHABLE` – TCP/445 connection failed.        |
| `2`         | `PORT_OPEN` – TCP/445 connection succeeded.              |
| `3`         | `SERVICE_NO_RESPONSE` – No data returned before timeout. |
| `4`         | `SERVICE_RESPONDED` – SMB negotiate reply received.      |
| `5` / `6`   | `SCAN_CYCLE_START` / `SCAN_CYCLE_END`.                   |
| `10` / `11` | `WIFI_CONNECT_SUCCESS` / `WIFI_CONNECT_FAILURE`.         |
| `15`        | `SCANNING_TARGET` – emitted before each probe.           |

This set is intentionally conservative: it confirms service availability without attempting code execution.

## Good Citizenship Checklist

- **Stay authorised** – obtain written permission before scanning assets you do not own.
- **Log responsibly** – audit trails can help prove intent and scope.
- **Rate-limit if needed** – adjust `SCAN_IDLE_DELAY_MS` in the firmware to be gentler on congested networks.
- **Disclose carefully** – if you do find an exposed SMB service, follow your organisation’s disclosure process.

## Troubleshooting

- Use `status` in the shell to confirm serial connectivity and stored Wi-Fi profiles.
- When the ESP32 is busy scanning, commands queue until the current job finishes.
- If the prompt becomes cluttered by asynchronous logs, press `Enter` to refresh, or run `clear`.

## License & Contribution

No formal license has been declared. Treat this as reference material unless you decide to publish under your preferred license. Contributions are welcome, please include test evidence and respect the safety notes above.
