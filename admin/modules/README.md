# Toolbox modules

Every folder here is one project the ESP32 Toolbox can manage. The Toolbox loads them at start-up,
so adding a project needs no new exe: copy the folder next to the exe (`admin/modules/<id>`).

```
modules/<id>/
  module.json      name, version, firmware files (required)
  module.py        the UI and backup/restore of this project (required)
  firmware/        built by build_modules.py (not in git)
  *.py             helper files the module imports (e.g. portal/portal_editor.py)
```

## New project in 4 steps

1. **Firmware:** copy `projects/template` to `projects/<id>`. In `src/main.cpp` set `APP_ID`
   to `<id>` and `FW_VERSION`. Keep `BoardLink::begin()` and `BoardLink::poll()`: they make the
   board recognisable for the Toolbox and give you file transfer for free.
   Keep the same partition table (`default_16MB.csv`), so switching projects keeps the other
   project's settings and files on the board.
2. **Module:** copy `admin/modules/template` to `admin/modules/<id>`. In `module.json` set
   `id` (the same as `APP_ID`), `name`, `description`, `version` (the same as `FW_VERSION`) and
   `project` (`projects/<id>`, relative to the repository root).
3. **Build:** `python admin\build_modules.py <id>` builds the firmware into `firmware/`.
4. **Install:** start the Toolbox, open your project and click **Install** (Firmware tab).

Then build your own UI in `module.py`.

## module.json

```json
{
  "id": "myproject",
  "name": "My project",
  "description": "One line shown in Install project.",
  "version": "1.0",
  "chip": "esp32s3",
  "project": "projects/myproject",
  "flash": {"0x0": "bootloader.bin", "0x8000": "partitions.bin",
            "0xe000": "boot_app0.bin", "0x10000": "firmware.bin"}
}
```

`flash` lists what esptool writes (offset → file in `firmware/`). The Toolbox offers **Update to
<version>** when the board reports another `fw` than `version`.

## module.py

Defines `class Module(espcore.BaseModule)`: the project screen, a Tk frame the Toolbox shows below
the connection card. Put your tabs in a `ttk.Notebook` and call `self.add_firmware_tab(notebook)`
once: the Toolbox fills that tab (install, update, backups). The Toolbox owns the port, the status
line and the progress bar.

| Use | |
|---|---|
| `self.port()` | the port, only when the board runs this project (else None) |
| `self.no_port()` | status message when `port()` is None ("install it first") |
| `self.run_bg(work, done, text, progress_total=None)` | runs `work(report)` in a thread, then `done(result)` on the Tk thread; errors go to the status line |
| `self.set_status(text, color)` | status line (`SUCCESS`, `SECONDARY` for errors, `PRIMARY` busy) |
| `self.action_buttons` | buttons that need the board: off while a task runs or when another project runs |
| `espcore.Board(port)` | `with Board(port) as b:` `b.cmd()`, `b.ok()`, `b.info()`, `b.list_files()`, `b.get_file()`, `b.put_file()` |
| `espcore.data_dir(id)` | folder on this PC for the module's own data (`admin/data/<id>`) |
| `espcore.button/ghost/label` and the colours | the Toolbox look |

| Override | Called |
|---|---|
| `connected(port, reply)` | the board on `port` runs this project; `reply` is the INFO line (`espcore.parse_kv(reply, "INFO")`) |
| `backup_board(port, path)` | **staticmethod**, before another project is installed: write a backup zip from the board (in a thread, without the UI). Default: every file on the board |
| `restore_backup_file(port, path)` | **Restore selected** in the Firmware tab. Default: all files back |
| `save_state()` | save edits (before installing, for example) |
| `restore_after_install(port, reply, path)` | this project was installed again and there is a backup; default: put back files that are missing |
| `close()` | the module is replaced or the Toolbox closes |

Keep settings that must survive a reinstall in LittleFS files (the default backup covers them) or
add them to your own `backup_board`, like the portal module does for its WiFi settings (NVS).
