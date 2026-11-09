// Statusscherm voor de Waveshare ESP32-S3-Touch-AMOLED-1.8 (368x448).
//
// Er bestaan twee hardwarerevisies met dezelfde pinnen:
//   origineel: SH8601-scherm + FT3168-touch (I2C 0x38)
//   V2:        CO5300-scherm + CST820-touch (I2C 0x15)
// De revisie wordt herkend aan het touch-IC dat op de I2C-bus antwoordt.
//
// Belangrijk: deze AMOLED-controllers accepteren alleen tekengebieden met
// even begin- en eindcoördinaten. Losse pixels/tekst rechtstreeks tekenen
// geeft strepen. Daarom wordt alles in een schermbuffer (PSRAM) getekend en
// in één keer als volledig scherm verstuurd.
//
// Vormgeving: Material Design dark theme, met pure zwarte achtergrond
// (spaart stroom en voorkomt oplichten op AMOLED).

#include <Wire.h>
#include <U8g2lib.h>               // lettertypes
#include <Arduino_GFX_Library.h>
#include "config.h"
#include "status_display.h"

#define C565(r, g, b) (uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3))

static const uint16_t COL_BG      = C565(0x00, 0x00, 0x00);
static const uint16_t COL_CARD    = C565(0x1E, 0x1E, 0x1E);
static const uint16_t COL_TEXT    = C565(0xE1, 0xE1, 0xE1);
static const uint16_t COL_MUTED   = C565(0x9E, 0x9E, 0x9E);
static const uint16_t COL_PRIMARY = C565(0x9C, 0x89, 0xD7);
static const uint16_t COL_ONLINE  = C565(0x5D, 0xBB, 0x6A);
static const uint16_t COL_OFFLINE = C565(0xFF, 0x9E, 0x71);

static const uint8_t EXPANDER_ADDR = 0x20;   // TCA9554
static const uint8_t TOUCH_V2_ADDR = 0x15;   // CST820  -> CO5300
static const uint8_t TOUCH_V1_ADDR = 0x38;   // FT3168  -> SH8601

static Arduino_DataBus *bus = nullptr;
static Arduino_OLED *panel = nullptr;     // het fysieke scherm
static Arduino_Canvas *cv = nullptr;      // schermbuffer waarin getekend wordt
static const char *driverName = "none";
static uint8_t touchAddr = 0;             // 0x15 (CST820) of 0x38 (FT3168)
static int lastTouchX = -1, lastTouchY = -1, lastFingers = 0;
static unsigned long lastTouchAt = 0;
static bool wasTouching = false;

const char *displayDriver() { return driverName; }

static bool drawn = false;
static PortalStatus last;
static int shiftStep = 0;
static unsigned long lastShift = 0;
static int ox = 0, oy = 0;   // kleine verschuiving tegen inbranden

// Indeling
static const int M = 16;                       // marge
static const int CW = LCD_WIDTH - 2 * M - 4;   // kaartbreedte (ruimte voor verschuiving)
static const int Y_SSID = 76, H_SSID = 120;
static const int Y_ROW = Y_SSID + H_SSID + 12, H_ROW = 100;       // devices | votes
static const int Y_SURVEY = Y_ROW + H_ROW + 12, H_SURVEY = 88;    // survey result
static const int Y_FOOTER = Y_SURVEY + H_SURVEY + 28;             // version / IP
static const int GAP = 10;
static const int W3 = (CW - 2 * GAP) / 3;                         // devices | votes | portal
static const int X_DEV = M, X_VOTES = M + W3 + GAP, X_PORTAL = M + 2 * (W3 + GAP);

// Portal list (opened with the portal button)
static const int LIST_Y = 78;
// Rows as large as the screen allows: 64 px high for up to 5 portals, smaller beyond that
static int listStep(int n) { return min(72, (LCD_HEIGHT - LIST_Y - 8) / max(1, n)); }
static int listRow(int n) { return listStep(n) - 8; }
static const unsigned long LIST_TIMEOUT_MS = 15000;
static bool listMode = false;
static unsigned long listSince = 0;

// ---------------------------------------------------------------- I2C-hulp

