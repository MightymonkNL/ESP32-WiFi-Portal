"""Shared code for the ESP32 Toolbox and its modules.

- theme (colours, ttk styles, button/label helpers)
- the USB connection to a board running BoardLink firmware (Board)
- flashing a module's firmware with esptool
- folders: modules, per-module data, backups per board
- BaseModule: what a module's UI class builds on (see modules/README.md)
"""

import base64
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from tkinter import ttk

import serial
from serial.tools import list_ports

# ============================================================================ folders

if getattr(sys, "frozen", False):
    # "ESP32 Toolbox.exe": next to an admin folder (this repository), or in a
    # folder that holds modules/ and tools/ itself.
    _exe_dir = os.path.dirname(sys.executable)
    BASE = os.path.join(_exe_dir, "admin") if os.path.isdir(os.path.join(_exe_dir, "admin", "modules")) \
        else _exe_dir
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
MODULES_DIR = os.path.join(BASE, "modules")
DATA_DIR = os.path.join(BASE, "data")          # per module: data/<id>/ (stays on this PC)
BACKUP_DIR = os.path.join(BASE, "backups")     # per board: backups/<mac>/<module>-<time>.zip
TOOLS_DIR = os.path.join(BASE, "tools")
SETTINGS_FILE = os.path.join(DATA_DIR, "toolbox.json")
KEEP_BACKUPS = 5                               # automatic backups kept per board and project


def data_dir(module_id):
    path = os.path.join(DATA_DIR, module_id)
    os.makedirs(path, exist_ok=True)
    return path


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


# ============================================================================ theme
# Material Design dark theme with the Johan de Witt purple as primary colour

BG = "#121212"
CARD = "#1E1E1E"
SURFACE2 = "#2C2C2C"
SURFACE3 = "#383838"
OUTLINE = "#3D3D3D"
TEXT = "#E1E1E1"
MUTED = "#9E9E9E"
PRIMARY = "#9C89D7"
PRIMARY_LIGHT = "#B6A8E5"
ON_PRIMARY = "#121212"
SECONDARY = "#FF9E71"
SUCCESS = "#5DBB6A"
IDLE = "#5A5A5A"
FONT = "Segoe UI"


def apply_style(root):
    st = ttk.Style(root)
    st.theme_use("clam")
    field = dict(fieldbackground=SURFACE2, background=SURFACE2, foreground=TEXT,
                 bordercolor=OUTLINE, lightcolor=OUTLINE, darkcolor=OUTLINE,
                 insertcolor=TEXT, arrowcolor=TEXT, selectbackground=PRIMARY,
                 selectforeground=ON_PRIMARY)
    st.configure("TEntry", padding=7, **field)
    st.map("TEntry", fieldbackground=[("disabled", CARD)], foreground=[("disabled", IDLE)],
           bordercolor=[("focus", PRIMARY)], lightcolor=[("focus", PRIMARY)])
    st.configure("TCombobox", padding=6, **field)
    st.map("TCombobox", fieldbackground=[("readonly", SURFACE2)], foreground=[("readonly", TEXT)],
           background=[("active", SURFACE3), ("readonly", SURFACE2)],
           selectbackground=[("readonly", SURFACE2)], selectforeground=[("readonly", TEXT)])
    root.option_add("*TCombobox*Listbox.background", SURFACE2)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", PRIMARY)
    root.option_add("*TCombobox*Listbox.selectForeground", ON_PRIMARY)
    st.configure("TCheckbutton", background=CARD, foreground=TEXT, font=(FONT, 10),
                 indicatorbackground=SURFACE2, indicatorforeground=PRIMARY, bordercolor=OUTLINE)
    st.map("TCheckbutton", background=[("active", CARD)], indicatorbackground=[("selected", SURFACE2)])
    st.configure("TRadiobutton", background=CARD, foreground=TEXT, font=(FONT, 10),
                 indicatorbackground=SURFACE2, indicatorforeground=PRIMARY, bordercolor=OUTLINE)
    st.map("TRadiobutton", background=[("active", CARD)], indicatorbackground=[("selected", SURFACE2)])
    st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(16, 0, 16, 0),
                 bordercolor=CARD, lightcolor=CARD, darkcolor=CARD)
    st.configure("TNotebook.Tab", padding=(18, 8), font=(FONT, 10, "bold"),
                 background=BG, foreground=MUTED, borderwidth=0, lightcolor=BG, bordercolor=BG)
    st.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", PRIMARY)],
           lightcolor=[("selected", CARD)])
    st.configure("Horizontal.TProgressbar", troughcolor=SURFACE2, background=PRIMARY,
                 bordercolor=SURFACE2, lightcolor=PRIMARY, darkcolor=PRIMARY)
    st.configure("Treeview", background=SURFACE2, fieldbackground=SURFACE2, foreground=TEXT,
                 bordercolor=OUTLINE, lightcolor=OUTLINE, darkcolor=OUTLINE, rowheight=24, font=(FONT, 9))
    st.map("Treeview", background=[("selected", PRIMARY)], foreground=[("selected", ON_PRIMARY)])
    st.configure("Treeview.Heading", background=CARD, foreground=MUTED, relief="flat",
                 bordercolor=OUTLINE, lightcolor=CARD, darkcolor=CARD, font=(FONT, 9, "bold"))
    st.map("Treeview.Heading", background=[("active", SURFACE3)])
    st.configure("Vertical.TScrollbar", background=SURFACE2, troughcolor=CARD, bordercolor=CARD,
                 lightcolor=SURFACE2, darkcolor=SURFACE2, arrowcolor=MUTED)


