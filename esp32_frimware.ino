#include <WiFi.h>
#include "esp_wifi.h"

// --- STATUS CODES ---
enum StatusCode : uint8_t {
  TARGET_UNREACHABLE = 1,
  PORT_OPEN = 2,
  SERVICE_NO_RESPONSE = 3,
  SERVICE_RESPONDED = 4,
  SCAN_CYCLE_START = 5,
  SCAN_CYCLE_END = 6,
  WIFI_CONNECT_SUCCESS = 10,
  WIFI_CONNECT_FAILURE = 11,
  SCANNING_TARGET = 15,
  DEVICE_READY = 16
};

// --- BINARY DATA STRUCTURE ---
struct __attribute__((packed)) ScanReport {
  uint32_t target_ip;
  uint8_t status_code;
};

// --- GLOBAL STATE & TASK MANAGEMENT ---
bool is_connected = false;
bool should_randomize_mac = false;
volatile bool scan_full_requested = false;
volatile bool scan_target_requested = false;
volatile bool scan_in_progress = false;
IPAddress scan_target_ip;
TaskHandle_t ScanTaskHandle = NULL;

// --- CONSTANTS ---
const uint16_t SMB_PORT = 445;
const uint16_t CONNECT_TIMEOUT_MS = 1500;
const uint16_t RESPONSE_TIMEOUT_MS = 750;
const uint16_t SCAN_IDLE_DELAY_MS = 15;

const uint8_t smb_negotiate_request[] = {
  0x00, 0x00, 0x00, 0x61, 0xFF, 0x53, 0x4D, 0x42, 0x72, 0x00, 0x00, 0x00, 0x00, 0x18, 0x53, 0xC8,
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x51, 0x00, 0x02, 0x4E,
  0x54, 0x20, 0x4C, 0x4D, 0x20, 0x30, 0x2E, 0x31, 0x32, 0x00, 0x02, 0x4C, 0x41, 0x4E, 0x4D, 0x41,
  0x4E, 0x31, 0x2E, 0x30, 0x00, 0x02, 0x53, 0x4D, 0x42, 0x20, 0x32, 0x2E, 0x30, 0x30, 0x00, 0x02,
  0x4E, 0x54, 0x20, 0x4C, 0x4D, 0x20, 0x30, 0x2E, 0x31, 0x31, 0x62, 0x00, 0x02, 0x4E, 0x54, 0x20,
  0x4C, 0x4D, 0x20, 0x30, 0x2E, 0x31, 0x31, 0x61, 0x00, 0x02, 0x4E, 0x54, 0x20, 0x4C, 0x4D, 0x20,
  0x30, 0x2E, 0x31, 0x30, 0x00
};

// --- HELPERS ---
static uint32_t toNetworkOrder(const IPAddress &ip) {
  return (static_cast<uint32_t>(ip[0]) << 24) |
         (static_cast<uint32_t>(ip[1]) << 16) |
         (static_cast<uint32_t>(ip[2]) << 8) |
         static_cast<uint32_t>(ip[3]);
}

static IPAddress fromNetworkOrder(uint32_t value) {
  return IPAddress(
    static_cast<uint8_t>((value >> 24) & 0xFF),
    static_cast<uint8_t>((value >> 16) & 0xFF),
    static_cast<uint8_t>((value >> 8) & 0xFF),
    static_cast<uint8_t>(value & 0xFF)
  );
}

static uint32_t toLittleEndianPacked(const IPAddress &ip) {
  return static_cast<uint32_t>(ip[0]) |
         (static_cast<uint32_t>(ip[1]) << 8) |
         (static_cast<uint32_t>(ip[2]) << 16) |
         (static_cast<uint32_t>(ip[3]) << 24);
}

// --- CORE FUNCTIONS ---

void reportToServer(uint32_t ip, StatusCode status) {
  const uint8_t MAGIC_BYTE = 0xAB;
  ScanReport report;
  report.target_ip = ip;
  report.status_code = status;
  
  Serial.write(MAGIC_BYTE);
  Serial.write((uint8_t*)&report, sizeof(report));
}

void reportToServer(const IPAddress &ip, StatusCode status) {
  reportToServer(toLittleEndianPacked(ip), status);
}

