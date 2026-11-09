#include "BoardLink.h"
#include <LittleFS.h>
#include <mbedtls/base64.h>

namespace BoardLink {

static const char *appId = "unknown";
static const char *fwVersion = "?";
static CommandHandler commandHandler = nullptr;
static InfoHandler infoHandler = nullptr;
static FileHandler fileHandler = nullptr;

static const char *UPLOAD_TMP = "/.upload";
static File upFile;
static String upPath;
static size_t upSize = 0, upGot = 0;
static bool upActive = false;

void begin(const char *app, const char *fw, unsigned long baud) {
  appId = app;
  fwVersion = fw;
  Serial.setRxBufferSize(4096);   // default 256 B: too small for FDATA lines (~690 characters)
  Serial.begin(baud);
}

void onCommand(CommandHandler handler) { commandHandler = handler; }
void onInfo(InfoHandler handler) { infoHandler = handler; }
void onFileChanged(FileHandler handler) { fileHandler = handler; }

String toHex(const String &s) {
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

bool fromHex(const String &hex, String &out) {
  if (hex.length() % 2) return false;
  out = "";
  for (size_t i = 0; i < hex.length(); i += 2) {
    char buf[3] = {hex[i], hex[i + 1], 0};
    char *end;
    long v = strtol(buf, &end, 16);
    if (*end != 0 || v == 0) return false;  // invalid or NUL character
    out += (char)v;
  }
  return true;
}

bool validPath(const String &p) {
  if (p.length() < 2 || p.length() > 31 || p[0] != '/' || p[1] == '.') return false;
  for (size_t i = 1; i < p.length(); i++) {
    char c = p[i];
    if (!(isalnum((unsigned char)c) || c == '.' || c == '_' || c == '-')) return false;
  }
  return true;
}

static String macAddress() {
  uint64_t mac = ESP.getEfuseMac();
  char buf[18];
  snprintf(buf, sizeof(buf), "%02x:%02x:%02x:%02x:%02x:%02x", (uint8_t)mac, (uint8_t)(mac >> 8),
           (uint8_t)(mac >> 16), (uint8_t)(mac >> 24), (uint8_t)(mac >> 32), (uint8_t)(mac >> 40));
  return buf;
}

static void abortUpload() {
  if (upFile) upFile.close();
  LittleFS.remove(UPLOAD_TMP);
  upActive = false;
}

static void changed(const String &path) {
  if (fileHandler) fileHandler(path);
}

// Returns true when the line was a base command.
static bool handleBase(const String &line) {
  if (line == "INFO") {
    String extra = infoHandler ? infoHandler() : String();
    Serial.printf("@INFO app=%s fw=%s mac=%s%s%s\n", appId, fwVersion, macAddress().c_str(),
                  extra.length() ? " " : "", extra.c_str());
    return true;
  }
  if (line == "REBOOT") {
    Serial.println("@OK");
    Serial.flush();
    delay(300);
    ESP.restart();
    return true;
  }
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
    changed(upPath);
    Serial.println("@OK");
    return true;
  }
  if (line == "FABORT") { abortUpload(); Serial.println("@OK"); return true; }
  if (line.startsWith("FDEL ")) {
    String path = line.substring(5);
    if (!validPath(path)) { Serial.println("@ERR Invalid path"); return true; }
    LittleFS.remove(path);
    changed(path);
    Serial.println("@OK");
    return true;
  }
  if (line.startsWith("FREAD ")) {
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
  if (line == "FLIST") {
    String out = "@FLIST";
    File root = LittleFS.open("/");
    for (File f = root.openNextFile(); f; f = root.openNextFile()) {
      String name = f.name();
      if (name.startsWith("/")) name = name.substring(1);
      if (!f.isDirectory() && !name.startsWith(".")) {
        out += ' ';
        out += name;
        out += ':';
        out += String((unsigned)f.size());
      }
      f.close();
    }
    root.close();
    Serial.println(out);
    return true;
  }
  return false;
}

static void handleLine(String line) {
  line.trim();
  if (line.isEmpty()) return;
  if (handleBase(line)) return;
  if (commandHandler && commandHandler(line)) return;
  Serial.println("@ERR Unknown command");
}

void poll() {
  static String buf;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (buf.length()) handleLine(buf);
      buf = "";
    } else if (buf.length() < 1024) {
      buf += c;
    }
  }
}

}  // namespace BoardLink
