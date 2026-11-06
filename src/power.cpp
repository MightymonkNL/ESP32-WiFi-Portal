#include <Wire.h>
#define XPOWERS_CHIP_AXP2101
#include <XPowersLib.h>
#include "config.h"
#include "power.h"

static XPowersPMU pmu;
static bool present = false;
static String bootReport = "not read";

static const uint8_t EXPANDER_ADDR = 0x20;   // TCA9554, input register 0x00
static const uint8_t EXIO_PWR_BIT = 0x10;    // EXIO4 = PWR button (Waveshare wiki)
static int expanderIdle = -1;                // level of EXIO4 when not pressed
static unsigned long buttonPresses = 0;

static int readExpanderPwr() {
  Wire.beginTransmission(EXPANDER_ADDR);
  Wire.write(0x00);
  if (Wire.endTransmission(false) != 0) return -1;
  if (Wire.requestFrom(EXPANDER_ADDR, (uint8_t)1) != 1) return -1;
  return (Wire.read() & EXIO_PWR_BIT) ? 1 : 0;
}

bool pmuButtonPressed() {
  static unsigned long lastPoll = 0, lastPress = 0;
  static int lastLevel = -1;
  if (millis() - lastPoll < 50) return false;
  lastPoll = millis();

  bool pressed = false;
  // 1) AXP2101 power key: short-press interrupt flag
  if (present) {
    pmu.getIrqStatus();
    if (pmu.isPekeyShortPressIrq()) pressed = true;
    pmu.clearIrqStatus();
  }
  // 2) IO expander EXIO4: edge away from the idle level
  int level = readExpanderPwr();
  if (level >= 0 && expanderIdle >= 0) {
    if (level != expanderIdle && lastLevel == expanderIdle) pressed = true;
    lastLevel = level;
  }

  // Debounce: both sources report the same press (on press and on release),
  // so count at most one press per second.
  if (pressed && millis() - lastPress > 1000) {
    lastPress = millis();
    buttonPresses++;
    Serial.println("[PWR] button pressed");
    return true;
  }
  return false;
}

static const char *chargerText(uint8_t st) {
  switch (st) {
    case XPOWERS_AXP2101_CHG_TRI_STATE:  return "trickle";
    case XPOWERS_AXP2101_CHG_PRE_STATE:  return "pre-charge";
    case XPOWERS_AXP2101_CHG_CC_STATE:   return "constant-current";
    case XPOWERS_AXP2101_CHG_CV_STATE:   return "constant-voltage";
    case XPOWERS_AXP2101_CHG_DONE_STATE: return "done";
    case XPOWERS_AXP2101_CHG_STOP_STATE: return "stopped";
    default:                             return "unknown";
  }
}

bool pmuPresent() { return present; }

PowerStatus pmuStatus() {
  PowerStatus s = {false, -1, 0, false, false, 0, 0, "n/a"};
  if (!present) return s;
  s.battery = pmu.isBatteryConnect();
  s.battMv = pmu.getBattVoltage();
  s.percent = s.battery ? pmu.getBatteryPercent() : -1;
  s.charging = pmu.isCharging();
  s.usb = pmu.isVbusIn();
  s.vbusMv = pmu.getVbusVoltage();
  s.sysMv = pmu.getSystemVoltage();
  s.charger = chargerText(pmu.getChargerStatus());
  return s;
}

String pmuReport(const PowerStatus &s) {
  char buf[240];
  snprintf(buf, sizeof(buf),
           "battery=%s percent=%d batt_mv=%d charging=%s charger=%s usb=%s vbus_mv=%d sys_mv=%d "
           "button_presses=%lu",
           s.battery ? "yes" : "no", s.percent, s.battMv, s.charging ? "yes" : "no", s.charger,
           s.usb ? "yes" : "no", s.vbusMv, s.sysMv, buttonPresses);
  return buf;
}

const String &pmuBootReport() { return bootReport; }

bool pmuBegin() {
  present = pmu.begin(Wire, AXP2101_SLAVE_ADDRESS, IIC_SDA, IIC_SCL);
  if (!present) {
    Serial.println("[PMU] AXP2101 not found");
    return false;
  }

  // Snapshot before we change anything (measurements must be on to read voltages)
  pmu.enableBattDetection();
  pmu.enableVbusVoltageMeasure();
  pmu.enableBattVoltageMeasure();
  pmu.enableSystemVoltageMeasure();
  delay(50);
  // ADC channel enable register 0x30, bit 1 = TS pin measurement
  Wire.beginTransmission(AXP2101_SLAVE_ADDRESS);
  Wire.write(0x30);
  Wire.endTransmission(false);
  Wire.requestFrom((uint8_t)AXP2101_SLAVE_ADDRESS, (uint8_t)1);
  int adcEn = Wire.available() ? Wire.read() : -1;
  bootReport = pmuReport(pmuStatus()) + String(" ts_measure=") +
               (adcEn < 0 ? "?" : ((adcEn & 0x02) ? "on" : "off"));

  // Board has no battery temperature sensor: TS measurement must be off,
  // otherwise the charger sees a "temperature fault" and won't charge normally.
  pmu.disableTSPinMeasure();
  pmu.enableTemperatureMeasure();

  // Charging profile for a single-cell 3.7 V LiPo (4.2 V full)
  pmu.setPrechargeCurr(XPOWERS_AXP2101_PRECHARGE_50MA);
  pmu.setChargerConstantCurr(BATTERY_CHARGE_MA >= 400 ? XPOWERS_AXP2101_CHG_CUR_400MA
                             : BATTERY_CHARGE_MA >= 300 ? XPOWERS_AXP2101_CHG_CUR_300MA
                             : BATTERY_CHARGE_MA >= 200 ? XPOWERS_AXP2101_CHG_CUR_200MA
                                                        : XPOWERS_AXP2101_CHG_CUR_100MA);
  pmu.setChargerTerminationCurr(XPOWERS_AXP2101_CHG_ITERM_25MA);
  pmu.setChargeTargetVoltage(XPOWERS_AXP2101_CHG_VOL_4V2);

  // PWR button: only the short-press interrupt (a long press still powers off in hardware)
  pmu.disableIRQ(XPOWERS_AXP2101_ALL_IRQ);
  pmu.clearIrqStatus();
  pmu.enableIRQ(XPOWERS_AXP2101_PKEY_SHORT_IRQ);
  expanderIdle = readExpanderPwr();

  Serial.println("[PMU] boot: " + bootReport);
  Serial.println("[PMU] now:  " + pmuReport(pmuStatus()));
  return true;
}