void probeTarget(const IPAddress &targetIP) {
  WiFiClient target_client;
  target_client.setTimeout(RESPONSE_TIMEOUT_MS);
  target_client.setNoDelay(true);

  if (!target_client.connect(targetIP, SMB_PORT, CONNECT_TIMEOUT_MS)) {
    reportToServer(targetIP, TARGET_UNREACHABLE);
    return;
  }

  reportToServer(targetIP, PORT_OPEN);
  target_client.write(smb_negotiate_request, sizeof(smb_negotiate_request));
  target_client.flush();

  const uint32_t deadline = millis() + RESPONSE_TIMEOUT_MS;
  bool response_available = false;

  while (millis() < deadline) {
    if (!target_client.connected()) {
      break;
    }
    if (target_client.available()) {
      response_available = true;
      break;
    }
    delay(SCAN_IDLE_DELAY_MS);
    yield();
  }

  if (response_available) {
    // Drain any residual response bytes to keep socket clean.
    while (target_client.available()) {
      target_client.read();
    }
    reportToServer(targetIP, SERVICE_RESPONDED);
  } else {
    reportToServer(targetIP, SERVICE_NO_RESPONSE);
  }
  target_client.stop();
}

bool runFullScan() {
  if (!is_connected || WiFi.status() != WL_CONNECTED) {
    Serial.println("Full scan aborted: not connected to Wi-Fi.");
    return false;
  }

  IPAddress localIP = WiFi.localIP();
  IPAddress subnet = WiFi.subnetMask();

  const uint32_t local = toNetworkOrder(localIP);
  const uint32_t mask = toNetworkOrder(subnet);
  const uint32_t network = local & mask;
  const uint32_t broadcast = network | (~mask);

  if (broadcast <= network + 1) {
    Serial.println("Subnet too small to scan.");
    return false;
  }

  uint32_t total_hosts = (broadcast > network) ? (broadcast - network - 1) : 0;
  const uint32_t MAX_HOSTS = 4094;
  if (total_hosts == 0) {
    Serial.println("No hosts detected for this subnet.");
    return false;
  }
  if (total_hosts > MAX_HOSTS) {
    Serial.printf("Large subnet detected (%lu hosts). Limiting scan to first %u nodes.\n",
                  static_cast<unsigned long>(total_hosts), MAX_HOSTS);
    total_hosts = MAX_HOSTS;
  }

  reportToServer(0, SCAN_CYCLE_START);

  for (uint32_t offset = 1; offset <= total_hosts; ++offset) {
    IPAddress targetIP = fromNetworkOrder(network + offset);
    if (targetIP == localIP) continue;

    reportToServer(targetIP, SCANNING_TARGET);
    probeTarget(targetIP);
    vTaskDelay(pdMS_TO_TICKS(SCAN_IDLE_DELAY_MS));
  }

  reportToServer(0, SCAN_CYCLE_END);
  Serial.println("Network scan cycle complete.");
  return true;
}

