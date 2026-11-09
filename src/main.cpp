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
#include <BoardLink.h>
#include "config.h"
#include "status_display.h"
#include "survey.h"
#include "power.h"

// Editor content (JSON) stored by the admin app, so a new PC can download it
#define PORTAL_CONTENT "/portal.json"

// ---- Multiple portals (profiles) -------------------------------------------
// /portals.cfg (uploaded by the admin app): one portal per line,
//   <id> TAB <name> TAB <ssid or empty>
// Files of a portal: /p-<id>-index.html, /p-<id>-thanks.html, /p-<id>-survey.cfg.
// Images are shared (content-hash names in the root). Without /portals.cfg the
// root files are used (single portal, as in firmware 2.3 and older).
#define PORTALS_CFG "/portals.cfg"
#define MAX_PORTALS 8
struct PortalDef { String id, name, ssid; };
static PortalDef portals[MAX_PORTALS];
static String portalNames[MAX_PORTALS];
static int nPortals = 0, activePortal = 0, portalRev = 0;

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

// WiFi transmit power levels (TX button on the display), in 0.25 dBm units
static const int8_t TX_QDBM[4] = {28, 44, 60, 78};      // 7, 11, 15, 19.5 dBm
static const uint8_t TX_PCT[4] = {25, 50, 75, 100};
static int txLevel = DEFAULT_TX_LEVEL;
String runningSsid;        // SSID the access point was started with

// Network name of the active portal, or the general one
static String effectiveSsid() {
  if (nPortals && portals[activePortal].ssid.length()) return portals[activePortal].ssid;
  return apSsid;
}

// Path of a file of the active portal, e.g. portalPath("index.html")
static String portalPath(const char *file) {
  if (!nPortals) return String("/") + file;
  return "/p-" + portals[activePortal].id + "-" + file;
}

// ---------------------------------------------------------------- instellingen

static void loadSettings() {
  prefs.begin("portal", true);
  apSsid = prefs.getString("ssid", AP_SSID);
  apPass = prefs.getString("pass", AP_PASSWORD);
  apEnabled = prefs.getBool("on", true);
  txLevel = constrain(prefs.getUChar("txp", DEFAULT_TX_LEVEL), 0, 3);
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
  runningSsid = effectiveSsid();
  WiFi.softAP(runningSsid.c_str(), pass, AP_CHANNEL, 0, AP_MAX_CONN);
  WiFi.setSleep(false);
  esp_wifi_set_max_tx_power(TX_QDBM[txLevel]);
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
  Serial.printf("[WiFi] ON  SSID: %s (%s)  IP: %s\n", runningSsid.c_str(),
                pass ? "password" : "open", WiFi.softAPIP().toString().c_str());
}

static void stopAP() {
  dnsServer.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_OFF);
  apRunning = false;
  Serial.println("[WiFi] OFF  network disabled");
}

static void cycleTxPower() {
  txLevel = (txLevel + 1) % 4;
  prefs.begin("portal", false);
  prefs.putUChar("txp", txLevel);
  prefs.end();
  if (apRunning) esp_wifi_set_max_tx_power(TX_QDBM[txLevel]);
  Serial.printf("[WiFi] transmit power %d%% (%.1f dBm)\n", TX_PCT[txLevel], TX_QDBM[txLevel] / 4.0);
}

static void setApEnabled(bool on) {
  apEnabled = on;
  saveEnabled(on);
  if (on && !apRunning) startAP();
  if (!on && apRunning) stopAP();
}

// ---------------------------------------------------------------- portals

static bool validPortalId(const String &id) {
  if (id.isEmpty() || id.length() > 8) return false;
  for (char c : id)
    if (!isalnum((unsigned char)c)) return false;
  return true;
}

// Activates the portal: its survey, and its network name (restarts the AP if it changed).
static void applyPortal() {
  surveySetConfig(portalPath("survey.cfg"));
  surveyReload();
  if (apRunning && effectiveSsid() != runningSsid) {
    stopAP();
    startAP();
  }
  portalRev++;
  Serial.printf("[PORTAL] active: %s\n", nPortals ? portals[activePortal].name.c_str() : "(single)");
}

