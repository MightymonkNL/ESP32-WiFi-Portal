# CLAUDE.md

Guidance for Claude Code in this repository. The user-facing documentation is in
[README.md](README.md) (setup, features, USB protocol, troubleshooting) and
[admin/modules/README.md](admin/modules/README.md) (how to add a project). Read those for details;
this file covers the architecture and the conventions that are not obvious from them.

## What this is

Two parts that work together:

1. **ESP32 firmware** (PlatformIO, Arduino framework, C++) for the **Waveshare
   ESP32-S3-Touch-AMOLED-1.8**: ESP32-S3R8, 16 MB flash, 8 MB octal PSRAM, 368×448 AMOLED touch
   display (two hardware revisions: SH8601 + FT3168 or CO5300 + CST820, detected at boot), AXP2101
   power management with LiPo battery. Other chips on the board that no project uses yet: IMU
   (QMI8658), RTC (PCF85063), audio codec with mic and speaker (ES8311).
   - The first project is the **WiFi Portal**: an access point with a captive landing page,
     an optional mini-survey and a live status display. Its firmware is in the repository root.
2. **ESP32 Toolbox**: a Windows desktop app (Python 3 + Tkinter, built into one exe with
   PyInstaller) that manages several ESP32 projects ("modules") on one board over USB-C:
   installing and switching firmware, backups, and each project's own UI (the portal's page editor,
   survey results and so on).

The user is a teacher or school staff member. The board is used in classrooms and at events
such as open days. Communicate with the user in **Dutch**. Code, comments, UI text, docs and commit
messages are in **English** (a few older comments in `src/main.cpp` are still Dutch).

## Repository layout

| Path | Contents |
|---|---|
| `platformio.ini` | Portal firmware build (env `waveshare-esp32s3`): PSRAM `qio_opi`, `default_16MB.csv`, LittleFS |
| `include/config.h` | `FW_VERSION`, `APP_ID`, defaults, pins. Overridable from `include/config_local.h` (gitignored) |
| `src/main.cpp` | Portal firmware: AP, DNS catch-all, web server, multiple portals, portal USB commands, main loop |
| `src/status_display.cpp` | AMOLED status screen, touch, on-screen buttons (network, TX power, PORTAL picker) |
| `src/survey.cpp` | Survey config, answer log `/responses.csv`, one vote per device (`/voters.txt`) |
| `src/power.cpp` | AXP2101: charging, battery level, PWR button |
| `data/index.html` | Default landing page (`pio run -t uploadfs`) |
| `lib/BoardLink/` | **Shared USB protocol library** used by every Toolbox project |
| `projects/template/` | Minimal firmware to start a new project (BoardLink only) |
| `admin/toolbox.pyw` | Toolbox main window: project cards, connect, install/update, add external project |
| `admin/espcore.py` | Shared Toolbox code: theme, `Board` (serial protocol), esptool flashing, folders, backups, `BaseModule` |
| `admin/external.py` | External projects: find an ESP Web Tools manifest from a link, download, settings-partition backups |
| `admin/modules/<id>/` | One folder per project: `module.json`, `module.py`, helpers, `firmware/` (built, gitignored) |
| `admin/modules/portal/` | WiFi Portal UI (`module.py`) and the browser-based page editor (`portal_editor.py`) |
| `admin/build_modules.py` | Builds every module's firmware into `admin/modules/<id>/firmware/`, copies esptool into `admin/tools/` |
| `admin/make_package.py` | Makes `dist/ESP32-Toolbox-<date>.zip` for another PC |
| `build_exe.bat` | Full build: firmware for all modules, then the exe, then the package |

## Architecture

### Firmware ↔ Toolbox: BoardLink

Every Toolbox project links `lib/BoardLink`. It is a line-based protocol over USB CDC serial at
115200 baud. Replies start with `@`, text arguments are hex-encoded UTF-8 and file blocks are base64
(max 512 bytes per line).

- `BoardLink::begin(APP_ID, FW_VERSION)` is called first in `setup()` and `BoardLink::poll()` in every `loop()`.
- Built-in commands: `INFO` (`@INFO app=<id> fw=<ver> mac=<mac> ...`), `FS`, `FLIST`, `FREAD`,
  `FBEGIN/FDATA/FEND/FABORT` (upload goes to a temp file, then replaces), `FDEL`, `REBOOT`.
- A project adds its own commands with `onCommand()`, extra INFO fields with `onInfo()`,
  and reacts to file changes with `onFileChanged()`.
- The Toolbox recognises which project runs on the board from `app=` and compares `fw=` with
  `module.json` `version` to offer an update.

### Toolbox modules

- `espcore.find_modules()` loads every `admin/modules/<id>/module.json` at start-up and imports
  `module.py` from the loose files. A new project therefore needs **no new exe**. Standard-library
  modules that modules use must be imported in `toolbox.pyw` so PyInstaller bundles them
  (see the import block at the top of that file).
- `module.py` defines `class Module(espcore.BaseModule)` (a Tk frame). The host (`Toolbox`) owns the
  COM port, the status line, the progress bar and the Firmware tab. Use `self.run_bg()` for anything
  that touches the board, so serial I/O never runs on the Tk thread.