def button(parent, text, command, bg=PRIMARY, fg=ON_PRIMARY, active=None, small=False):
    if bg == PRIMARY and active is None:
        active = PRIMARY_LIGHT
    return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                     activebackground=active or bg, activeforeground=fg, relief="flat", bd=0,
                     padx=12 if small else 16, pady=6 if small else 10, cursor="hand2",
                     font=(FONT, 9 if small else 10, "bold"))


def ghost(parent, text, command, fg=PRIMARY, small=False):
    return button(parent, text, command, SURFACE2, fg=fg, active=SURFACE3, small=small)


def label(parent, text, bold=False, color=TEXT, size=10, bg=CARD):
    return tk.Label(parent, text=text, bg=bg, fg=color, anchor="w",
                    font=(FONT, size, "bold" if bold else "normal"))


# ============================================================================ serial

ESPRESSIF_VID = 0x303A
BAUD = 115200
CHUNK = 512                       # bytes per FDATA line


def find_ports():
    ports = []
    for p in list_ports.comports():
        is_esp = p.vid == ESPRESSIF_VID
        ports.append((p.device, f"{p.device} — {'ESP32-S3' if is_esp else p.description}", is_esp))
    ports.sort(key=lambda x: (not x[2], x[0]))
    return ports


def to_hex(text):
    return text.encode("utf-8").hex()


def from_hex(text):
    return bytes.fromhex(text).decode("utf-8", errors="replace")


def parse_kv(reply, word):
    """'WORD a=1 b=2' -> {"a": "1", "b": "2"}"""
    if not reply.startswith(word):
        raise ValueError(f"Unexpected response: {reply}")
    out = {}
    for part in reply.split()[1:]:
        key, _, value = part.partition("=")
        out[key] = value
    return out


def app_of(info):
    """Project id from an INFO reply; portal firmware before 2.7 did not send one."""
    return info.get("app") or ("portal" if "ssid" in info else None)


