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
};

// Initialiseert het scherm. Geeft false terug als er geen scherm gevonden is.
bool displayBegin();

// "SH8601", "CO5300" or "none" (after displayBegin)
const char *displayDriver();

// Leest het touchscreen. Geeft true terug bij een tik op de netwerkkaart.
// Vaak aanroepen (bijv. elke loop).
bool displayPollTap();

// Laatste aanraking (voor diagnose via USB): x, y en aantal vingers.
void displayLastTouch(int &x, int &y, int &fingers, unsigned long &ageMs);

// Tekent alleen wat er veranderd is; veilig om vaak aan te roepen.
void displayUpdate(const PortalStatus &s);
