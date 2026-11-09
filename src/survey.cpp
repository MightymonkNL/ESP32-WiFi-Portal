#include <LittleFS.h>
#include <Preferences.h>
#include <set>
#include <esp_random.h>
#include <mbedtls/sha256.h>
#include "survey.h"

static uint8_t salt[16];
static bool saltLoaded = false;
static std::set<String> voters;   // voter keys for the current survey

static void loadSalt() {
  if (saltLoaded) return;
  Preferences p;
  p.begin("portal", false);
  if (p.getBytes("salt", salt, sizeof(salt)) != sizeof(salt)) {
    esp_fill_random(salt, sizeof(salt));
    p.putBytes("salt", salt, sizeof(salt));
  }
  p.end();
  saltLoaded = true;
}

static String cfgPath = SURVEY_CFG;   // survey of the active portal

void surveySetConfig(const String &path) { cfgPath = path; }

static bool active = false;
static String sId, sType, sQuestion;
static String labels[SURVEY_MAX_OPTIONS];
static int nOptions = 0;
static int counts[SURVEY_MAX_OPTIONS];
static int votes = 0;

static void resetCounts() {
  for (int i = 0; i < SURVEY_MAX_OPTIONS; i++) counts[i] = 0;
  votes = 0;
}

static String csvField(String s) {
  s.replace("\r", " ");
  s.replace("\n", " ");
  s.replace("\"", "\"\"");
  return "\"" + s + "\"";
}

// Count answers for the current survey id from the log.
static void countFromLog() {
  resetCounts();
  if (!active || !LittleFS.exists(SURVEY_LOG)) return;
  File f = LittleFS.open(SURVEY_LOG, "r");
  String prefix = sId + ",";
  while (f.available()) {
    String line = f.readStringUntil('\n');
    if (!line.startsWith(prefix)) continue;
    int idx = line.substring(prefix.length()).toInt();
    if (sType == "open") {
      votes++;
    } else if (idx >= 1 && idx <= nOptions) {
      counts[idx - 1]++;
      votes++;
    }
  }
  f.close();
}

static void loadVoters() {
  voters.clear();
  if (!LittleFS.exists(SURVEY_VOTERS)) return;
  File f = LittleFS.open(SURVEY_VOTERS, "r");
  while (f.available()) {
    String key = f.readStringUntil('\n');
    key.trim();
    if (key.length()) voters.insert(key);
  }
  f.close();
}

String surveyVoterKey(const uint8_t *deviceId, size_t len) {
  loadSalt();
  mbedtls_sha256_context ctx;
  uint8_t out[32];
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);
  mbedtls_sha256_update(&ctx, salt, sizeof(salt));
  mbedtls_sha256_update(&ctx, (const uint8_t *)sId.c_str(), sId.length());
  mbedtls_sha256_update(&ctx, deviceId, len);
  mbedtls_sha256_finish(&ctx, out);
  mbedtls_sha256_free(&ctx);
  char hex[33];
  for (int i = 0; i < 16; i++) sprintf(hex + i * 2, "%02x", out[i]);
  return String(hex);
}

bool surveyHasVoted(const String &voterKey) {
  return voterKey.length() && voters.count(voterKey) > 0;
}

void surveyReload() {
  active = false;
  nOptions = 0;
  if (LittleFS.exists(cfgPath)) {
    File f = LittleFS.open(cfgPath, "r");
    sId = f.readStringUntil('\n');
    sType = f.readStringUntil('\n');
    sQuestion = f.readStringUntil('\n');
    String opts = f.readStringUntil('\n');
    f.close();
    sId.trim();
    sType.trim();
    sQuestion.trim();
    opts.trim();
    int start = 0;
    while (nOptions < SURVEY_MAX_OPTIONS && start <= (int)opts.length() && opts.length()) {
      int tab = opts.indexOf('\t', start);
      labels[nOptions++] = tab < 0 ? opts.substring(start) : opts.substring(start, tab);
      if (tab < 0) break;
      start = tab + 1;
    }
    active = sId.length() && ((nOptions >= 2 && (sType == "stars" || sType == "yesno" || sType == "choice")) ||
                              sType == "open");
  }
  countFromLog();
  // Keys include the survey id, so a new survey starts with an empty list
  if (!active) {
    voters.clear();
  } else {
    loadVoters();
  }
  Serial.printf("[SURVEY] %s (%s, %d options, %d votes)\n", active ? "active" : "none", sType.c_str(),
                nOptions, votes);
}

void surveyBegin() { surveyReload(); }

void surveyClearLog() {
  LittleFS.remove(SURVEY_LOG);
  LittleFS.remove(SURVEY_VOTERS);
  voters.clear();
  resetCounts();
}

bool surveyActive() { return active; }
const String &surveyType() { return sType; }
int surveyVotes() { return votes; }

float surveyAverage() {
  if (!votes) return 0;
  long sum = 0;
  for (int i = 0; i < nOptions; i++) sum += (long)(i + 1) * counts[i];
  return (float)sum / votes;
}