- Hooks: `connected`, `backup_board` (staticmethod, runs without the UI), `restore_backup_file`,
  `save_state`, `restore_after_install`, `close`. See `admin/modules/README.md`.
- **Install flow** (`Toolbox.install`): back up the running project to
  `admin/backups/<mac>/<module>-<time>.zip` (keep 5), flash with esptool, then restore the last
  backup of the new project. All projects use the same partition table (`default_16MB.csv`), so the
  NVS and LittleFS data of the other project normally survive a switch.
- **External projects** (`external.py`, `ExternalModule`): third-party firmware from an ESP Web Tools
  manifest, downloaded to `admin/external/<id>`. These cannot speak BoardLink, so the Toolbox
  remembers per board MAC what it installed (`admin/data/toolbox.json`). Before switching it saves
  their small data partitions with esptool `read_flash`. It checks for updates at most every 6 h.
  No external code ever runs on the PC.
- Frozen exe: `espcore.BASE` is `<exe dir>/admin` when that has `modules/`, otherwise the exe dir
  itself (the portable package layout).

### WiFi Portal firmware

- SoftAP `192.168.4.1` with a DNS catch-all. Unknown hosts are redirected to the portal, which
  makes the captive-portal pop-up open on phones. `WebServer` serves files from LittleFS.
- NVS namespace `portal`: `ssid`, `pass`, `on` (AP on/off), `txp` (TX power level 0–3) and the active portal.
- Multiple portals (max 8): `/portals.cfg` (`id TAB name TAB ssid`). The files of each portal are
  `/p-<id>-index.html`, `/p-<id>-thanks.html` and `/p-<id>-survey.cfg`. Images are shared and use
  content-hash names in the root. `/portal.json` holds the editor content, so another PC can download it.
- Survey: POST `/answer`, GET `/voted`. One answer per device via a salted SHA-256 of the client MAC.
  The salt never leaves the board, and `/voters.txt` is never exported.
- Optional web settings page `/settings`, only enabled when `ADMIN_PASSWORD` is set in `config_local.h`.
- Power saving: CPU at 80 MHz. On battery the display dims and then switches off; touch or PWR wakes it.
  BOOT held for 5 s does a factory reset.

### Portal module (PC side)

- The content lives locally in `admin/data/portal/` (`portal_content.json`, `portal_assets/`, gitignored).
  The Tk app owns the content. It renders the HTML pages itself (`render_html`, `render_survey`,
  `render_thanks`) and uploads them with BoardLink file commands.
- `portal_editor.py` runs a small HTTP server on `127.0.0.1` (random port and token) with a single-page
  block editor, opened in Edge app mode or the browser. Images are resized to at most 1280 px.
  Animated GIFs are shrunk with Pillow to about 700 kB. The board has about 3 MB for pages and images.

## Build and run

```bash
python -m platformio run                    # build the portal firmware (repo root)
python -m platformio run -t upload          # flash it
python admin/build_modules.py [id ...]      # build module firmware into admin/modules/<id>/firmware
python admin/toolbox.pyw                    # run the Toolbox from source (needs pyserial, optionally Pillow)
build_exe.bat                               # everything: firmware, exe, dist zip
```

- `pio` is often not on PATH. `build_modules.py` falls back to `python -m platformio` and installs
  it with pip when it is missing.
- Only one program can open the COM port. Close the serial monitor before using the Toolbox, and the reverse.
- There is no automated test suite. Verify firmware by building it. Verify Toolbox changes by
  starting it, and test with the real board when the user has it connected.

## Conventions and rules

- **Version bump**: `FW_VERSION` in `include/config.h` (or a project's `main.cpp`) must equal
  `version` in that project's `module.json`. `APP_ID` must equal the module `id`. Bump both when the
  firmware changes behaviour, and note in the README which firmware version a feature needs.
- **New project**: follow the 4 steps in `admin/modules/README.md`: copy `projects/template` and
  `admin/modules/template`, set id and version, build, install. Keep the `default_16MB.csv` partition
  table and settings that must survive a reinstall in LittleFS (or extend `backup_board`).
- **Backward compatibility**: the Toolbox must keep working with older portal firmware.
  Existing code checks firmware versions (for example 2.2, 2.3, 2.4 and 2.5 features), so follow that pattern.
- **Toolbox look**: use the colours and helpers from `espcore` (`PRIMARY` #9C89D7, `SECONDARY`
  #FF9E71, `SUCCESS`, dark background, Segoe UI). They follow the Johan de Witt Scholengroep
  brand colours.
- **Privacy (GDPR)**: survey answers stay anonymous. Never log IP addresses, MAC addresses or names,
  and never export `/voters.txt` or the salt. Backups contain WiFi passwords and answers, so they
  stay local (`admin/backups`, gitignored).
- **Never commit**: `include/config_local.h`, `admin/data/`, `admin/backups/`, `admin/external/`,
  `admin/tools/`, `admin/modules/*/firmware/`, the built exe files and `dist/`
  (see `.gitignore`). The exe is distributed as a zip or GitHub Release.
- Keep README.md up to date with user-visible changes (features, USB commands, troubleshooting).
- Commit messages: short, English, `Area: what changed`, for example `Toolbox: ...` or `Build: ...`.
  Work on `main`, and push only when the user asks. Remote: `github.com/MightymonkNL/ESP32-WiFi-Portal`.
