#pragma once
#include <Arduino.h>

struct PortalStatus {
  bool online;        // accesspoint actief
  String ssid;
  bool open;          // open netwerk (geen wachtwoord)
  int clients;
  String ip;
  unsigned long uptimeSec;
  bool survey;        // survey active
  String surveyType;  // stars | yesno | choice
  int votes;
  float average;      // stars
  String summary;     // e.g. "Yes 12  No 5"
  int battery;        // 0-100, -1 = no battery
  bool charging;
  const String *portalNames = nullptr;   // portals to choose from (0 = single portal)
  int portalCount = 0;
  int portalActive = 0;
  int portalRev = 0;                     // changes when the list or the active portal changes
  int txPercent = 100;                   // WiFi transmit power (25/50/75/100)
};

enum TapType { TAP_NONE, TAP_NETWORK, TAP_PORTAL, TAP_WAKE, TAP_TXPOWER };
struct Tap {
  TapType type;
  int index;          // TAP_PORTAL: chosen portal
  bool touched;       // a finger touched the screen (wakes a dimmed display)
};

// Initialiseert het scherm. Geeft false terug als er geen scherm gevonden is.
bool displayBegin();

// "SH8601", "CO5300" or "none" (after displayBegin)
const char *displayDriver();

// Leest het touchscreen. Vaak aanroepen (bijv. elke loop).
// TAP_NETWORK: tik op de netwerkkaart. De portal-knop opent een keuzelijst;
// een tik op een regel daarin geeft TAP_PORTAL met de gekozen index.
// Op een gedimd scherm maakt de eerste tik het alleen wakker (TAP_WAKE, geen actie).
Tap displayPollTap();

// Laatste aanraking (voor diagnose via USB): x, y en aantal vingers.
void displayLastTouch(int &x, int &y, int &fingers, unsigned long &ageMs);

// Sets the panel brightness (0-255); only sends a command when it changes.
void displaySetBrightness(uint8_t level);

// Switches the panel off (sleep) or on again; the touchscreen keeps working.
void displaySetPower(bool on);

// Tekent alleen wat er veranderd is; veilig om vaak aan te roepen.
void displayUpdate(const PortalStatus &s);