class Board:
    """One serial session with the board (use with 'with')."""

    def __init__(self, port):
        self.s = serial.Serial()
        self.s.port = port
        self.s.baudrate = BAUD
        self.s.timeout = 0.2
        self.s.dtr = False      # keep low: avoids a reset when opening
        self.s.rts = False
        self.buf = b""

    def __enter__(self):
        self.s.open()
        self.s.reset_input_buffer()
        return self

    def __exit__(self, *exc):
        try:
            self.s.close()
        except Exception:
            pass

    def cmd(self, line, timeout=3.0):
        self.s.write((line + "\n").encode("utf-8"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.buf += self.s.read(max(1, self.s.in_waiting))
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                text = raw.decode("utf-8", errors="replace").strip()
                if text.startswith("@"):
                    return text[1:]
        raise TimeoutError("No response from the board. Is a Toolbox project installed on it?")

    def ok(self, line, timeout=3.0):
        reply = self.cmd(line, timeout)
        if not reply.startswith("OK"):
            raise RuntimeError(reply[4:] if reply.startswith("ERR") else reply)

    def info(self):
        return parse_kv(self.cmd("INFO"), "INFO")

    def list_files(self):
        """{name: size} of the files on the board, or None for firmware without FLIST."""
        reply = self.cmd("FLIST")
        if not reply.startswith("FLIST"):
            return None
        files = {}
        for part in reply.split()[1:]:
            name, _, size = part.rpartition(":")
            if name:
                files[name.lstrip("/")] = int(size or 0)
        return files

    def get_file(self, path, progress=None):
        """Contents of a file on the board (b"" when it does not exist)."""
        data = b""
        while True:
            reply = self.cmd(f"FREAD {path} {len(data)}")
            if reply.startswith("EOF"):
                return data
            if not reply.startswith("DATA"):
                raise RuntimeError(reply[4:] if reply.startswith("ERR") else reply)
            chunk = base64.b64decode(reply[5:].strip())
            if not chunk:
                return data
            data += chunk
            if progress:
                progress(len(data))

    def put_file(self, path, data, progress=None):
        self.ok(f"FBEGIN {path} {len(data)}")
        try:
            for i in range(0, len(data), CHUNK):
                self.ok("FDATA " + base64.b64encode(data[i:i + CHUNK]).decode("ascii"))
                if progress:
                    progress(min(i + CHUNK, len(data)))
            self.ok("FEND", timeout=5)
        except Exception:
            try:
                self.cmd("FABORT")
            except Exception:
                pass
            raise


def send_command(port, command, timeout=3.0):
    with Board(port) as b:
        return b.cmd(command, timeout)


def wait_for_info(port, first_wait=2.5, timeout=25):
    """INFO from a board that is restarting (after flashing or new WiFi settings)."""
    time.sleep(first_wait)
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return send_command(port, "INFO", timeout=1.5)
        except Exception as exc:  # noqa: BLE001  (port gone while USB reconnects, no reply yet)
            last = exc
            time.sleep(1)
    raise TimeoutError(f"Board not responding after restart ({last}).")


# ============================================================================ modules

class ModuleInfo:
    """A folder in modules/ with a module.json (and module.py, firmware/)."""

    def __init__(self, folder):
        self.folder = folder
        with open(os.path.join(folder, "module.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.meta = meta
        self.id = meta["id"]
        self.name = meta.get("name", self.id)
        self.description = meta.get("description", "")
        self.version = str(meta.get("version", "?"))
        self.chip = meta.get("chip", "esp32s3")
        self.flash = meta.get("flash", {})          # {"0x0": "bootloader.bin", ...}

    def firmware_files(self):
        """[(offset, path)] or None when a file is missing (firmware not built yet)."""
        if not self.flash:
            return None
        out = []
        for offset, name in sorted(self.flash.items(), key=lambda kv: int(kv[0], 16)):
            path = os.path.join(self.folder, "firmware", name)
            if not os.path.exists(path):
                return None
            out.append((int(offset, 16), path))
        return out

    def load_class(self):
        """The Module class from module.py (loaded once)."""
        if getattr(self, "_cls", None):
            return self._cls
        if self.folder not in sys.path:
            sys.path.insert(0, self.folder)        # module.py may import files from its own folder
        spec = importlib.util.spec_from_file_location(f"toolbox_module_{self.id}",
                                                      os.path.join(self.folder, "module.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._cls = mod.Module
        return self._cls


def find_modules():
    mods = []
    for path in sorted(glob.glob(os.path.join(MODULES_DIR, "*", "module.json"))):
        try:
            mods.append(ModuleInfo(os.path.dirname(path)))
        except (OSError, ValueError, KeyError):
            continue
    mods.sort(key=lambda m: (m.id == "template", m.name.lower()))
    return mods


# ============================================================================ flashing

def find_esptool():
    """Command to run esptool, or None."""
    exe = os.path.join(TOOLS_DIR, "esptool.exe")
    if os.path.exists(exe):
        return [exe]
    pio = os.path.join(os.path.expanduser("~"), ".platformio", "packages", "tool-esptool", "esptool.exe")
    if os.path.exists(pio):
        return [pio]
    if not getattr(sys, "frozen", False) and importlib.util.find_spec("esptool"):
        python = sys.executable.replace("pythonw.exe", "python.exe")
        return [python, "-m", "esptool"]
    return None


def flash(port, module, report=None):
    """Writes the module's firmware; returns the board's MAC address (or None).

    Only the bootloader, partition table and app are written: the settings (NVS)
    and files (LittleFS) of other projects stay on the board.
    """
    files = module.firmware_files()
    if not files:
        raise FileNotFoundError(f"The firmware of “{module.name}” is missing in {module.folder}\\firmware. "
                                "Build it first (build_modules.bat).")
    tool = find_esptool()
    if not tool:
        raise FileNotFoundError(f"esptool.exe not found. Put it in {TOOLS_DIR} (build_modules.bat does this).")
    regions = [(off, os.path.getsize(p)) for off, p in files]
    args = tool + ["--chip", module.chip, "--port", port, "--baud", "921600",
                   "--before", "default-reset", "--after", "hard-reset", "write-flash", "-z"]
    for off, path in files:
        args += [hex(off), path]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
    out, mac, line = [], None, b""
    while True:
        ch = proc.stdout.read(1)
        if not ch:
            break
        if ch not in (b"\r", b"\n"):
            line += ch
            continue
        text = line.decode("utf-8", errors="replace").strip()
        line = b""
        if not text:
            continue
        out.append(text)
        m = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", text)
        if m:
            mac = m.group(1).lower()
        m = re.search(r"Writing at 0x([0-9a-fA-F]+)", text)
        if m and report:
            addr, done = int(m.group(1), 16), 0
            for off, size in regions:
                if addr >= off + size:
                    done += size
                elif addr >= off:
                    done += addr - off
            report(done)
    if proc.wait() != 0:
        tail = " ".join(t for t in out[-4:] if t)
        raise RuntimeError(f"Flashing failed: {tail or 'esptool stopped'}")
    if report:
        report(sum(size for _, size in regions))
    return mac


def flash_size(module):
    files = module.firmware_files() or []
    return sum(os.path.getsize(p) for _, p in files) or 1


# ============================================================================ backups

def mac_folder(mac):
    return os.path.join(BACKUP_DIR, (mac or "unknown").replace(":", "").lower())


def new_backup_path(mac, module_id):
    folder = mac_folder(mac)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{module_id}-{time.strftime('%Y%m%d-%H%M%S')}.zip")


def latest_backup(mac, module_id):
    found = sorted(glob.glob(os.path.join(mac_folder(mac), f"{module_id}-*.zip")))
    return found[-1] if found else None


def prune_backups(mac, module_id):
    found = sorted(glob.glob(os.path.join(mac_folder(mac), f"{module_id}-*.zip")))
    for old in found[:-KEEP_BACKUPS]:
        try:
            os.remove(old)
        except OSError:
            pass


def backup_board_files(board, path, info=None):
    """Generic backup: every file on the board, in a zip (files/<name>)."""
    files = board.list_files() or {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("toolbox-backup.json", json.dumps(
            {"format": 1, "kind": "files", "created": time.strftime("%Y-%m-%d %H:%M:%S"),
             "info": info or {}, "files": files}, indent=2))
        for name in sorted(files):
            z.writestr("files/" + name, board.get_file("/" + name))
    return len(files)


def restore_board_files(board, path, only_missing=True):
    """Puts the files from a generic backup back; by default only files that are missing."""
    with zipfile.ZipFile(path) as z:
        names = [n[6:] for n in z.namelist() if n.startswith("files/") and len(n) > 6]
        present = board.list_files() or {}
        count = 0
        for name in names:
            if only_missing and name in present:
                continue
            board.put_file("/" + name, z.read("files/" + name))
            count += 1
    return count


# ============================================================================ module base class

class BaseModule(tk.Frame):
    """Base for a module's UI: one project screen in the Toolbox.

    The Toolbox (host) owns the port, the status line, the progress bar and the Firmware tab
    (install / update / backups). A module puts its own tabs in a ttk.Notebook and calls
    self.add_firmware_tab(notebook) once.

    Override:
      connected(port, reply)           the board on `port` runs this project (reply: the INFO line;
                                       parse_kv(reply, "INFO") gives a dict)
      backup_board(port, path)         staticmethod: write a backup zip of this project from the
                                       board (runs in a thread, without the UI). Default: all files
      restore_after_install(port, reply, path)
                                       this project was installed again: put the settings from the
                                       last backup back (default: files that are missing)
      restore_backup_file(port, path)  Restore button in the Firmware tab (default: all files)
      save_state()                     save edits before the Toolbox backs up or closes
      close()                          the project screen is closed (stop servers, save)
    """

    def __init__(self, parent, host, module):
        super().__init__(parent, bg=BG)
        self.host = host
        self.module = module
        self.action_buttons = []      # need the board: disabled while busy or when another project runs
        self.notebook = None
        self.firmware_tab = None

    # host delegation
    @property
    def busy(self):
        return self.host.busy

    @property
    def status_text(self):
        return self.host.status_text

    def run_bg(self, work, done, busy_text, progress_total=None):
        self.host.run_bg(work, done, busy_text, progress_total)

    def set_status(self, text, color=IDLE):
        self.host.set_status(text, color)

    def port(self):
        """The port, only when the board there runs this project."""
        return self.host.port_for(self.module.id)

    def no_port(self):
        """Status message for an action that needs the board."""
        self.host.no_port_message(self.module)

    def add_firmware_tab(self, notebook):
        self.notebook = notebook
        self.firmware_tab = self.host.build_firmware_tab(notebook, self)
        notebook.add(self.firmware_tab, text="Firmware")

    def show_firmware(self, firmware):
        """Host: select the Firmware tab (project not on the board) or the first own tab."""
        if not self.notebook or not self.firmware_tab:
            return
        tabs = self.notebook.tabs()
        own = [t for t in tabs if t != str(self.firmware_tab)]
        self.notebook.select(str(self.firmware_tab) if firmware or not own else own[0])

    # to override
    def connected(self, port, reply):
        pass

    @staticmethod
    def backup_board(port, path):
        with Board(port) as b:
            backup_board_files(b, path, b.info())

    def save_state(self):
        pass

    def restore_after_install(self, port, reply, path):
        def work(_r):
            with Board(port) as b:
                return restore_board_files(b, path)

        def done(n):
            self.set_status(f"{self.module.name} installed" + (f"; {n} file(s) put back from the backup."
                                                                if n else "."), SUCCESS)
            self.connected(port, reply)

        self.run_bg(work, done, "Putting files back…")

    def restore_backup_file(self, port, path):
        from tkinter import messagebox
        if not messagebox.askyesno("Restore backup", f"Put all files from {os.path.basename(path)} back on "
                                                     "the ESP32? Files with the same name are replaced."):
            return

        def work(_r):
            with Board(port) as b:
                return restore_board_files(b, path, only_missing=False)

        self.run_bg(work, lambda n: self.set_status(f"Restored {n} file(s) from the backup.", SUCCESS),
                    "Restoring backup…")

    def close(self):
        pass
