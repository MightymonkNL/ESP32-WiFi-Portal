// ESP32-Portal — WiFi accesspoint met captive landing page
//
// Werking:
//  1. ESP32 start een eigen WiFi-netwerk (accesspoint).
//  2. Een DNS-server beantwoordt ELKE domeinnaam met het IP van de ESP32.
//  3. Telefoons/laptops controleren na verbinden of er internet is
//     (bijv. /generate_204, /hotspot-detect.html). Die verzoeken worden
//     doorgestuurd naar de landing page, waardoor het OS automatisch het
//     "Sign in to network" window opent.
//  4. Bestanden van de landing page staan in LittleFS (map data/).
//  5. SSID/wachtwoord zijn te wijzigen via /instellingen (opgeslagen in NVS).

#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_netif.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <mbedtls/base64.h>
#include "config.h"
#include "status_display.h"
#include "survey.h"
#include "power.h"

static const byte DNS_PORT = 53;
static const IPAddress apIP(AP_IP);
static const IPAddress netMask(AP_NETMASK);

DNSServer dnsServer;
WebServer server(80);
Preferences prefs;

String apSsid;
String apPass;
bool apEnabled = true;     // netwerk aan/uit (bewaard in NVS)
bool apRunning = false;

// ---------------------------------------------------------------- instellingen

static void loadSettings() {
  prefs.begin("portal", true);
  apSsid = prefs.getString("ssid", AP_SSID);
  apPass = prefs.getString("pass", AP_PASSWORD);
  apEnabled = prefs.getBool("on", true);
  prefs.end();
  if (apSsid.isEmpty()) apSsid = AP_SSID;
}

static void saveSettings(const String &ssid, const String &pass) {
  prefs.begin("portal", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.end();
}

static void factoryReset() {
  prefs.begin("portal", false);
  prefs.clear();
  prefs.end();
}

static void saveEnabled(bool on) {
  prefs.begin("portal", false);
  prefs.putBool("on", on);
  prefs.end();
}

// ---------------------------------------------------------------- accesspoint aan/uit

static void startAP() {
  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(apIP, apIP, netMask);
  const char *pass = apPass.length() >= 8 ? apPass.c_str() : nullptr;
  WiFi.softAP(apSsid.c_str(), pass, AP_CHANNEL, 0, AP_MAX_CONN);
  WiFi.setSleep(false);
  // Devices that leave without signing off are dropped after 15 s without traffic
  // (ESP-IDF default is 300 s, which leaves stale entries in the device count).
  esp_wifi_set_inactive_time(WIFI_IF_AP, 15);
  dnsServer.setErrorReplyCode(DNSReplyCode::NoError);
  dnsServer.start(DNS_PORT, "*", apIP);   // elke DNS-vraag -> IP van de ESP32
  apRunning = true;
  // The web server needs the network stack, which only exists once WiFi has
  // been started; start it here (once) instead of unconditionally in setup().
  static bool serverStarted = false;
  if (!serverStarted) {
    server.begin();
    serverStarted = true;
    Serial.println("[HTTP] web server running on port 80");
  }
  Serial.printf("[WiFi] ON  SSID: %s (%s)  IP: %s\n", apSsid.c_str(),
                pass ? "password" : "open", WiFi.softAPIP().toString().c_str());
}

static void stopAP() {
  dnsServer.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_OFF);
  apRunning = false;
  Serial.println("[WiFi] OFF  network disabled");
}

static void setApEnabled(bool on) {
  apEnabled = on;
  saveEnabled(on);
  if (on && !apRunning) startAP();
  if (!on && apRunning) stopAP();
}

// Geeft een foutmelding terug, of "" als de waarden geldig zijn.
static String validateSettings(const String &ssid, const String &pass) {
  if (ssid.isEmpty() || ssid.length() > 32) return "Network name must be 1 to 32 characters.";
  if (!pass.isEmpty() && (pass.length() < 8 || pass.length() > 63))
    return "Password must be 8 to 63 characters.";
  return "";
}

