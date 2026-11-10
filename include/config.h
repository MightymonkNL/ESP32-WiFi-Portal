#pragma once

#define FW_VERSION "2.9"
// Project id for the ESP32 Toolbox app (must match admin/modules/portal/module.json)
#define APP_ID "portal"

// Personal settings go in include/config_local.h (not in git).
// Copy include/config_local.example.h to include/config_local.h and edit it.
#if __has_include("config_local.h")
#include "config_local.h"
#endif

// ---- WiFi access point defaults -------------------------------------------
// The SSID and password can also be changed with the admin app or the web page
// /settings (stored in flash). These values apply on first start and after a
// factory reset (hold the BOOT button for 5 seconds).
#ifndef AP_SSID
#define AP_SSID      "ESP32-Portal"
#endif

// "" = open network (usual for a landing page); otherwise 8 to 63 characters.
#ifndef AP_PASSWORD
#define AP_PASSWORD  ""
#endif

#ifndef AP_CHANNEL
#define AP_CHANNEL   6
#endif
#define AP_MAX_CONN  8      // the ESP32 supports about 10 clients at most

// IP address of the portal
#define AP_IP        192, 168, 4, 1
#define AP_NETMASK   255, 255, 255, 0

// ---- Web settings page (http://192.168.4.1/settings) -----------------------
// The page stays DISABLED while the password is "changeme". Set your own
// password in config_local.h to enable it. The admin app over USB always works.
#ifndef ADMIN_USER
#define ADMIN_USER     "admin"
#endif
#ifndef ADMIN_PASSWORD
#define ADMIN_PASSWORD "changeme"
#endif

// BOOT button (GPIO0 on Waveshare ESP32-S3 boards): hold 5 s = factory reset
#define RESET_BUTTON_PIN 0
#define RESET_HOLD_MS    5000

// ---- AMOLED display (Waveshare ESP32-S3-Touch-AMOLED-1.8) -----------------
#define LCD_SDIO0   4
#define LCD_SDIO1   5
#define LCD_SDIO2   6
#define LCD_SDIO3   7
#define LCD_SCLK    11
#define LCD_CS      12
#define LCD_WIDTH   368
#define LCD_HEIGHT  448
#define IIC_SDA     15
#define IIC_SCL     14
#ifndef DISPLAY_BRIGHTNESS
#define DISPLAY_BRIGHTNESS 170   // 0-255; lower = less AMOLED burn-in
#endif

// Battery mode: on battery (no USB) the display dims; a short press on the PWR
// button or touching the screen wakes it to full brightness for WAKE_SECONDS.
#ifndef DIM_BRIGHTNESS
#define DIM_BRIGHTNESS 15
#endif
#ifndef WAKE_SECONDS
#define WAKE_SECONDS 10
#endif
// ...and after SCREEN_OFF_SECONDS more without a touch the display switches off
// (a touch or the PWR button switches it on again).
#ifndef SCREEN_OFF_SECONDS
#define SCREEN_OFF_SECONDS 30
#endif

// ---- Power saving ----------------------------------------------------------
// 80 MHz is plenty for the portal and uses clearly less power than 240 MHz.
#ifndef CPU_MHZ
#define CPU_MHZ 80
#endif
// WiFi transmit power, chosen with the TX button on the display:
// 25% = 7 dBm, 50% = 11 dBm (a classroom), 75% = 15 dBm, 100% = 19.5 dBm (a hall)
#ifndef DEFAULT_TX_LEVEL
#define DEFAULT_TX_LEVEL 1   // 0 = 25%, 1 = 50%, 2 = 75%, 3 = 100%
#endif