String surveySummary() {
  if (!active) return "No survey active";
  if (!votes) return "No votes yet";
  if (sType == "open") return String(votes) + (votes == 1 ? " answer" : " answers");
  if (sType == "stars") {
    char buf[24];
    snprintf(buf, sizeof(buf), "%.1f / 5", surveyAverage());
    return buf;
  }
  String out;
  for (int i = 0; i < nOptions; i++) {
    if (i) out += "  ";
    out += labels[i] + " " + String(counts[i]);
  }
  return out;
}

// Shared by both record functions: checks, then appends one line to the log.
static String appendAnswer(int index, const String &answer, const String &time, const String &device,
                           const String &voterKey) {
  if (surveyHasVoted(voterKey)) return "Already answered";

  bool exists = LittleFS.exists(SURVEY_LOG);
  if (exists) {
    File chk = LittleFS.open(SURVEY_LOG, "r");
    size_t size = chk.size();
    chk.close();
    if (size > SURVEY_MAX_LOG_BYTES) return "Log is full";
  }

  // Only accept a plain date/time from the phone
  String t = time.substring(0, 25);
  for (char c : t)
    if (!isdigit((unsigned char)c) && c != '-' && c != ':' && c != ' ' && c != 'T' && c != '+') {
      t = "";
      break;
    }
  if (t.isEmpty()) t = "unknown";

  File f = LittleFS.open(SURVEY_LOG, "a");
  if (!f) return "Cannot write log";
  if (!exists) f.print("survey_id,answer_index,time,device,question,answer\n");
  f.print(sId + "," + String(index) + "," + csvField(t) + "," + csvField(device) + "," +
          csvField(sQuestion) + "," + csvField(answer) + "\n");
  f.close();

  if (voterKey.length()) {
    File v = LittleFS.open(SURVEY_VOTERS, "a");
    if (v) {
      v.print(voterKey + "\n");
      v.close();
    }
    voters.insert(voterKey);
  }

  if (index >= 1) counts[index - 1]++;
  votes++;
  Serial.printf("[SURVEY] answer %d from %s\n", index, device.c_str());
  return "";
}

String surveyRecord(int index, const String &time, const String &device, const String &voterKey) {
  if (!active || sType == "open") return "No survey active";
  if (index < 1 || index > nOptions) return "Invalid answer";
  return appendAnswer(index, labels[index - 1], time, device, voterKey);
}

String surveyRecordText(const String &text, const String &time, const String &device, const String &voterKey) {
  if (!active || sType != "open") return "No survey active";
  // Keep printable text only (control characters become spaces), max SURVEY_MAX_TEXT
  // UTF-8 characters; never cut a multi-byte character in half.
  String clean;
  int chars = 0;
  for (size_t i = 0; i < text.length(); i++) {
    uint8_t b = (uint8_t)text[i];
    bool lead = (b & 0xC0) != 0x80;
    if (lead && chars == SURVEY_MAX_TEXT) break;
    if (lead) chars++;
    clean += (b < 0x20 || b == 0x7F) ? ' ' : (char)b;
  }
  clean.trim();
  if (clean.isEmpty()) return "Empty answer";
  // Excel would run text starting with = + - @ as a formula: store it as plain text
  char first = clean[0];
  if (first == '=' || first == '+' || first == '-' || first == '@') clean = "'" + clean;
  return appendAnswer(0, clean, time, device, voterKey);
}

// ------------------------------------------------------------------ device type

static String versionAfter(const String &ua, const char *key, char sep) {
  int p = ua.indexOf(key);
  if (p < 0) return "";
  p += strlen(key);
  String v;
  while (p < (int)ua.length() && (isdigit((unsigned char)ua[p]) || ua[p] == sep) && v.length() < 6) {
    if (ua[p] == sep) break;   // major version only
    v += ua[p++];
  }
  return v;
}

String deviceFromUserAgent(const String &ua) {
  if (ua.indexOf("iPhone") >= 0) {
    String v = versionAfter(ua, "iPhone OS ", '_');
    return v.length() ? "iPhone (iOS " + v + ")" : "iPhone";
  }
  if (ua.indexOf("iPad") >= 0) {
    String v = versionAfter(ua, "CPU OS ", '_');
    return v.length() ? "iPad (iPadOS " + v + ")" : "iPad";
  }
  if (ua.indexOf("Android") >= 0) {
    String v = versionAfter(ua, "Android ", '.');
    String brand;
    if (ua.indexOf("SM-") >= 0 || ua.indexOf("Samsung") >= 0) brand = "Samsung";
    else if (ua.indexOf("Pixel") >= 0) brand = "Pixel";
    String out = v.length() ? "Android " + v : "Android";
    return brand.length() ? out + " (" + brand + ")" : out;
  }
  if (ua.indexOf("CrOS") >= 0) return "ChromeOS";
  if (ua.indexOf("Windows") >= 0) return "Windows";
  if (ua.indexOf("Macintosh") >= 0 || ua.indexOf("Mac OS X") >= 0) return "Mac";
  if (ua.indexOf("Linux") >= 0) return "Linux";
  return "Other";
}