// ---------------------------------------------------------------- helpers

static String portalUrl() { return String("http://") + apIP.toString() + "/"; }

static void redirectToPortal() {
  server.sendHeader("Location", portalUrl(), true);
  server.sendHeader("Cache-Control", "no-cache, no-store, must-revalidate");
  server.send(302, "text/plain", "");
}

// Verzoek voor een ander domein (bijv. connectivitycheck.gstatic.com)?
static bool isForeignHost() {
  String host = server.hostHeader();
  return host.length() > 0 && host != apIP.toString();
}

static String htmlEscape(const String &s) {
  String out;
  out.reserve(s.length() + 8);
  for (char c : s) {
    switch (c) {
      case '&': out += "&amp;"; break;
      case '<': out += "&lt;"; break;
      case '>': out += "&gt;"; break;
      case '"': out += "&quot;"; break;
      case '\'': out += "&#39;"; break;
      default: out += c;
    }
  }
  return out;
}

static String contentType(const String &path) {
  if (path.endsWith(".html")) return "text/html; charset=utf-8";
  if (path.endsWith(".css"))  return "text/css";
  if (path.endsWith(".js"))   return "application/javascript";
  if (path.endsWith(".json")) return "application/json";
  if (path.endsWith(".png"))  return "image/png";
  if (path.endsWith(".jpg") || path.endsWith(".jpeg")) return "image/jpeg";
  if (path.endsWith(".svg"))  return "image/svg+xml";
  if (path.endsWith(".ico"))  return "image/x-icon";
  if (path.endsWith(".webp")) return "image/webp";
  if (path.endsWith(".woff2")) return "font/woff2";
  return "application/octet-stream";
}

static bool serveFile(String path) {
  if (path.endsWith("/")) path += "index.html";
  if (path.startsWith("/.")) return false;  // interne bestanden (bijv. /.upload)
  if (!LittleFS.exists(path)) return false;
  File f = LittleFS.open(path, "r");
  server.streamFile(f, contentType(path));
  f.close();
  return true;
}

// ---------------------------------------------------------------- instellingenpagina

