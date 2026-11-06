#pragma once
// Copy this file to include/config_local.h and change the values you need.
// config_local.h is listed in .gitignore, so your settings never end up in git.

// Default network name (also used after a factory reset)
#define AP_SSID        "My-Portal"

// "" = open network, or 8 to 63 characters
#define AP_PASSWORD    ""

// Login for http://192.168.4.1/settings (the page is disabled while this is "changeme")
#define ADMIN_USER     "admin"
#define ADMIN_PASSWORD "choose-a-strong-password"

// Battery charge current in mA (100/200/300/400); keep it at or below the battery capacity
// #define BATTERY_CHARGE_MA 200

// Display brightness 0-255
// #define DISPLAY_BRIGHTNESS 170

// Battery mode: brightness without USB power, and how long the PWR button lights it up
// #define DIM_BRIGHTNESS 15
// #define WAKE_SECONDS 10