static void loadPortals() {
  nPortals = 0;
  if (LittleFS.exists(PORTALS_CFG)) {
    File f = LittleFS.open(PORTALS_CFG, "r");
    while (f.available() && nPortals < MAX_PORTALS) {
      String line = f.readStringUntil('\n');
      line.replace("\r", "");
      int t1 = line.indexOf('\t');
      int t2 = t1 < 0 ? -1 : line.indexOf('\t', t1 + 1);
      if (t1 < 0) continue;
      PortalDef d;
      d.id = line.substring(0, t1);
      d.name = t2 < 0 ? line.substring(t1 + 1) : line.substring(t1 + 1, t2);
      d.ssid = t2 < 0 ? "" : line.substring(t2 + 1);
      if (!validPortalId(d.id) || d.ssid.length() > 32) continue;
      if (d.name.isEmpty()) d.name = "Portal " + String(nPortals + 1);
      portals[nPortals] = d;
      portalNames[nPortals] = d.name;
      nPortals++;
    }
    f.close();
  }
  prefs.begin("portal", true);
  String want = prefs.getString("portal", "");
  prefs.end();
  activePortal = 0;
  for (int i = 0; i < nPortals; i++)
    if (portals[i].id == want) activePortal = i;
}

static bool selectPortal(int index) {
  if (index < 0 || index >= nPortals) return false;
  activePortal = index;
  prefs.begin("portal", false);
  prefs.putString("portal", portals[index].id);
  prefs.end();
  applyPortal();
  return true;
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
  if (path.endsWith(".gif"))  return "image/gif";
  if (path.endsWith(".svg"))  return "image/svg+xml";
  if (path.endsWith(".ico"))  return "image/x-icon";
  if (path.endsWith(".webp")) return "image/webp";
  if (path.endsWith(".woff2")) return "font/woff2";
  return "application/octet-stream";
}