static const char PAGE_HEAD[] PROGMEM = R"(<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settings</title><style>
:root{--p:#9C89D7;--bg:#121212;--s1:#1E1E1E;--s2:#2C2C2C;--t:#E1E1E1;--m:#9E9E9E;--o:#FF9E71;--g:#5DBB6A}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:var(--bg);color:var(--t)}
header{background:var(--s2);padding:28px 16px 52px;text-align:center}
h1{margin:0;font-size:24px}main{max-width:460px;margin:-32px auto 32px;padding:0 16px}
.card{background:var(--s1);border-radius:16px;padding:24px;box-shadow:0 2px 12px rgba(0,0,0,.25)}
label{display:block;font-weight:600;margin:16px 0 6px}label:first-child{margin-top:0}
input[type=text],input[type=password]{width:100%;padding:12px;border:1px solid #3D3D3D;border-radius:10px;font-size:16px;background:var(--s2);color:var(--t)}
input:focus{outline:none;border-color:var(--p)}
.chk{display:flex;gap:8px;align-items:center;font-weight:400}
.hint{font-size:13px;color:var(--m);margin:6px 0 0}
button{margin-top:24px;width:100%;padding:14px;border:0;border-radius:10px;background:var(--p);color:#121212;font-size:16px;font-weight:600}
.msg{padding:12px 14px;border-radius:10px;margin-bottom:16px;background:var(--s2)}.ok{color:var(--g)}.err{color:var(--o)}
</style></head><body><header><h1>Settings</h1></header><main><div class="card">)";

static const char PAGE_TAIL[] PROGMEM = "</div></main></body></html>";

static void sendSettingsPage(const String &message, bool isError) {
  String html = FPSTR(PAGE_HEAD);
  if (message.length()) {
    html += "<div class=\"msg ";
    html += isError ? "err" : "ok";
    html += "\">" + htmlEscape(message) + "</div>";
  }
  html += F("<form method=\"post\" action=\"/settings\">"
            "<label for=\"ssid\">Network name (SSID)</label>"
            "<input type=\"text\" id=\"ssid\" name=\"ssid\" maxlength=\"32\" required value=\"");
  html += htmlEscape(apSsid);
  html += F("\"><label for=\"pass\">New WiFi password</label>"
            "<input type=\"password\" id=\"pass\" name=\"pass\" maxlength=\"63\" autocomplete=\"new-password\">"
            "<p class=\"hint\">8–63 characters. Leave empty to keep the current one.</p>"
            "<label class=\"chk\"><input type=\"checkbox\" name=\"open\" value=\"1\"");
  if (apPass.isEmpty()) html += " checked";
  html += F("> Open network (no password)</label>"
            "<button type=\"submit\">Save and restart</button></form>");
  html += FPSTR(PAGE_TAIL);
  server.send(isError ? 400 : 200, "text/html; charset=utf-8", html);
}

static bool requireAdmin() {
  // No usable default password in public builds: the page stays off until the
  // owner sets ADMIN_PASSWORD in include/config_local.h.
  if (strcmp(ADMIN_PASSWORD, "changeme") == 0) {
    server.send(403, "text/plain; charset=utf-8",
                "The settings page is disabled. Set ADMIN_PASSWORD in include/config_local.h "
                "and flash the firmware, or use the Portal Admin app over USB.");
    return false;
  }
  if (!server.authenticate(ADMIN_USER, ADMIN_PASSWORD)) {
    server.requestAuthentication(BASIC_AUTH, "ESP32-Portal");
    return false;
  }
  return true;
}

static void handleSettingsGet() {
  if (!requireAdmin()) return;
  sendSettingsPage("", false);
}

static void handleSettingsPost() {
  if (!requireAdmin()) return;

  String ssid = server.arg("ssid");
  ssid.trim();
  String pass = server.arg("pass");
  bool open = server.hasArg("open");

  String newPass;
  if (open) {
    newPass = "";
  } else if (pass.isEmpty()) {
    newPass = apPass;
    if (newPass.isEmpty()) {
      sendSettingsPage("Enter a password, or choose 'Open network'.", true);
      return;
    }
  } else {
    newPass = pass;
  }

  String err = validateSettings(ssid, newPass);
  if (err.length()) {
    sendSettingsPage(err, true);
    return;
  }

  saveSettings(ssid, newPass);
  apSsid = ssid;
  apPass = newPass;
  Serial.printf("[CFG] New SSID saved: %s, restarting\n", ssid.c_str());

  String html = FPSTR(PAGE_HEAD);
  html += "<div class=\"msg ok\">Saved. The portal is restarting and will be available as <strong>"
          + htmlEscape(ssid) + "</strong>. Reconnect to that network.</div>";
  html += FPSTR(PAGE_TAIL);
  server.send(200, "text/html; charset=utf-8", html);
  delay(1500);
  ESP.restart();
}

// ---------------------------------------------------------------- survey answers

// Voter key for the requesting device: based on its WiFi MAC address (looked up
// via the DHCP lease of its IP); falls back to the IP address.
static String currentVoterKey() {
  IPAddress ip = server.client().remoteIP();
  wifi_sta_list_t sta;
  if (esp_wifi_ap_get_sta_list(&sta) == ESP_OK && sta.num > 0) {
    esp_netif_pair_mac_ip_t pairs[ESP_WIFI_MAX_CONN_NUM];
    int n = sta.num;
    for (int i = 0; i < n; i++) memcpy(pairs[i].mac, sta.sta[i].mac, 6);
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
    if (netif && esp_netif_dhcps_get_clients_by_mac(netif, n, pairs) == ESP_OK) {
      for (int i = 0; i < n; i++)
        if (pairs[i].ip.addr == (uint32_t)ip) return surveyVoterKey(pairs[i].mac, 6);
    }
  }
  uint32_t raw = (uint32_t)ip;
  return surveyVoterKey((const uint8_t *)&raw, sizeof(raw));
}

static void handleVoted() {
  bool voted = surveyActive() && surveyHasVoted(currentVoterKey());
  server.sendHeader("Cache-Control", "no-cache, no-store, must-revalidate");
  server.send(200, "application/json", voted ? "{\"voted\":true}" : "{\"voted\":false}");
}

static void handleAnswer() {
  // Simple flood protection: max one answer per client IP every 2 seconds
  static uint32_t recentIp[8] = {0};
  static unsigned long recentAt[8] = {0};
  uint32_t ip = (uint32_t)server.client().remoteIP();
  int slot = -1, oldest = 0;
  for (int i = 0; i < 8; i++) {
    if (recentIp[i] == ip) slot = i;
    if (recentAt[i] < recentAt[oldest]) oldest = i;
  }
  if (slot >= 0 && millis() - recentAt[slot] < 2000) {
    redirectToPortal();
    return;
  }
  if (slot < 0) slot = oldest;
  recentIp[slot] = ip;
  recentAt[slot] = millis();

  String err = surveyRecord(server.arg("a").toInt(), server.arg("t"),
                            deviceFromUserAgent(server.header("User-Agent")), currentVoterKey());
  if (err.length()) Serial.println("[SURVEY] not stored: " + err);
  String target = String("http://") + apIP.toString() + "/thanks.html";
  if (err == "Already answered") target += "?dup=1";
  server.sendHeader("Location", target, true);
  server.sendHeader("Cache-Control", "no-cache, no-store, must-revalidate");
  server.send(303, "text/plain", "");
}

// ---------------------------------------------------------------- algemene handler

static void handleRequest() {
  // Captive-portal-detectie en alle andere domeinen -> landing page
  if (isForeignHost()) {
    redirectToPortal();
    return;
  }
  if (!serveFile(server.uri())) {
    redirectToPortal();
  }
}

// ---------------------------------------------------------------- resetknop

static void checkResetButton() {
  static unsigned long pressedSince = 0;
  if (digitalRead(RESET_BUTTON_PIN) == LOW) {
    if (pressedSince == 0) pressedSince = millis();
    if (millis() - pressedSince >= RESET_HOLD_MS) {
      Serial.println("[CFG] Factory reset: default SSID restored, restarting");
      factoryReset();
      delay(200);
      ESP.restart();
    }
  } else {
    pressedSince = 0;
  }
}

// ---------------------------------------------------------------- USB-beheer
//
// Regelgebaseerd protocol over de USB-seriële poort (115200 baud), gebruikt
// door de laptop-app admin/portal_admin.pyw. Antwoorden beginnen met '@'
// zodat ze te onderscheiden zijn van gewone logregels.
//
//   INFO                      -> @INFO ssid=<hex> open=<0|1> clients=<n> ip=<ip> fw=<versie>
//   SET <ssidhex> <pass>      -> @OK (daarna herstart) of @ERR <melding>
//                                <pass>: "-" = open netwerk, "=" = ongewijzigd,
//                                anders het wachtwoord in hex
//   RESET                     -> @OK (fabrieksreset, daarna herstart)
//
// Tekst wordt hex-gecodeerd (UTF-8) zodat spaties en speciale tekens geen
// probleem geven. Toegang via USB vereist fysieke toegang tot het bordje,
// daarom is hier geen beheerwachtwoord nodig.

//   FS                        -> @FS total=<bytes> used=<bytes>
//   FBEGIN <pad> <grootte>    -> start upload van een bestand in de root (bijv. /index.html)
//   FDATA <base64>            -> blok data (max. 512 bytes na decoderen)
//   FEND                      -> @OK: bestand vervangen; @ERR bij onvolledige upload
//   FABORT                    -> upload afbreken
//   FDEL <pad>                -> bestand verwijderen


static const char *UPLOAD_TMP = "/.upload";
static File upFile;
static String upPath;
static size_t upSize = 0, upGot = 0;
static bool upActive = false;

static bool validPath(const String &p) {
  if (p.length() < 2 || p.length() > 31 || p[0] != '/' || p[1] == '.') return false;
  for (size_t i = 1; i < p.length(); i++) {
    char c = p[i];
    if (!(isalnum((unsigned char)c) || c == '.' || c == '_' || c == '-')) return false;
  }
  return true;
}

static void abortUpload() {
  if (upFile) upFile.close();
  LittleFS.remove(UPLOAD_TMP);
  upActive = false;
}

// Geeft true terug als het commando een bestandscommando was.
static bool handleFileCommand(const String &line) {
  if (line == "FS") {
    Serial.printf("@FS total=%u used=%u\n", (unsigned)LittleFS.totalBytes(), (unsigned)LittleFS.usedBytes());
    return true;
  }
  if (line.startsWith("FBEGIN ")) {
    int sp = line.indexOf(' ', 7);
    String path = sp > 0 ? line.substring(7, sp) : "";
    long size = sp > 0 ? line.substring(sp + 1).toInt() : -1;
    if (!validPath(path) || size < 0) { Serial.println("@ERR Invalid path or size"); return true; }
    if (upActive) abortUpload();
    size_t existing = 0;
    if (LittleFS.exists(path)) { File f = LittleFS.open(path, "r"); existing = f.size(); f.close(); }
    size_t freeBytes = LittleFS.totalBytes() - LittleFS.usedBytes() + existing;
    if ((size_t)size + 16384 > freeBytes) { Serial.println("@ERR Not enough space on the board"); return true; }
    upFile = LittleFS.open(UPLOAD_TMP, "w");
    if (!upFile) { Serial.println("@ERR Cannot create file"); return true; }
    upPath = path; upSize = size; upGot = 0; upActive = true;
    Serial.println("@OK");
    return true;
  }
  if (line.startsWith("FDATA ")) {
    if (!upActive) { Serial.println("@ERR No upload in progress"); return true; }
    static uint8_t out[600];
    size_t olen = 0;
    String b64 = line.substring(6);
    if (mbedtls_base64_decode(out, sizeof(out), &olen, (const uint8_t *)b64.c_str(), b64.length()) != 0 ||
        upGot + olen > upSize) {
      abortUpload(); Serial.println("@ERR Invalid data"); return true;
    }
    if (upFile.write(out, olen) != olen) { abortUpload(); Serial.println("@ERR Write failed (storage full?)"); return true; }
    upGot += olen;
    Serial.println("@OK");
    return true;
  }
  if (line == "FEND") {
    if (!upActive) { Serial.println("@ERR No upload in progress"); return true; }
    upFile.close();
    upActive = false;
    if (upGot != upSize) { LittleFS.remove(UPLOAD_TMP); Serial.println("@ERR Upload incomplete"); return true; }
    LittleFS.remove(upPath);
    if (!LittleFS.rename(UPLOAD_TMP, upPath)) { Serial.println("@ERR Save failed"); return true; }
    Serial.printf("[FS] %s updated (%u bytes)\n", upPath.c_str(), (unsigned)upSize);
    if (upPath == SURVEY_CFG) surveyReload();
    Serial.println("@OK");
    return true;
  }
  if (line == "FABORT") { abortUpload(); Serial.println("@OK"); return true; }
  if (line.startsWith("FDEL ")) {
    String path = line.substring(5);
    if (!validPath(path)) { Serial.println("@ERR Invalid path"); return true; }
    LittleFS.remove(path);
    if (path == SURVEY_CFG) surveyReload();
    Serial.println("@OK");
    return true;
  }
  if (line.startsWith("FREAD ")) {
    // FREAD <path> <offset> -> @DATA <base64, max 512 bytes> or @EOF
    int sp = line.indexOf(' ', 6);
    String path = sp > 0 ? line.substring(6, sp) : "";
    long offset = sp > 0 ? line.substring(sp + 1).toInt() : -1;
    if (!validPath(path) || offset < 0) { Serial.println("@ERR Invalid path or offset"); return true; }
    if (!LittleFS.exists(path)) { Serial.println("@EOF"); return true; }
    File f = LittleFS.open(path, "r");
    if ((size_t)offset >= f.size()) { f.close(); Serial.println("@EOF"); return true; }
    static uint8_t raw[512];
    static unsigned char enc[700];
    f.seek(offset);
    size_t n = f.read(raw, sizeof(raw));
    f.close();
    size_t olen = 0;
    mbedtls_base64_encode(enc, sizeof(enc), &olen, raw, n);
    enc[olen] = 0;
    Serial.print("@DATA ");
    Serial.println((const char *)enc);
    return true;
  }
  if (line == "LOGCLEAR") {
    surveyClearLog();
    Serial.println("@OK");
    return true;
  }
  return false;
}

static String toHex(const String &s) {
  static const char *digits = "0123456789abcdef";
  String out;
  out.reserve(s.length() * 2);
  for (size_t i = 0; i < s.length(); i++) {
    uint8_t b = (uint8_t)s[i];
    out += digits[b >> 4];
    out += digits[b & 0x0F];
  }
  return out;
}

static bool fromHex(const String &hex, String &out) {
  if (hex.length() % 2) return false;
  out = "";
  for (size_t i = 0; i < hex.length(); i += 2) {
    char buf[3] = {hex[i], hex[i + 1], 0};
    char *end;
    long v = strtol(buf, &end, 16);
    if (*end != 0 || v == 0) return false;  // ongeldig of NUL-teken
    out += (char)v;
  }
  return true;
}

static void handleSerialCommand(String line) {
  line.trim();
  if (line.isEmpty()) return;
  if (handleFileCommand(line)) return;

  if (line == "INFO") {
    PowerStatus ps = pmuStatus();
    Serial.printf("@INFO ssid=%s open=%d clients=%d ip=%s fw=%s lcd=%s ap=%d bat=%d chg=%d\n",
                  toHex(apSsid).c_str(), apPass.isEmpty() ? 1 : 0,
                  apRunning ? WiFi.softAPgetStationNum() : 0, apIP.toString().c_str(), FW_VERSION,
                  displayDriver(), apRunning ? 1 : 0, ps.battery ? ps.percent : -1,
                  ps.battery && ps.charging ? 1 : 0);
    return;
  }

  if (line == "AP 1" || line == "AP 0") {
    setApEnabled(line == "AP 1");
    Serial.println("@OK");
    return;
  }

  if (line == "PMU") {
    Serial.println("@PMU " + pmuReport(pmuStatus()) + " | boot: " + pmuBootReport());
    return;
  }

  if (line == "TOUCH") {
    int x, y, f;
    unsigned long age;
    displayLastTouch(x, y, f, age);
    Serial.printf("@TOUCH x=%d y=%d fingers=%d age=%lu\n", x, y, f, age);
    return;
  }

  if (line == "RESET") {
    Serial.println("@OK");
    Serial.flush();
    factoryReset();
    delay(300);
    ESP.restart();
    return;
  }

  if (line.startsWith("SET ")) {
    int sp = line.indexOf(' ', 4);
    if (sp < 0) { Serial.println("@ERR Invalid command"); return; }
    String ssidHex = line.substring(4, sp);
    String passArg = line.substring(sp + 1);

    String ssid, pass;
    if (!fromHex(ssidHex, ssid)) { Serial.println("@ERR Invalid network name"); return; }
    if (passArg == "-") pass = "";
    else if (passArg == "=") pass = apPass;
    else if (!fromHex(passArg, pass)) { Serial.println("@ERR Invalid password"); return; }

    String err = validateSettings(ssid, pass);
    if (err.length()) { Serial.println("@ERR " + err); return; }

    saveSettings(ssid, pass);
    Serial.println("@OK");
    Serial.flush();
    delay(300);
    ESP.restart();
    return;
  }

  Serial.println("@ERR Unknown command");
}

static void pollSerial() {
  static String buf;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (buf.length()) handleSerialCommand(buf);
      buf = "";
    } else if (buf.length() < 1024) {
      buf += c;
    }
  }
}