// --- SCAN TASK (runs on Core 1) ---
void scanTask(void *pvParameters) {
  for (;;) {
    if (scan_in_progress) {
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }

    if (scan_target_requested) {
      scan_in_progress = true;
      scan_target_requested = false;
      IPAddress target = scan_target_ip;

      if (!is_connected || WiFi.status() != WL_CONNECTED) {
        Serial.println("Target scan cancelled: Wi-Fi not connected.");
        scan_in_progress = false;
      } else if (target == WiFi.localIP()) {
        Serial.println("Target scan skipped: target equals local IP.");
        scan_in_progress = false;
      } else {
        reportToServer(0, SCAN_CYCLE_START);
        reportToServer(target, SCANNING_TARGET);
        probeTarget(target);
        reportToServer(0, SCAN_CYCLE_END);
        Serial.println("Target scan complete.");
        scan_in_progress = false;
      }
      continue;
    }

    if (scan_full_requested) {
      scan_in_progress = true;
      scan_full_requested = false;
      bool completed = runFullScan();
      scan_in_progress = false;
      if (completed) {
        is_connected = false;
        WiFi.disconnect();
        Serial.println("Scan complete. Disconnected. Awaiting new commands.");
      }
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

void setMacAddress() {
  uint8_t new_mac[6];
  for (uint8_t &byte : new_mac) {
    byte = static_cast<uint8_t>(random(0, 256));
  }
  new_mac[0] = (new_mac[0] | 0x02) & 0xFE; // locally administered, unicast

  esp_wifi_stop();
  esp_err_t result = esp_wifi_set_mac(WIFI_IF_STA, new_mac);
  esp_wifi_start();

  if (result == ESP_OK) {
    Serial.printf("MAC randomized to %02X:%02X:%02X:%02X:%02X:%02X\n",
                  new_mac[0], new_mac[1], new_mac[2], new_mac[3], new_mac[4], new_mac[5]);
  } else {
    Serial.printf("Failed to set MAC address (error %d)\n", result);
  }
}

void connectToWiFi(const String &ssid, const String &password) {
  if (ssid.isEmpty()) {
    Serial.println("SSID is required to connect.");
    return;
  }

  if (should_randomize_mac) {
    setMacAddress();
    should_randomize_mac = false;
  }

  Serial.printf("Connecting to %s ...\n", ssid.c_str());
  WiFi.disconnect(true);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), password.c_str());

  unsigned long start = millis();
  const unsigned long timeout = 15000;
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeout) {
    delay(200);
    Serial.print(".");
    yield();
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    is_connected = true;
    scan_full_requested = false;
    scan_target_requested = false;
    scan_in_progress = false;
    IPAddress localIP = WiFi.localIP();
    reportToServer(localIP, WIFI_CONNECT_SUCCESS);
    Serial.print("Connected. IP: ");
    Serial.println(localIP);
  } else {
    reportToServer(0, WIFI_CONNECT_FAILURE);
    Serial.println("Failed to connect to Wi-Fi.");
    WiFi.disconnect(true);
  }
}

void handleSerialCommands() {
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();
    
    if (command.startsWith("join ")) {
      int first_space = command.indexOf(' ');
      int second_space = command.indexOf(' ', first_space + 1);
      if (second_space > first_space) {
        String ssid = command.substring(first_space + 1, second_space);
        String password = command.substring(second_space + 1);
        connectToWiFi(ssid, password);
      }
    } else if (command.startsWith("scan")) {
      if (!is_connected || WiFi.status() != WL_CONNECTED) {
        Serial.println("Scan request ignored: Wi-Fi not connected.");
        return;
      }
      if (scan_in_progress || scan_full_requested || scan_target_requested) {
        Serial.println("Scan request ignored: another scan already queued or running.");
        return;
      }

      String args = command.substring(4);
      args.trim();
      if (args.equalsIgnoreCase("-all") || args.equalsIgnoreCase("--all")) {
        scan_full_requested = true;
        Serial.println("Full network scan queued.");
      } else if (args.startsWith("-t")) {
        String ipString = args.substring(2);
        ipString.trim();
        if (ipString.length() == 0) {
          Serial.println("Usage: scan -t <target_ip>");
          return;
        }
        IPAddress target;
        if (!target.fromString(ipString)) {
          Serial.println("Invalid target IP address.");
          return;
        }
        if (target == WiFi.localIP()) {
          Serial.println("Target IP matches local interface; skipping.");
          return;
        }
        scan_target_ip = target;
        scan_target_requested = true;
        Serial.print("Target scan queued for ");
        Serial.println(target);
      } else {
        Serial.println("Usage: scan -all | scan -t <target_ip>");
      }
    } else if (command == "randomize_mac") {
      should_randomize_mac = true;
      Serial.println("MAC address will be randomized on next connection.");
    } else if (command == "reboot") {
      Serial.println("Reboot command received. Restarting...");
      scan_full_requested = false;
      scan_target_requested = false;
      scan_in_progress = false;
      delay(100);
      ESP.restart();
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("ESP32 Agent Initialized. Waiting for commands...");
  randomSeed(analogRead(0));

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  xTaskCreatePinnedToCore(
    scanTask, "ScanTask", 10000, NULL, 1, &ScanTaskHandle, 1);

  delay(100);
  reportToServer(0, DEVICE_READY);
}

void loop() {
  handleSerialCommands();
  delay(25);
}
