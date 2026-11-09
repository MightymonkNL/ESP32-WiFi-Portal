// Template — empty project for the ESP32 Toolbox.
//
// It does nothing yet except talk to the Toolbox over USB (via BoardLink):
// the Toolbox recognises it, can back up its files and switch to another
// project. Start your own project from here.

#include <Arduino.h>
#include <LittleFS.h>
#include <BoardLink.h>

#define APP_ID "template"     // must match "id" in admin/modules/<project>/module.json
#define FW_VERSION "0.1"

// Example of a project command: "HELLO" -> "@OK hello from the template"
static bool handleCommand(const String &line) {
  if (line == "HELLO") {
    Serial.println("@OK hello from the template");
    return true;
  }
  return false;     // unknown: BoardLink replies "@ERR Unknown command"
}

// Extra key=value pairs for the INFO reply (shown in the Toolbox)
static String info() {
  return "uptime=" + String(millis() / 1000);
}

void setup() {
  BoardLink::begin(APP_ID, FW_VERSION);
  BoardLink::onCommand(handleCommand);
  BoardLink::onInfo(info);
  delay(500);
  Serial.println("\n[Template] start");
  if (!LittleFS.begin(true)) Serial.println("[FS] LittleFS mount failed");
}

void loop() {
  BoardLink::poll();
  delay(5);
}
