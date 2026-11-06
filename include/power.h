#pragma once
#include <Arduino.h>

// AXP2101 power management (battery charging and measurements).
// Configured like Waveshare's example: TS pin measurement OFF (this board has
// no battery temperature sensor; leaving it on blocks normal charging).

#ifndef BATTERY_CHARGE_MA
#define BATTERY_CHARGE_MA 200   // safe for small LiPo cells (~0.5-1C for 200-400 mAh)
#endif

bool pmuBegin();
bool pmuPresent();

struct PowerStatus {
  bool battery;        // battery detected
  int percent;         // 0-100, -1 = unknown
  int battMv;
  bool charging;
  bool usb;            // USB/VBUS power present
  int vbusMv;
  int sysMv;
  const char *charger; // charger state text
};

PowerStatus pmuStatus();

// True once per short press of the PWR button (AXP2101 power key or IO expander EXIO4).
// Call often (e.g. every loop); it polls at most every 50 ms.
bool pmuButtonPressed();

// One-line diagnostic text (for the USB command PMU)
String pmuReport(const PowerStatus &s);
// The same report taken at boot, before this firmware changed any settings
const String &pmuBootReport();