// ---------------------------------------------------------------- setup / loop

void setup() {
  Serial.setRxBufferSize(4096);   // standaard 256 B: te klein voor FDATA-regels (~690 tekens)
  Serial.begin(115200);
  delay(500);
  Serial.println("\n[ESP32-Portal] start");

  pinMode(RESET_BUTTON_PIN, INPUT_PULLUP);
  loadSettings();

  if (!LittleFS.begin(true)) {
    Serial.println("[FS] LittleFS mount failed");
  } else if (!LittleFS.exists("/index.html")) {
    Serial.println("[FS] Note: /index.html is missing; upload a portal page with the admin app");
  }

  surveyBegin();
  static const char *headerKeys[] = {"User-Agent"};
  server.collectHeaders(headerKeys, 1);
  server.on("/answer", HTTP_POST, handleAnswer);
  server.on("/voted", HTTP_GET, handleVoted);
  server.on("/settings", HTTP_GET, handleSettingsGet);
  server.on("/settings", HTTP_POST, handleSettingsPost);
  // Al het overige via één handler (incl. /generate_204, /hotspot-detect.html,
  // /connecttest.txt, /ncsi.txt — die worden zo doorgestuurd).
  server.onNotFound(handleRequest);

  // startAP() also starts the web server (needs the network stack)
  if (apEnabled) startAP();
  else {
    WiFi.mode(WIFI_OFF);
    Serial.println("[WiFi] network is off (turn on via the admin app or the touchscreen)");
  }

  displayBegin();
  pmuBegin();
}