static bool i2cPresent(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

static void i2cWriteReg(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

// Zelfde volgorde als Waveshare's board_variant.c:
// EXIO0 = LCD-reset, EXIO1 = schermvoeding, EXIO2 = touch-reset, EXIO7 = SD-CS
static void resetViaExpander() {
  if (!i2cPresent(EXPANDER_ADDR)) {
    Serial.println("[LCD] IO expander not found, reset skipped");
    return;
  }
  const uint8_t mask = 0x01 | 0x02 | 0x04 | 0x80;
  i2cWriteReg(EXPANDER_ADDR, 0x03, (uint8_t)~mask);  // deze pinnen als uitgang
  i2cWriteReg(EXPANDER_ADDR, 0x01, 0x80);            // reset actief, SD-CS hoog
  delay(20);
  i2cWriteReg(EXPANDER_ADDR, 0x01, mask);            // alles vrijgeven
  delay(150);
}

// ---------------------------------------------------------------- tekenen

static void drawTextFit(const String &text, int x, int baseline, int maxW,
                        const uint8_t *const *fonts, int nFonts, uint16_t color) {
  int16_t bx, by;
  uint16_t w, h;
  for (int i = 0; i < nFonts; i++) {
    cv->setFont(fonts[i]);
    cv->getTextBounds(text, 0, 0, &bx, &by, &w, &h);
    if (w <= maxW || i == nFonts - 1) break;
  }
  cv->setTextColor(color);
  cv->setCursor(x, baseline);
  cv->print(text);
}

static void drawCard(int y, int h) {
  cv->fillRoundRect(M + ox, y + oy, CW, h, 18, COL_CARD);
}

static void drawLabel(const char *text, int y) {
  cv->setFont(u8g2_font_helvB10_tf);
  cv->setTextColor(COL_MUTED);
  cv->setCursor(M + 20 + ox, y + oy);
  cv->print(text);
}

static void drawHeader(const PortalStatus &s) {
  cv->setFont(u8g2_font_helvB12_tf);
  cv->setTextColor(COL_PRIMARY);
  cv->setCursor(M + 4 + ox, 48 + oy);
  cv->print("ESP32-PORTAL");

  // Battery indicator between the title and the status pill
  if (s.battery >= 0) {
    const int bw = 28, bh = 15;
    int bx0 = 168 + ox, by0 = 36 + oy;
    uint16_t bcol = s.charging ? COL_ONLINE : (s.battery <= 15 ? COL_OFFLINE : COL_TEXT);
    cv->drawRoundRect(bx0, by0, bw, bh, 3, COL_MUTED);
    cv->fillRect(bx0 + bw, by0 + 4, 3, bh - 8, COL_MUTED);           // terminal
    int fill = (bw - 4) * constrain(s.battery, 0, 100) / 100;
    if (fill > 0) cv->fillRect(bx0 + 2, by0 + 2, fill, bh - 4, bcol);
    if (s.charging) {                                                // small bolt
      int cx = bx0 + bw / 2, cy = by0 + bh / 2;
      cv->fillTriangle(cx + 2, cy - 6, cx - 4, cy + 1, cx + 1, cy + 1, COL_BG);
      cv->fillTriangle(cx - 2, cy + 6, cx + 4, cy - 1, cx - 1, cy - 1, COL_BG);
    }
    cv->setFont(u8g2_font_helvB10_tf);
    cv->setTextColor(bcol);
    cv->setCursor(bx0 + bw + 7, by0 + 13);
    cv->print(String(s.battery) + "%");
  }

  const char *txt = s.online ? "ONLINE" : "OFFLINE";
  uint16_t col = s.online ? COL_ONLINE : COL_OFFLINE;
  int16_t bx, by;
  uint16_t w, h;
  cv->getTextBounds(txt, 0, 0, &bx, &by, &w, &h);
  int pillW = w + 46, pillH = 34;
  int px = LCD_WIDTH - M - 4 - pillW + ox, py = 26 + oy;
  cv->drawRoundRect(px, py, pillW, pillH, pillH / 2, col);
  cv->fillCircle(px + 18, py + pillH / 2, 6, col);
  cv->setTextColor(col);
  cv->setCursor(px + 32, py + 23);
  cv->print(txt);
}

// TX power button at the bottom right of the network card
static const int TX_W = 112, TX_H = 36;
static int txX() { return M + CW - 16 - TX_W; }
static int txY() { return Y_SSID + H_SSID - 12 - TX_H; }

static void drawTxButton(const PortalStatus &s) {
  int x = txX() + ox, y = txY() + oy;
  cv->fillRoundRect(x, y, TX_W, TX_H, TX_H / 2, C565(0x2C, 0x2C, 0x2C));
  cv->drawRoundRect(x, y, TX_W, TX_H, TX_H / 2, COL_PRIMARY);
  // signal bars: one per 25%
  for (int i = 0; i < 4; i++) {
    int h = 5 + i * 4, bx = x + 14 + i * 6;
    cv->fillRect(bx, y + 26 - h, 4, h, (i + 1) * 25 <= s.txPercent ? COL_PRIMARY : C565(0x55, 0x55, 0x55));
  }
  cv->setFont(u8g2_font_helvB12_tf);
  cv->setTextColor(COL_TEXT);
  cv->setCursor(x + 46, y + 24);
  cv->print(String(s.txPercent) + "%");
}

static void drawSsid(const PortalStatus &s) {
  drawCard(Y_SSID, H_SSID);
  drawLabel("NETWORK", Y_SSID + 32);

  // Schakelaar rechtsboven op de kaart (tik op de kaart = aan/uit)
  const int sw = 56, sh = 30;
  int sx = M + CW - 20 - sw + ox, sy = Y_SSID + 14 + oy;
  cv->fillRoundRect(sx, sy, sw, sh, sh / 2, s.online ? COL_ONLINE : C565(0x3D, 0x3D, 0x3D));
  int knobX = s.online ? sx + sw - sh / 2 : sx + sh / 2;
  cv->fillCircle(knobX, sy + sh / 2, sh / 2 - 4, s.online ? COL_TEXT : COL_MUTED);
  static const uint8_t *const fonts[] = {u8g2_font_helvB24_tf, u8g2_font_helvB18_tf,
                                         u8g2_font_helvB14_tf, u8g2_font_helvB10_tf};
  drawTextFit(s.ssid, M + 20 + ox, Y_SSID + 74 + oy, CW - 40, fonts, 4,
              s.online ? COL_TEXT : COL_MUTED);
  cv->setFont(u8g2_font_helvR12_tf);
  cv->setTextColor(s.online ? COL_MUTED : COL_OFFLINE);
  cv->setCursor(M + 20 + ox, Y_SSID + 104 + oy);
  if (!s.online) cv->print("Network is off");
  else cv->print(s.open ? "Open network" : "Password protected");
  drawTxButton(s);
}

// Text that does not fit in maxW is shortened with "..." (in the current font)
static String ellipsize(String text, int maxW) {
  int16_t bx, by;
  uint16_t w, h;
  cv->getTextBounds(text, 0, 0, &bx, &by, &w, &h);
  if (w <= maxW) return text;
  while (text.length() > 1) {
    text.remove(text.length() - 1);
    while (text.length() && ((uint8_t)text[text.length() - 1] & 0xC0) == 0x80) text.remove(text.length() - 1);
    cv->getTextBounds(text + "...", 0, 0, &bx, &by, &w, &h);
    if (w <= maxW) break;
  }
  return text + "...";
}

// Small card with a label and a big number
static void drawNumberCard(int x, const char *label, const String &value, uint16_t color) {
  cv->fillRoundRect(x + ox, Y_ROW + oy, W3, H_ROW, 18, COL_CARD);
  cv->setFont(u8g2_font_helvB10_tf);
  cv->setTextColor(COL_MUTED);
  cv->setCursor(x + 16 + ox, Y_ROW + 30 + oy);
  cv->print(label);
  cv->setFont(value.length() > 3 ? u8g2_font_logisoso28_tn : u8g2_font_logisoso42_tn);
  cv->setTextColor(color);
  cv->setCursor(x + 16 + ox, Y_ROW + 84 + oy);
  cv->print(value);
}

// Button with the active portal; tapping it opens the list
static void drawPortalButton(const PortalStatus &s) {
  int x = X_PORTAL + ox, y = Y_ROW + oy;
  bool multi = s.portalCount > 1;
  cv->fillRoundRect(x, y, W3, H_ROW, 18, COL_CARD);
  if (multi) cv->drawRoundRect(x, y, W3, H_ROW, 18, COL_PRIMARY);
  cv->setFont(u8g2_font_helvB10_tf);
  cv->setTextColor(COL_MUTED);
  cv->setCursor(x + 16, y + 30);
  cv->print("PORTAL");
  String name = s.portalCount ? s.portalNames[s.portalActive] : String("Default");
  cv->setFont(u8g2_font_helvB12_tf);
  cv->setTextColor(COL_TEXT);
  cv->setCursor(x + 16, y + 60);
  cv->print(ellipsize(name, W3 - 28));
  cv->setFont(u8g2_font_helvR08_tf);
  cv->setTextColor(multi ? COL_PRIMARY : COL_MUTED);
  cv->setCursor(x + 16, y + 84);
  cv->print(multi ? String(s.portalCount) + " portals  >" : String("1 portal"));
}

static void drawList(const PortalStatus &s) {
  cv->fillScreen(COL_BG);
  cv->setFont(u8g2_font_helvB12_tf);
  cv->setTextColor(COL_PRIMARY);
  cv->setCursor(M + 4 + ox, 48 + oy);
  cv->print("CHOOSE PORTAL");
  cv->setFont(u8g2_font_helvR10_tf);
  cv->setTextColor(COL_MUTED);
  cv->setCursor(LCD_WIDTH - M - 56 + ox, 48 + oy);
  cv->print("Close");
  const int step = listStep(s.portalCount), rowH = listRow(s.portalCount);
  const bool big = rowH >= 52;
  const int dot = big ? 10 : 7, textX = big ? 56 : 42;
  for (int i = 0; i < s.portalCount; i++) {
    int y = LIST_Y + i * step + oy, x = M + ox, cy = y + rowH / 2;
    bool on = i == s.portalActive;
    cv->fillRoundRect(x, y, CW, rowH, 16, COL_CARD);
    if (on) {
      cv->drawRoundRect(x, y, CW, rowH, 16, COL_PRIMARY);
      cv->drawRoundRect(x + 1, y + 1, CW - 2, rowH - 2, 15, COL_PRIMARY);
      cv->fillCircle(x + textX / 2, cy, dot, COL_PRIMARY);
    } else {
      cv->drawCircle(x + textX / 2, cy, dot, COL_MUTED);
    }
    cv->setFont(big ? u8g2_font_helvB18_tf : u8g2_font_helvB14_tf);
    cv->setTextColor(on ? COL_TEXT : COL_MUTED);
    cv->setCursor(x + textX, cy + (big ? 9 : 7));
    cv->print(ellipsize(s.portalNames[i], CW - textX - 14));
  }
}

static void drawStar(int cx, int cy, int r, uint16_t color) {
  int px[10], py[10];
  for (int i = 0; i < 10; i++) {
    float a = -PI / 2 + i * PI / 5;
    float rr = (i % 2 == 0) ? r : r * 0.45f;
    px[i] = cx + (int)roundf(cosf(a) * rr);
    py[i] = cy + (int)roundf(sinf(a) * rr);
  }
  for (int i = 0; i < 10; i++) {
    int j = (i + 1) % 10;
    cv->fillTriangle(cx, cy, px[i], py[i], px[j], py[j], color);
  }
}

static void drawSurvey(const PortalStatus &s) {
  drawCard(Y_SURVEY, H_SURVEY);
  drawLabel("SURVEY RESULT", Y_SURVEY + 30);
  int x = M + 20 + ox, base = Y_SURVEY + 70 + oy;
  if (s.survey && s.surveyType == "stars" && s.votes > 0) {
    int filled = (int)roundf(s.average);
    for (int i = 0; i < 5; i++)
      drawStar(x + 14 + i * 34, base - 10, 14, i < filled ? COL_PRIMARY : C565(0x3D, 0x3D, 0x3D));
    cv->setFont(u8g2_font_helvB18_tf);
    cv->setTextColor(COL_TEXT);
    cv->setCursor(x + 5 * 34 + 10, base);
    cv->print(s.summary);
    return;
  }
  static const uint8_t *const fonts[] = {u8g2_font_helvB18_tf, u8g2_font_helvB14_tf,
                                         u8g2_font_helvB10_tf, u8g2_font_helvB08_tf};
  drawTextFit(s.summary, x, base, CW - 40, fonts, 4,
              s.survey && s.votes > 0 ? COL_TEXT : COL_MUTED);
}

static void drawFooter(const PortalStatus &s) {
  String txt = "v" FW_VERSION "   \xC2\xB7   " + s.ip;   // middle dot (UTF-8)
  cv->setFont(u8g2_font_helvR10_tf);
  int16_t bx, by;
  uint16_t w, h;
  cv->getTextBounds(txt, 0, 0, &bx, &by, &w, &h);
  cv->setTextColor(COL_MUTED);
  cv->setCursor((LCD_WIDTH - w) / 2 + ox, Y_FOOTER + oy);
  cv->print(txt);
}

static void render(const PortalStatus &s) {
  if (listMode) {
    drawList(s);
    cv->flush(true);
    return;
  }
  cv->fillScreen(COL_BG);
  drawHeader(s);
  drawSsid(s);
  drawNumberCard(X_DEV, "DEVICES", String(s.clients), s.clients > 0 ? COL_PRIMARY : COL_TEXT);
  drawNumberCard(X_VOTES, "VOTES", s.survey ? String(s.votes) : String("-"),
                 s.votes > 0 ? COL_PRIMARY : COL_TEXT);
  drawPortalButton(s);
  drawSurvey(s);
  drawFooter(s);
  cv->flush(true);   // volledig scherm in één keer: altijd correct uitgelijnd
}

// ---------------------------------------------------------------- touch
//
// FT3168 en CST820 gebruiken dezelfde registers:
//   0x02 aantal vingers, 0x03/0x04 X (4 + 8 bits), 0x05/0x06 Y (4 + 8 bits)

static bool readTouch(int &x, int &y, int &fingers) {
  Wire.beginTransmission(touchAddr);
  Wire.write(0x02);
  if (Wire.endTransmission(false) != 0) return false;   // touch-IC slaapt: geen aanraking
  if (Wire.requestFrom(touchAddr, (uint8_t)5) != 5) return false;
  uint8_t d[5];
  for (int i = 0; i < 5; i++) d[i] = Wire.read();
  fingers = d[0] & 0x0F;
  x = ((d[1] & 0x0F) << 8) | d[2];
  y = ((d[3] & 0x0F) << 8) | d[4];
  return true;
}

void displayLastTouch(int &x, int &y, int &fingers, unsigned long &ageMs) {
  x = lastTouchX;
  y = lastTouchY;
  fingers = lastFingers;
  ageMs = lastTouchAt ? millis() - lastTouchAt : 0;
}

static int currentBrightness = -1;

Tap displayPollTap() {
  Tap none = {TAP_NONE, -1, false};
  if (!touchAddr) return none;
  static unsigned long lastPoll = 0, lastTap = 0;
  if (millis() - lastPoll < 30) return none;
  lastPoll = millis();

  int x, y, fingers = 0;
  if (!readTouch(x, y, fingers)) fingers = 0;
  bool touching = fingers > 0 && fingers < 3 && x < LCD_WIDTH && y < LCD_HEIGHT;
  if (touching) {
    lastTouchX = x;
    lastTouchY = y;
    lastFingers = fingers;
    lastTouchAt = millis();
  }

  Tap tap = none;
  // Alleen bij het neerzetten van de vinger, en niet vlak na de vorige tik
  if (touching && !wasTouching && millis() - lastTap > 500) {
    lastTap = millis();
    bool dimmed = currentBrightness >= 0 && currentBrightness < DISPLAY_BRIGHTNESS;
    if (dimmed) {
      Serial.printf("[TOUCH] tap at %d,%d wakes the display\n", x, y);
      tap = {TAP_WAKE, -1, true};
    } else if (listMode) {
      int step = listStep(last.portalCount);
      int row = (y - LIST_Y) / step;
      // the 8 px gaps between rows count as the row above, so near-misses still select
      bool onRow = y >= LIST_Y && row < last.portalCount;
      Serial.printf("[TOUCH] list tap at %d,%d -> %d\n", x, y, onRow ? row : -1);
      listMode = false;           // any tap closes the list
      drawn = false;
      tap = onRow ? Tap{TAP_PORTAL, row, true} : Tap{TAP_NONE, -1, true};
    } else {
      bool inTx = x >= txX() - 10 && x <= txX() + TX_W + 10 && y >= txY() - 10 && y <= txY() + TX_H + 10;
      bool inCard = !inTx && x >= M - 8 && x <= M + CW + 8 && y >= Y_SSID - 8 && y <= Y_SSID + H_SSID + 8;
      bool inPortal = x >= X_PORTAL - 6 && x <= M + CW + 8 && y >= Y_ROW - 6 && y <= Y_ROW + H_ROW + 6;
      Serial.printf("[TOUCH] tap at %d,%d%s\n", x, y, inCard ? " (network card)" : inPortal ? " (portal)" : "");
      tap = inTx ? Tap{TAP_TXPOWER, -1, true} : inCard ? Tap{TAP_NETWORK, -1, true} : Tap{TAP_NONE, -1, true};
      if (inPortal && last.portalCount > 1) {
        listMode = true;
        listSince = millis();
        drawn = false;
      }
    }
  }
  wasTouching = touching;
  return tap;
}

// ---------------------------------------------------------------- API

static bool panelOn = true;

void displaySetPower(bool on) {
  if (!panel || !cv || on == panelOn) return;
  panelOn = on;
  if (on) {
    panel->displayOn();
    drawn = false;              // redraw everything that changed while it was off
    currentBrightness = -1;     // next displaySetBrightness() sends the level again
  } else {
    panel->setBrightness(0);
    panel->displayOff();
    currentBrightness = 0;      // counts as dimmed: the first touch only wakes it
  }
  Serial.printf("[LCD] display %s\n", on ? "on" : "off (battery saving)");
}

void displaySetBrightness(uint8_t level) {
  if (!panel || !cv || !panelOn || level == currentBrightness) return;
  panel->setBrightness(level);
  currentBrightness = level;
}

bool displayBegin() {
  Wire.begin(IIC_SDA, IIC_SCL);
  resetViaExpander();

  bool v2 = i2cPresent(TOUCH_V2_ADDR);
  bool v1 = i2cPresent(TOUCH_V1_ADDR);
  Serial.printf("[LCD] touch-IC: CST820(0x15)=%s FT3168(0x38)=%s -> %s\n", v2 ? "yes" : "no",
                v1 ? "yes" : "no", v2 ? "CO5300" : "SH8601");
  Serial.printf("[LCD] PSRAM: %u bytes free\n", (unsigned)ESP.getFreePsram());

  bus = new Arduino_ESP32QSPI(LCD_CS, LCD_SCLK, LCD_SDIO0, LCD_SDIO1, LCD_SDIO2, LCD_SDIO3);
  if (v2) {
    panel = new Arduino_CO5300(bus, GFX_NOT_DEFINED, 0, LCD_WIDTH, LCD_HEIGHT, 16, 0, 0, 0);
  } else {
    panel = new Arduino_SH8601(bus, GFX_NOT_DEFINED, 0, LCD_WIDTH, LCD_HEIGHT);
  }
  cv = new Arduino_Canvas(LCD_WIDTH, LCD_HEIGHT, panel);
  if (!cv->begin()) {   // initialiseert ook het scherm en reserveert de buffer
    Serial.println("[LCD] initialisation failed (no PSRAM?)");
    cv = nullptr;
    return false;
  }
  driverName = v2 ? "CO5300" : "SH8601";
  touchAddr = v2 ? TOUCH_V2_ADDR : (v1 ? TOUCH_V1_ADDR : 0);
  if (v2) i2cWriteReg(TOUCH_V2_ADDR, 0xFE, 0x01);   // CST8xx: automatische slaapstand uit
  cv->setUTF8Print(true);
  cv->fillScreen(COL_BG);
  cv->flush(true);
  panel->setBrightness(DISPLAY_BRIGHTNESS);
  currentBrightness = DISPLAY_BRIGHTNESS;
  lastShift = millis();
  Serial.println("[LCD] display active");
  return true;
}

void displayUpdate(const PortalStatus &s) {
  if (!cv || !panelOn) return;    // nothing to draw while the panel is off

  // Elke 5 minuten alles 2 px verschuiven tegen inbranden van het AMOLED
  if (millis() - lastShift > 5UL * 60UL * 1000UL) {
    lastShift = millis();
    shiftStep = (shiftStep + 1) % 4;
    static const int8_t dx[] = {0, 2, 2, 0}, dy[] = {0, 0, 2, 2};
    ox = dx[shiftStep];
    oy = dy[shiftStep];
    drawn = false;
  }

  if (listMode && millis() - listSince > LIST_TIMEOUT_MS) {
    listMode = false;
    drawn = false;
  }
  bool changed = !drawn || s.portalRev != last.portalRev || s.portalCount != last.portalCount ||
                 s.txPercent != last.txPercent ||
                 s.online != last.online || s.ssid != last.ssid ||
                 s.open != last.open || s.clients != last.clients || s.ip != last.ip ||
                 s.survey != last.survey || s.votes != last.votes || s.summary != last.summary ||
                 s.battery != last.battery || s.charging != last.charging;
  if (!changed) return;
  render(s);
  last = s;
  drawn = true;
}