static bool serveFile(String path) {
  if (path.endsWith("/")) path += "index.html";
  if (path.startsWith("/.")) return false;  // interne bestanden (bijv. /.upload)
  if (path == "/index.html" || path == "/thanks.html") path = portalPath(path.c_str() + 1);
  if (path == PORTALS_CFG || path.endsWith("survey.cfg")) return false;
  // Survey answers, voter keys and the app's settings are only readable over USB
  if (path == SURVEY_LOG || path == SURVEY_VOTERS || path == SURVEY_CFG || path == PORTAL_CONTENT)
    return false;
  if (!LittleFS.exists(path)) return false;
  File f = LittleFS.open(path, "r");
  // Editor images are named after their content hash, so browsers may cache them for good
  if (path.startsWith("/img-")) server.sendHeader("Cache-Control", "max-age=31536000, immutable");
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

  String device = deviceFromUserAgent(server.header("User-Agent"));
  String err = surveyType() == "open"
                   ? surveyRecordText(server.arg("txt"), server.arg("t"), device, currentVoterKey())
                   : surveyRecord(server.arg("a").toInt(), server.arg("t"), device, currentVoterKey());
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

// ---------------------------------------------------------------- USB commands
//
// The shared BoardLink library (lib/BoardLink) handles INFO, the file commands
// (FS, FLIST, FREAD, FBEGIN/FDATA/FEND/FABORT, FDEL) and REBOOT for the ESP32
// Toolbox app. The portal adds these commands (replies start with '@'):
//
//   SET <ssidhex> <pass>      -> @OK (then restart) or @ERR <message>
//                                <pass>: "-" = open network, "=" = unchanged,
//                                otherwise the password in hex
//   RESET                     -> @OK (factory reset, then restart)
//   AP 0 | AP 1               -> @OK: network off / on
//   PORTAL <id>               -> @OK: activate that portal (see /portals.cfg)
//   WIFI                      -> @WIFI ssid=<hex> pass=<hex> ap=<0|1> (for the app's backup)
//   LOGCLEAR                  -> @OK: clear the survey log
//   PMU, TOUCH                -> diagnostics
//
// Text is hex encoded (UTF-8) so spaces and special characters are no problem.
// USB access needs physical access to the board, so no admin password here.

using BoardLink::toHex;
using BoardLink::fromHex;

static String portalInfo() {
  PowerStatus ps = pmuStatus();
  char buf[320];
  snprintf(buf, sizeof(buf),
           "ssid=%s open=%d clients=%d ip=%s lcd=%s ap=%d bat=%d chg=%d portal=%s essid=%s txp=%d",
           toHex(apSsid).c_str(), apPass.isEmpty() ? 1 : 0,
           apRunning ? WiFi.softAPgetStationNum() : 0, apIP.toString().c_str(),
           displayDriver(), apRunning ? 1 : 0, ps.battery ? ps.percent : -1,
           ps.battery && ps.charging ? 1 : 0, nPortals ? portals[activePortal].id.c_str() : "-",
           toHex(effectiveSsid()).c_str(), TX_PCT[txLevel]);
  return buf;
}

static void portalFileChanged(const String &path) {
  if (path == PORTALS_CFG) { loadPortals(); applyPortal(); }
  else if (path == portalPath("survey.cfg")) surveyReload();
}

static bool handlePortalCommand(const String &line) {
  if (line == "LOGCLEAR") {
    surveyClearLog();
    Serial.println("@OK");
    return true;
  }

  if (line == "WIFI") {
    // WIFI -> @WIFI ssid=<hex> pass=<hex> ap=<0|1>  (for the app's backup; USB only)
    Serial.printf("@WIFI ssid=%s pass=%s ap=%d\n", toHex(apSsid).c_str(), toHex(apPass).c_str(),
                  apEnabled ? 1 : 0);
    return true;
  }

  if (line.startsWith("PORTAL ")) {
    // PORTAL <id> -> activate that portal (as if chosen on the display)
    String id = line.substring(7);
    for (int i = 0; i < nPortals; i++)
      if (portals[i].id == id) { selectPortal(i); Serial.println("@OK"); return true; }
    Serial.println("@ERR Unknown portal");
    return true;
  }

  if (line == "AP 1" || line == "AP 0") {
    setApEnabled(line == "AP 1");
    Serial.println("@OK");
    return true;
  }

  if (line == "PMU") {
    Serial.println("@PMU " + pmuReport(pmuStatus()) + " | boot: " + pmuBootReport());
    return true;
  }

  if (line == "TOUCH") {
    int x, y, f;
    unsigned long age;
    displayLastTouch(x, y, f, age);
    Serial.printf("@TOUCH x=%d y=%d fingers=%d age=%lu\n", x, y, f, age);
    return true;
  }

  if (line == "RESET") {
    Serial.println("@OK");
    Serial.flush();
    factoryReset();
    delay(300);
    ESP.restart();
    return true;
  }

  if (line.startsWith("SET ")) {
    int sp = line.indexOf(' ', 4);
    if (sp < 0) { Serial.println("@ERR Invalid command"); return true; }
    String ssidHex = line.substring(4, sp);
    String passArg = line.substring(sp + 1);

    String ssid, pass;
    if (!fromHex(ssidHex, ssid)) { Serial.println("@ERR Invalid network name"); return true; }
    if (passArg == "-") pass = "";
    else if (passArg == "=") pass = apPass;
    else if (!fromHex(passArg, pass)) { Serial.println("@ERR Invalid password"); return true; }

    String err = validateSettings(ssid, pass);
    if (err.length()) { Serial.println("@ERR " + err); return true; }

    saveSettings(ssid, pass);
    Serial.println("@OK");
    Serial.flush();
    delay(300);
    ESP.restart();
    return true;
  }
  return false;
}

// ---------------------------------------------------------------- setup / loop

// Battery mode state (see updatePower)
static unsigned long wakeUntil = 0;   // full brightness until then (PWR button or touch)
static bool onBattery = false;

void setup() {
  BoardLink::begin(APP_ID, FW_VERSION);   // USB protocol for the ESP32 Toolbox app
  BoardLink::onCommand(handlePortalCommand);
  BoardLink::onInfo(portalInfo);
  BoardLink::onFileChanged(portalFileChanged);
  delay(500);
  Serial.println("\n[ESP32-Portal] start");
  setCpuFrequencyMhz(CPU_MHZ);          // power saving (default 240 MHz)

  pinMode(RESET_BUTTON_PIN, INPUT_PULLUP);
  loadSettings();

  if (!LittleFS.begin(true)) {
    Serial.println("[FS] LittleFS mount failed");
  } else if (!LittleFS.exists("/index.html")) {
    Serial.println("[FS] Note: /index.html is missing; upload a portal page with the admin app");
  }

  loadPortals();
  surveySetConfig(portalPath("survey.cfg"));
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
  wakeUntil = millis() + WAKE_SECONDS * 1000UL;   // start with a bright display
}

static PowerStatus powerNow = {false, -1, 0, false, false, 0, 0, "n/a"};

static void updateDisplay(bool force = false) {
  static unsigned long lastUpdate = 0;
  if (!force && millis() - lastUpdate < 500) return;
  lastUpdate = millis();

  PortalStatus s;
  s.online = apRunning && (WiFi.getMode() & WIFI_AP);
  s.ssid = effectiveSsid();
  s.portalNames = portalNames;
  s.portalCount = nPortals;
  s.portalActive = activePortal;
  s.portalRev = portalRev;
  s.open = apPass.isEmpty();
  s.clients = s.online ? WiFi.softAPgetStationNum() : 0;
  s.ip = apIP.toString();
  s.uptimeSec = millis() / 1000;
  s.survey = surveyActive();
  s.surveyType = surveyType();
  s.votes = surveyVotes();
  s.average = surveyAverage();
  s.summary = surveySummary();
  s.txPercent = TX_PCT[txLevel];
  // On battery the percentage is shown in steps of 5: fewer full-screen redraws
  int pct = powerNow.percent;
  if (powerNow.battery && !powerNow.charging) pct = min(100, (pct + 2) / 5 * 5);
  s.battery = powerNow.battery ? pct : -1;
  s.charging = powerNow.battery && powerNow.charging;
  displayUpdate(s);
}

// Battery mode: full brightness for WAKE_SECONDS after a touch or the PWR button,
// then dimmed, and after SCREEN_OFF_SECONDS more the display switches off.

static void updatePower() {
  static unsigned long lastPmu = 0;
  if (lastPmu == 0 || millis() - lastPmu > 2000) {   // power source / battery every 2 s
    lastPmu = millis();
    powerNow = pmuStatus();
  }
  if (pmuButtonPressed()) wakeUntil = millis() + WAKE_SECONDS * 1000UL;

  bool battery = powerNow.battery && !powerNow.usb;
  if (battery && !onBattery) wakeUntil = millis() + WAKE_SECONDS * 1000UL;   // just unplugged
  onBattery = battery;
  long left = (long)(wakeUntil - millis());                 // > 0: full brightness
  bool screenOn = !onBattery || left > -(long)(SCREEN_OFF_SECONDS * 1000UL);
  displaySetPower(screenOn);
  displaySetBrightness(onBattery && left <= 0 ? DIM_BRIGHTNESS : DISPLAY_BRIGHTNESS);
}

void loop() {
  if (apRunning) {
    dnsServer.processNextRequest();
    server.handleClient();
  }
  checkResetButton();
  BoardLink::poll();
  updatePower();
  Tap tap = displayPollTap();
  if (tap.touched) {                       // touching keeps a dimmed (battery) display bright
    wakeUntil = millis() + WAKE_SECONDS * 1000UL;
    updatePower();
  }
  if (tap.type == TAP_TXPOWER) {          // TX-knop: 25 -> 50 -> 75 -> 100%
    cycleTxPower();
    updateDisplay(true);
  } else if (tap.type == TAP_NETWORK) {   // tik op de netwerkkaart = netwerk aan/uit
    setApEnabled(!apEnabled);
    updateDisplay(true);
  } else if (tap.type == TAP_PORTAL) {    // portal gekozen in de lijst
    selectPortal(tap.index);
    updateDisplay(true);
  }
  updateDisplay();
  delay(onBattery ? 10 : 2);   // on battery the CPU idles more between rounds
}
