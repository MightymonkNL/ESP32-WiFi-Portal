# ESP32-Portal

A WiFi access point with a captive landing page and an optional mini-survey, for the
**Waveshare ESP32-S3-Touch-AMOLED-1.8** (16 MB flash, 8 MB PSRAM, 368×448 AMOLED touch display).

When someone joins the network, their phone or laptop opens the landing page automatically
(if not, they can browse to `http://192.168.4.1`). You manage everything from your laptop over
USB-C with the **Portal Admin** app: network name, landing page (text, image, colours), survey and results.

**Features**

- Captive portal: every web request is redirected to your landing page; no internet needed
- Live status on the AMOLED display: online/offline, network name, connected devices, votes, survey result
- Tap the network card on the display to turn the WiFi network on or off
- Landing page editor with image upload (PNG/JPG) and colour picker
- Mini-survey: star rating (1–5), yes/no or multiple choice, one answer per device
- Results table in the app with CSV export (opens in Excel)
- Both hardware revisions supported and detected automatically (SH8601 + FT3168, or CO5300 + CST820)

---

## 1. Install the software (once)

You need **Python 3** and **PlatformIO** to build and flash the firmware.

Option A – VS Code: install [Visual Studio Code](https://code.visualstudio.com/), then the
**PlatformIO IDE** extension, and open this folder.

Option B – command line:

```bash
python -m pip install --user -U platformio pyserial
```

Use `python -m platformio` in the commands below (the `pio` command is often not on the PATH).

## 2. Your own settings (optional)

Copy `include/config_local.example.h` to `include/config_local.h` and edit it:
default network name, WiFi password and the login for the web settings page.
`config_local.h` is in `.gitignore`, so your settings never end up in git.

Without it the board starts as an **open network called `ESP32-Portal`**, and the web settings
page is **disabled** (there is no default password). The admin app over USB always works.

## 3. Flash the board

Connect the board with a USB-C **data** cable. Then, in this folder:

```bash
python -m platformio run -t upload
```

```bash
python -m platformio run -t uploadfs
```

The first command flashes the firmware, the second the default landing page from `data/`.
If there are several COM ports, add `--upload-port COM5` (your port).
If uploading fails, put the board in download mode: hold **BOOT**, press **RESET**, release **BOOT**.

> `uploadfs` overwrites everything on the board's file system, including a page and survey
> uploaded from the app. Upload them again from the app afterwards.

## 4. Portal Admin app

Start it with **`Portal Admin.exe`** (see *Releases*), or from source with `admin\Portal Admin.bat`
(needs Python 3 + pyserial). Close the PlatformIO serial monitor first: only one program can use the COM port.

| Tab | What you can do |
|---|---|
| **WiFi** | Turn the network on/off, change network name and password, factory reset |
| **Portal** | Heading, text, footer, image, colours; preview in your browser; upload to the board |
| **Survey** | Show a question on the portal: star rating, yes/no or multiple choice; thank-you page |
| **Results** | Read the answers from the board, summary, export to CSV, clear the log |

Your editor content is stored locally in `admin/portal_content.json` and `admin/portal_assets/`
(both ignored by git).

To build the exe yourself, run `build_exe.bat`.

## 5. Survey and privacy

Each answer is stored as one line in `/responses.csv` on the board:
`survey_id, answer_index, time, device, question, answer`.

- *time* is the visitor's phone clock; *device* is a coarse type such as `iPhone (iOS 18)` or `Android 15 (Samsung)`.
- No IP addresses, MAC addresses or names are written to the log.
- **One answer per device per question:** the board keeps a salted SHA-256 hash of the device's
  MAC address in `/voters.txt` (the random salt never leaves the board). This file is never exported;
  **Clear log** deletes it together with the answers. A new question starts with an empty list.
  Someone who forgets the network and reconnects with a new random MAC address counts as a new device.
- The survey page says "Your answer is stored anonymously". Tell your visitors what you use the answers for,
  and follow the privacy rules (e.g. GDPR) that apply to you.

## 6. Other ways to change settings

- **Touch display:** tap the network card to switch the network on or off.
- **Web page:** `http://192.168.4.1/settings` (only when you set `ADMIN_PASSWORD` in `config_local.h`;
  the login is sent over plain HTTP, so use a password you don't use anywhere else).
- **Factory reset:** hold the **BOOT** button for 5 seconds. Network name and password go back to the defaults
  and the network is switched on.

---

## Project structure

| Path | Contents |
|---|---|
| `platformio.ini` | Board and build settings |
| `include/config.h` | Defaults: network, admin login, display pins |
| `include/config_local.example.h` | Template for your own settings |
| `src/main.cpp` | Access point, DNS redirect, web server, USB protocol |
| `src/status_display.cpp` | AMOLED status screen and touch |
| `src/survey.cpp` | Survey answers, log, one-vote-per-device |
| `data/` | Default landing page |
| `admin/` | Portal Admin app (Python/Tkinter) |
| `build_exe.bat` | Builds `Portal Admin.exe` with PyInstaller |

### USB protocol

Line-based over the serial port (115200 baud); replies start with `@`. Text is hex-encoded (UTF-8).

| Command | Reply |
|---|---|
| `INFO` | `@INFO ssid=<hex> open=<0/1> clients=<n> ip=<ip> fw=<version> lcd=<SH8601/CO5300/none> ap=<0/1>` |
| `SET <ssid-hex> <password>` | `@OK` + restart, or `@ERR <message>`. Password: `-` = open, `=` = unchanged, otherwise hex |
| `AP 1` / `AP 0` | Network on/off (kept after a restart) |
| `RESET` | `@OK` + factory reset and restart |
| `FS` | `@FS total=<bytes> used=<bytes>` |
| `FBEGIN <path> <size>` / `FDATA <base64>` / `FEND` / `FABORT` | Upload a file to the board |
| `FREAD <path> <offset>` | `@DATA <base64>` (max. 512 bytes) or `@EOF` |
| `FDEL <path>` | Delete a file |
| `LOGCLEAR` | Delete all survey answers and the voter list |
| `TOUCH` | Last touch position (diagnostics) |

## Troubleshooting

| Problem | Solution |
|---|---|
| Upload fails / no COM port | Other USB cable (data, not charge-only); download mode (BOOT + RESET); other USB port |
| Network visible but no page | Upload a page from the app, or run `uploadfs` |
| Sign-in window doesn't open | Browse to `http://192.168.4.1` |
| App says "Cannot open the port" | Close the PlatformIO serial monitor or other serial programs |
| Windows warns when starting the exe | The exe is not code-signed: *More info → Run anyway* |

## License

[MIT](LICENSE). Uses [GFX Library for Arduino](https://github.com/moononournation/Arduino_GFX) and
[U8g2](https://github.com/olikraus/u8g2) fonts; display pins and init order follow Waveshare's example code.