static void updateDisplay(bool force = false) {
  static unsigned long lastUpdate = 0;
  if (!force && millis() - lastUpdate < 500) return;
  lastUpdate = millis();

  PortalStatus s;
  s.online = apRunning && (WiFi.getMode() & WIFI_AP);
  s.ssid = apSsid;
  s.open = apPass.isEmpty();
  s.clients = s.online ? WiFi.softAPgetStationNum() : 0;
  s.ip = apIP.toString();
  s.uptimeSec = millis() / 1000;
  s.survey = surveyActive();
  s.surveyType = surveyType();
  s.votes = surveyVotes();
  s.average = surveyAverage();
  s.summary = surveySummary();
  // Battery: read every 5 s (enough for a slowly changing value)
  static unsigned long lastPmu = 0;
  static PowerStatus ps = {false, -1, 0, false, false, 0, 0, "n/a"};
  if (lastPmu == 0 || millis() - lastPmu > 5000) {
    lastPmu = millis();
    ps = pmuStatus();
  }
  s.battery = ps.battery ? ps.percent : -1;
  s.charging = ps.battery && ps.charging;
  displayUpdate(s);
}

void loop() {
  if (apRunning) {
    dnsServer.processNextRequest();
    server.handleClient();
  }
  checkResetButton();
  pollSerial();
  if (displayPollTap()) {          // tik op de netwerkkaart = netwerk aan/uit
    setApEnabled(!apEnabled);
    updateDisplay(true);
  }
  updateDisplay();
  delay(2);
}
