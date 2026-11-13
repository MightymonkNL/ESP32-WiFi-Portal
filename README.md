# ESP32-Portal

A WiFi access point with a captive landing page and an optional mini-survey, for the
**Waveshare ESP32-S3-Touch-AMOLED-1.8** (16 MB flash, 8 MB PSRAM, 368×448 AMOLED touch display).

When someone joins the network, their phone or laptop opens the landing page automatically
(if not, they can browse to `http://192.168.4.1`). You manage everything from your laptop over
USB-C with the **ESP32 Toolbox** app: network name, landing page (text, image, colours), survey and results.

The Toolbox handles more than this portal: every project is a **module**, and the Toolbox can install
another project on the board (and back) without losing settings. See [section 4](#4-esp32-toolbox).

**Features**

- Captive portal: every web request is redirected to your landing page; no internet needed
- Live status on the AMOLED display: online/offline, network name, connected devices, votes, survey result
- Tap the network card on the display to turn the WiFi network on or off
- TX button on the network card: WiFi transmit power 25% / 50% / 75% / 100% (default 50%,
  enough for a classroom; use 75–100% for a large hall)
- Battery saving: CPU at 80 MHz; on battery the display dims after 10 s and switches off after
  another 30 s (a touch or the PWR button wakes it)
- Several portals (up to 8), e.g. "Open day" and "Parents' evening", each with its own page, survey
  and optional network name; choose the active one with the PORTAL button on the display
- Page editor with live preview: headings, formatted text, images, photo galleries, highlights,
  dividers and spacing; colour themes, fonts and layout. Large photos are resized automatically
- Mini-survey: star rating (1–5), yes/no, multiple choice or an open question (up to 240 characters),
  one answer per device
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

## 4. ESP32 Toolbox

Start it with **`ESP32 Toolbox.exe`**, or from source with `admin\ESP32 Toolbox.bat`
(needs Python 3 + pyserial). Close the PlatformIO serial monitor first: only one program can use the COM port.

**On another PC:** `build_exe.bat` also makes `dist/ESP32-Toolbox-<date>.zip`: the exe with the
projects, their firmware and esptool, without your personal data. Unzip it anywhere and start
`ESP32 Toolbox.exe`; the WiFi Portal downloads its portals and settings from the board. Projects
added with **+ Add project** are added again on that PC.

The Toolbox starts with the **projects**: one card per project, and at the top which project runs
on the connected ESP32 (marked **On the ESP32** on its card). Click a card to open the project;
**← Projects** goes back. The projects are loose folders in `admin/modules` next to the exe, so a
new project needs no new exe.

| Module | Project |
|---|---|
| **WiFi Portal** (`modules/portal`) | This portal (firmware in the repository root) |
| **Empty project** (`modules/template`) | Placeholder to start a new project from (`projects/template`) |

### Installing a project (Firmware tab)

Every project has a **Firmware** tab: the version on the ESP32 and in the Toolbox, the install or
update button and the backups of that project for this board. When you open a project that does not
run on the ESP32, an orange bar says what runs there instead, with an **Install** button; you can
still prepare the project (for example portal pages), but actions that need the board are off until
it is installed. Installing:

1. backs up the project on the board to `admin/backups/<board>/<project>-<time>.zip`
   (the last 5 per project and board are kept);
2. flashes the new project with esptool (about a minute);
3. puts the last settings of that project back, when there is a backup of it for this board.

Only the program itself is written: the WiFi settings and files of the other project stay on the
board as long as both projects use the same partition table, so switching back is quick and complete.
When another project did erase them, step 3 restores them from the backup. **Update to x.y** appears
when the project has newer firmware than the board; **Restore selected** puts an older backup back.

Before the first install, build the firmware of all modules: `build_exe.bat` (or
`python admin\build_modules.py`) puts it in `admin/modules/<id>/firmware` and copies esptool to
`admin/tools`. To add your own project, see [admin/modules/README.md](admin/modules/README.md).

The backups contain WiFi passwords and survey answers: they stay on this PC (`admin/backups` is
ignored by git).

### External projects (Add project)

The **+ Add project** card adds an existing ESP32 project that has a web installer (ESP Web Tools,
used by many projects such as ESPHome, WLED or [Pocket Tank](https://pocketank.com/)). Paste a link:
the project's website, install page, GitHub repository or `manifest.json`. The Toolbox finds the
installer, shows name, version, board, source and license, and downloads the ESP32-S3 firmware to
`admin/external/<project>` (ignored by git: it is someone else's software, under its own license).

External firmware cannot talk to the Toolbox, so:

- the Toolbox remembers which project it installed on which board (recognised by the MAC address the
  ESP32-S3 reports over USB) and shows it as running there;
- before another project is installed, its settings partitions (NVS and other small data partitions,
  read from its partition table) are saved with esptool; installing it again writes them back, so for
  example your Pocket Tank keeps its fish;
- its project screen has the Firmware tab and an **About** tab with links, license,
  **Check for update** and **Remove project**;
- **updates:** at start-up (at most every 6 hours per project) the Toolbox checks the project's
  installer for a newer version. A newer version shows as **Update available** on the project card and
  in the Firmware tab: **Download** fetches it, then **Update to …** installs it (its saved settings stay).

Projects with another partition table (like Pocket Tank with its 7.5 MB model) overwrite the portal's
files; installing the WiFi Portal again restores them and its WiFi settings from the automatic backup.
Only add projects you trust: their firmware runs on your board. No code from an external project
runs on your PC.

### WiFi Portal module

| Tab | What you can do |
|---|---|
| **WiFi** | Turn the network on/off, change network name and password, factory reset |
| **Portal** | Open the page editor; preview in your browser; upload to the board |
| **Survey** | Show a question on the portal: star rating, yes/no, multiple choice or open question; thank-you page |
| **Results** | Read the answers from the board, summary, export to CSV, clear the log |
| **Backup** | Save everything (portals, images, WiFi settings, active portal, answers) to one file and restore it |

### Portals

At the top of the app you choose the portal you are editing, and add (**New**, **Duplicate**) or
remove portals. Each portal has its own page, survey and thank-you page. In the Portal tab you set
its name (shown on the display) and, optionally, its own network name; leave that empty to use the
name from the WiFi tab.

**Upload all portals to ESP32** puts every portal on the board (images used by several portals are
stored once). Choose the active portal on the display: tap the **PORTAL** button at the bottom right
and tap a portal in the list (the list closes after 15 seconds). Or click **Make active on ESP32** in
the app. When a portal has its own network name the WiFi network restarts with that name, so
connected phones have to reconnect.

In the Results tab you can show the answers of one portal or all; the CSV export has a `portal`
column. Needs firmware 2.4 or newer.

### Backup

**Create backup** saves one `.zip` file with all portals and images, the WiFi settings (name,
password, network on/off), the active portal and all survey answers. Use it before you install
another program on the ESP32: that usually erases the board's storage.

To get everything back: install the WiFi Portal again (**Install project…** does this automatically),
or flash it yourself (section 3), and click **Restore backup**. The portals go back on this PC and on the board, the board restarts with the
WiFi settings from the backup, and the answers from the backup are added to any answers already on
the board. Devices that answered before can answer once more after a reinstall (the board's secret
for recognising devices is new).

The backup contains the WiFi password and survey answers: keep it in a safe place. WiFi settings
need firmware 2.5 or newer.

### Page editor

**Open page editor** (Portal tab) opens the editor in its own window. Build the page from blocks:

| Block | |
|---|---|
| Heading | large, medium or small; left or centred |
| Text | bold, italic, underline, lists, links |
| Image | caption, description for screen readers, full/75%/half width |
| Gallery | up to 12 photos in 2 or 3 columns |
| Highlight | coloured box for important information |
| Divider / Space | structure the page |

Drag blocks by their handle (⋮⋮) or use the arrows to reorder them. The eye icon hides a block
from the portal without deleting it (click it again to show the block); hidden blocks stay in
the editor, dimmed and marked *Hidden*. Their images are still uploaded, so showing a block
again only needs a new upload of the pages. The **Style** tab has
colour themes, colours, font, layout (card or full page) and width. The preview on the right
updates while you type (phone or desktop view). Changes are saved automatically; click
**Upload to ESP32** in the editor or the app to put the page on the board.

Photos are resized to at most 1280 px before they are stored, so you can use photos straight
from a phone. Gallery photos are made even smaller: at most 300×300 px, stored as PNG or JPEG,
whichever is smaller (PNG when the image has transparency). **Animated GIFs** stay animated: small ones are stored as they are, larger ones are
scaled down (all frames and timing kept, transparency too) until they are about 700 kB at most, so
they fit on the board and load quickly on phones. The board has about 3 MB of space for pages and images; the editor shows how much
your images use. With firmware 2.2 or newer, uploads skip images that are already on the board.

**Content warning** (the editor's **Warning** tab, per portal): a screen in front of the page with an
optional round logo, a heading, text and two buttons. **Enter** shows the page (remembered while
that browser tab stays open); **Leave** shows a goodbye message. With firmware 2.8 or newer the board
then disconnects that phone and refuses it for 30 seconds, so it does not rejoin and open the portal
again. Older firmware cannot disconnect it, so the page tells the visitor to turn off WiFi. The
preview shows the warning while the Warning tab is open.

When the app starts it downloads the page, survey settings and images from the board (stored there
on every upload, firmware 2.3 or newer). On a new PC you only need the exe and the board. If this PC has
changes that were not uploaded yet, the app asks which version to keep and saves a backup of the other.

Survey answers, voter keys and the app settings on the board can only be read over USB, not over WiFi.

Your editor content is stored locally in `admin/data/portal/` (ignored by git). Content of the
old Portal Admin app (`admin/portal_content.json`, `admin/portal_assets/`) is moved there on the first start.

To build the exe yourself, run `build_exe.bat`. It needs Python 3; when PlatformIO is not installed it is
installed with pip, and the first build downloads the ESP32 toolchain (about 1 GB). Only to *use* the
Toolbox on another PC you do not need to build anything: use the package (`dist/ESP32-Toolbox-<date>.zip`).

## 5. Survey and privacy

Each answer is stored as one line in `/responses.csv` on the board:
`survey_id, answer_index, time, device, question, answer`.

- *time* is the visitor's phone clock; *device* is a coarse type such as `iPhone (iOS 18)` or `Android 15 (Samsung)`.
- No IP addresses, MAC addresses or names are written to the log.
- **Leave** (content warning): the board keeps the MAC addresses of phones that chose Leave in RAM
  only, for 30 seconds, to keep them off the network. They are never logged or stored, and a restart clears them.
- **One answer per device per question:** the board keeps a salted SHA-256 hash of the device's
  MAC address in `/voters.txt` (the random salt never leaves the board). This file is never exported;
  **Clear log** deletes it together with the answers. A new question starts with an empty list.
  Someone who forgets the network and reconnects with a new random MAC address counts as a new device.
- The survey page says "Your answer is stored anonymously". Tell your visitors what you use the answers for,
  and follow the privacy rules (e.g. GDPR) that apply to you.

## 6. Battery (optional)

The board can run on a single-cell 3.7 V LiPo on the **MX1.25** battery connector; it charges
over USB-C via the on-board AXP2101. The display shows the battery level (with a bolt while charging),
and the app shows it on the WiFi tab.

- **Check the polarity** before plugging in: small LiPo cells often come with red/black swapped
  compared to the board's **+ / −** marking. With reversed polarity the board sees 0 V.
- Charge current is 200 mA by default (fine for 200–400 mAh cells). For a bigger battery set
  `#define BATTERY_CHARGE_MA 400` in `config_local.h`.
- The USB command `PMU` reports battery voltage, percentage and charger state for troubleshooting.
- **Battery mode:** without USB power the display dims (`DIM_BRIGHTNESS`, default 15). A short press on
  the **PWR** button lights it up for `WAKE_SECONDS` (default 10). Holding PWR for about 6 seconds
  switches the board off (hardware); a short press switches it on again.

## 7. Other ways to change settings

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
| `src/main.cpp` | Access point, DNS redirect, web server, portal USB commands |
| `src/status_display.cpp` | AMOLED status screen and touch |
| `src/survey.cpp` | Survey answers, log, one-vote-per-device |
| `src/power.cpp` | AXP2101 battery charging and measurements |
| `data/` | Default landing page |
| `lib/BoardLink/` | Shared USB protocol of all Toolbox projects (INFO, files) |
| `projects/template/` | Empty project to start a new one from |
| `admin/toolbox.pyw`, `admin/espcore.py` | ESP32 Toolbox app (Python/Tkinter) and its shared code |
| `admin/modules/` | One folder per project: `module.json`, `module.py`, `firmware/` |
| `admin/external.py` | Add project: finding and downloading web-installer firmware, esptool backups |
| `admin/build_modules.py` | Builds every module's firmware into its `firmware/` folder |
| `build_exe.bat` | Builds the firmware and `ESP32 Toolbox.exe` |

### USB protocol

Line-based over the serial port (115200 baud); replies start with `@`. Text is hex-encoded (UTF-8).
`INFO`, `REBOOT` and the file commands come from the shared BoardLink library (every Toolbox project
has them); the others are the portal's own.

| Command | Reply |
|---|---|
| `INFO` | `@INFO app=portal fw=<version> mac=<mac> ssid=<hex> open=<0/1> clients=<n> ip=<ip> lcd=<SH8601/CO5300/none> ap=<0/1> bat=<0-100/-1> chg=<0/1> portal=<id> essid=<hex> txp=<25-100> leave=1` (`leave=1`: firmware 2.8+) |
| `REBOOT` | `@OK` + restart |
| `FLIST` | `@FLIST <name>:<size> ...` |
| `PORTAL <id>` | Activate a portal |
| `WIFI` | `@WIFI ssid=<hex> pass=<hex> ap=<0/1>` (for the backup) |
| `PMU` | Battery and charger status (voltage, percentage, charging, USB) |
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
| "Firmware not built yet" in Install project | Run `build_exe.bat` or `python admin\build_modules.py` |
| Board not recognised / installing fails | Download mode (hold BOOT, press RESET, release BOOT), then **Install project…** again |
| "Incorrect padding" or "damaged data on the USB line" when portals are downloaded from the board | Update the Toolbox: it now reads damaged blocks again. Still failing: close other serial programs, use another USB cable or port |
| Toolbox says the firmware on the ESP32 is newer | The board was updated from another PC. Use the latest Toolbox package there too; installing the older firmware is a downgrade |
| Windows warns when starting the exe | The exe is not code-signed: *More info → Run anyway* |

## License

[MIT](LICENSE). Uses [GFX Library for Arduino](https://github.com/moononournation/Arduino_GFX) and
[U8g2](https://github.com/olikraus/u8g2) fonts; display pins and init order follow Waveshare's example code.
