#pragma once
// BoardLink: the shared USB protocol between an ESP32 project and the ESP32 Toolbox app.
//
// Every project that uses BoardLink is recognised by the Toolbox, and gets file
// transfer (for settings, pages, logs, backups) without writing any code for it.
//
// Lines from the PC end with '\n'; every reply line starts with '@'.
//   INFO                    -> @INFO app=<id> fw=<version> mac=<mac> [project-specific key=value ...]
//   FS                      -> @FS total=<bytes> used=<bytes>
//   FLIST                   -> @FLIST <name>:<size> ...      (files in the LittleFS root)
//   FREAD <path> <offset>   -> @DATA <base64, max 512 bytes> or @EOF
//   FBEGIN <path> <size>    -> @OK: start an upload (written to a temp file first)
//   FDATA <base64>          -> @OK: next block (max 512 bytes decoded)
//   FEND                    -> @OK: replace the file
//   FABORT                  -> @OK: cancel the upload
//   FDEL <path>             -> @OK: delete a file
//   REBOOT                  -> @OK, then restart
// Anything else goes to the project's command handler (or @ERR Unknown command).
//
// Usage:
//   void setup() {
//     BoardLink::begin("myproject", "1.0");    // first thing in setup(): starts Serial
//     LittleFS.begin(true);
//   }
//   void loop() { BoardLink::poll(); ... }

#include <Arduino.h>

namespace BoardLink {

// Return true when the line was handled (the handler prints the @ reply itself).
typedef bool (*CommandHandler)(const String &line);
// Extra "key=value key=value" for the INFO reply (no leading space needed).
typedef String (*InfoHandler)();
// Called after a file was replaced (FEND) or deleted (FDEL).
typedef void (*FileHandler)(const String &path);

void begin(const char *app, const char *fw, unsigned long baud = 115200);
void onCommand(CommandHandler handler);
void onInfo(InfoHandler handler);
void onFileChanged(FileHandler handler);

// Reads and handles commands from the PC; call often (every loop).
void poll();

// Helpers for text in commands (UTF-8 safe, no spaces)
String toHex(const String &s);
bool fromHex(const String &hex, String &out);     // false for invalid hex or NUL characters
bool validPath(const String &path);              // "/name.ext": letters, digits, . _ -

}  // namespace BoardLink
