"""ESP32 Toolbox: manage several ESP32 projects (modules) on one board over USB-C.

Start screen: the projects (folders in admin/modules) and which one runs on the connected
ESP32. Open a project to work with it; its Firmware tab installs or updates it on the board
(the project that runs there is backed up first, and the last settings of the new one are
put back).

Start: 'ESP32 Toolbox.exe', 'admin/ESP32 Toolbox.bat' or: python toolbox.pyw
Requires Python 3 with pyserial (python -m pip install --user pyserial).
"""

import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

# Standard library modules used by modules (bundled into the exe; modules are loaded from loose files)
import base64, copy, csv, hashlib, html, io, json, re, secrets, socket, socketserver  # noqa: E401,F401
import uuid, webbrowser, zipfile, mimetypes, http.server, html.parser, urllib.parse  # noqa: E401,F401
from tkinter import filedialog, simpledialog  # noqa: F401
try:    # Pillow: the portal editor uses it to shrink large animated GIFs
    import PIL.Image, PIL.ImageSequence, PIL.GifImagePlugin  # noqa: E401,F401
except ImportError:
    pass

try:
    import serial  # noqa: F401
except ImportError:  # pragma: no cover
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror("ESP32 Toolbox",
                         "pyserial is missing.\n\nInstall with:\npython -m pip install --user pyserial")
    raise SystemExit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import espcore as core  # noqa: E402
import external  # noqa: E402
from espcore import (BG, CARD, SURFACE2, SURFACE3, TEXT, MUTED, PRIMARY, SECONDARY, SUCCESS,  # noqa: E402
                     IDLE, FONT)


class Toolbox(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ESP32 Toolbox")
        self.configure(bg=BG)
        self.busy = False
        self.status_text = ""
        self.status_kind = "idle"
        self.modules = core.find_modules()
        self.view = None              # the open project (module UI), None on the start screen
        self.board = None             # {"port", "app", "fw", "mac", "reply"} of the connected board
        self.connection = 0           # bumped on every (re)connect
        self.notified = None          # (view, connection) that got connected()
        self.fw = {}                  # widgets of the open project's Firmware tab
        core.apply_style(self)
        self._build()
        self.show_home()
        self.after(100, self.refresh_ports)
        self.after(1500, self.check_updates)          # external projects: newer version online?
        self.protocol("WM_DELETE_WINDOW", self._quit)

    # ================================================================ layout
    def _build(self):
        header = tk.Frame(self, bg=SURFACE2, padx=20, pady=14)
        header.pack(fill="x")
        self.back_btn = core.ghost(header, "←  Projects", self.show_home, small=True)
        titles = tk.Frame(header, bg=SURFACE2)
        titles.pack(side="left", fill="x", expand=True)
        tk.Label(titles, text="ESP32 TOOLBOX", bg=SURFACE2, fg=PRIMARY, font=(FONT, 8, "bold")).pack(anchor="w")
        self.title_var = tk.StringVar(value="Projects")
        tk.Label(titles, textvariable=self.title_var, bg=SURFACE2, fg=TEXT,
                 font=(FONT, 17, "bold")).pack(anchor="w")

        conn = tk.Frame(self, bg=CARD, padx=20, pady=14)
        conn.pack(fill="x", padx=16, pady=16)
        row = tk.Frame(conn, bg=CARD)
        row.pack(fill="x")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly")
        self.port_box.pack(side="left", fill="x", expand=True)
        self.port_box.bind("<<ComboboxSelected>>", lambda e: self.connect())
        self.search_btn = core.button(row, "Search", self.refresh_ports, small=True)
        self.search_btn.pack(side="left", padx=(8, 0))
        brow = tk.Frame(conn, bg=CARD)
        brow.pack(fill="x", pady=(10, 0))
        self.board_var = tk.StringVar(value="No ESP32 connected")
        tk.Label(brow, text="ON THE ESP32", bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(side="left")
        tk.Label(brow, textvariable=self.board_var, bg=CARD, fg=TEXT, font=(FONT, 10, "bold"),
                 anchor="w").pack(side="left", padx=(10, 0))
        srow = tk.Frame(conn, bg=CARD)
        srow.pack(fill="x", pady=(8, 0))
        self.dot = tk.Canvas(srow, width=12, height=12, bg=CARD, highlightthickness=0)
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=IDLE, outline="")
        self.dot.pack(side="left")
        self.status_var = tk.StringVar(value="Not connected")
        tk.Label(srow, textvariable=self.status_var, bg=CARD, fg=MUTED, font=(FONT, 9),
                 anchor="w", justify="left", wraplength=440).pack(side="left", padx=(8, 0), fill="x")
        self.progress = ttk.Progressbar(conn, mode="determinate", style="Horizontal.TProgressbar")

        self.area = tk.Frame(self, bg=BG)
        self.area.pack(fill="both", expand=True)

    def _fit(self, min_h=0):
        self.update_idletasks()
        w = max(self.winfo_reqwidth(), int(self.winfo_fpixels("5.2i")))
        h = max(self.winfo_reqheight(), min_h)
        self.minsize(w, h)
        self.geometry(f"{w}x{h}")

    def _clear_area(self):
        if self.view is not None:
            try:
                self.view.close()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            self.view = None
            self.fw = {}
        for w in self.area.winfo_children():
            w.destroy()

    def _module(self, module_id):
        return next((m for m in self.modules if m.id == module_id), None)

    def _app_name(self, app):
        m = self._module(app)
        return m.name if m else app

    # ================================================================ start screen
    def show_home(self):
        if self.busy:
            return
        self._clear_area()
        self.back_btn.pack_forget()
        self.title_var.set("Projects")
        self.title("ESP32 Toolbox")
        wrap = tk.Frame(self.area, bg=BG, padx=16)
        wrap.pack(fill="both", expand=True, pady=(0, 16))
        if not self.modules:
            core.label(wrap, f"No projects found in {core.MODULES_DIR}.", color=MUTED, bg=BG).pack(fill="x")
        grid = tk.Frame(wrap, bg=BG)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1, uniform="c")
        grid.columnconfigure(1, weight=1, uniform="c")
        cards = [self._card(grid, m) for m in self.modules] + [self._add_card(grid)]
        for i, card in enumerate(cards):
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=(0, 8) if i % 2 == 0 else (8, 0),
                      pady=(0, 16))
        hint = core.label(wrap, "Open a project to work with it. Installing or updating a project on the "
                                "ESP32 is done in the project's Firmware tab. Add project: an existing ESP32 "
                                "project from its website or GitHub (with a web installer).",
                          color=MUTED, size=9, bg=BG)
        hint.configure(wraplength=460, justify="left")
        hint.pack(fill="x")
        self._fit()

    def _card(self, parent, m):
        card = tk.Frame(parent, bg=SURFACE2, padx=16, pady=14, cursor="hand2")
        tk.Label(card, text=m.name, bg=SURFACE2, fg=TEXT, font=(FONT, 12, "bold"), anchor="w").pack(fill="x")
        desc = tk.Label(card, text=m.description, bg=SURFACE2, fg=MUTED, font=(FONT, 9), anchor="w",
                        justify="left", wraplength=200)
        desc.pack(fill="x", pady=(4, 8))
        tk.Label(card, text=f"Version {m.version}" + ("  ·  external" if m.external else ""),
                 bg=SURFACE2, fg=MUTED, font=(FONT, 9), anchor="w").pack(fill="x")
        text, color = self._badge(m)
        if text:
            tk.Label(card, text=text, bg=SURFACE2, fg=color, font=(FONT, 9, "bold"), anchor="w").pack(
                fill="x", pady=(6, 0))
        widgets = [card] + card.winfo_children()

        def hover(on):
            for w in widgets:
                w.configure(bg=SURFACE3 if on else SURFACE2)

        for w in widgets:
            w.bind("<Button-1>", lambda e, mod=m: self.open_project(mod))
            w.bind("<Enter>", lambda e: hover(True))
            w.bind("<Leave>", lambda e: hover(False))
        card.bind("<Configure>", lambda e: desc.configure(wraplength=max(120, e.width - 34)))
        return card

    def _add_card(self, parent):
        card = tk.Frame(parent, bg=BG, padx=16, pady=14, cursor="hand2", highlightthickness=1,
                        highlightbackground=SURFACE3)
        tk.Label(card, text="+  Add project", bg=BG, fg=PRIMARY, font=(FONT, 12, "bold"), anchor="w").pack(fill="x")
        desc = tk.Label(card, text="From a website, GitHub or an installer link, e.g. pocketank.com",
                        bg=BG, fg=MUTED, font=(FONT, 9), anchor="w", justify="left", wraplength=200)
        desc.pack(fill="x", pady=(4, 0))
        for w in (card, *card.winfo_children()):
            w.bind("<Button-1>", lambda e: self.add_project())
        card.bind("<Configure>", lambda e: desc.configure(wraplength=max(120, e.width - 34)))
        return card

    def _badge(self, m):
        b = self.board
        online = self.online_update(m)
        if online:
            on = "\u25cf On the ESP32  \u00b7  " if b and b.get("app") == m.id else ""
            return f"{on}Update available: {online}", SECONDARY
        if b and b.get("app") == m.id:
            if m.firmware_files() and b.get("fw") and b["fw"] != m.version:
                return f"● On the ESP32  ·  update {b['fw']} → {m.version}", SECONDARY
            return "● On the ESP32", SUCCESS
        if not m.firmware_files():
            return "Firmware not built yet", MUTED
        return "", MUTED

    # ================================================================ project screen
    def open_project(self, module):
        if self.busy:
            return
        self._clear_area()
        self.back_btn.pack(side="right")
        self.title_var.set(module.name)
        self.title(f"{module.name} — ESP32 Toolbox")
        self.banner = tk.Frame(self.area, bg=SURFACE2, padx=16, pady=10)
        self.banner_var = tk.StringVar()
        tk.Label(self.banner, textvariable=self.banner_var, bg=SURFACE2, fg=SECONDARY, font=(FONT, 9, "bold"),
                 anchor="w", justify="left", wraplength=340).pack(side="left", fill="x", expand=True)
        self.banner_btn = core.button(self.banner, "Install", lambda: self.install(module), small=True)
        self.banner_btn.pack(side="right")
        try:
            cls = module.load_class()
            self.view = cls(self.area, self, module)
        except Exception as exc:  # noqa: BLE001  (a broken module must not stop the Toolbox)
            traceback.print_exc()
            self.view = None
            core.label(self.area, f"Project “{module.name}” could not be loaded:\n{exc}", color=SECONDARY,
                       bg=BG).pack(fill="x", padx=16)
            self._fit()
            return
        self.view.pack(fill="both", expand=True)
        self._board_changed()
        self.view.show_firmware(not self._runs(module.id))
        self._fit()

    def _runs(self, module_id):
        return bool(self.board and self.board.get("app") == module_id)

    def _update_banner(self):
        if self.view is None:
            return
        m = self.view.module
        b = self.board
        text, button = "", None
        if not b:
            text = "No ESP32 connected. You can prepare this project; connect the board to use it."
        elif not b.get("app"):
            text = f"The ESP32 on {b['port']} runs no Toolbox project."
            button = f"Install {m.name}"
        elif b["app"] != m.id:
            text = f"On the ESP32 now: {self._app_name(b['app'])} {b.get('fw') or ''}".rstrip() + "."
            button = f"Install {m.name}"
        elif m.firmware_files() and b.get("fw") and b["fw"] != m.version:
            text = f"Firmware {b['fw']} on the ESP32; the Toolbox has {m.version}."
            button = f"Update to {m.version}"
        if button and not m.firmware_files():
            button = None
            text += " Its firmware is not built yet (build_modules.bat)."
        if text:
            self.banner_var.set(text)
            if button:
                self.banner_btn.configure(text=button)
                self.banner_btn.pack(side="right")
            else:
                self.banner_btn.pack_forget()
            self.banner.pack(fill="x", padx=16, pady=(0, 12), before=self.view)
        else:
            self.banner.pack_forget()

    def _apply_lock(self):
        """Board actions of the open project only work when that project runs on the board."""
        if self.view is None:
            return
        state = "normal" if self._runs(self.view.module.id) and not self.busy else "disabled"
        for b in self.view.action_buttons:
            try:
                b.configure(state=state)
            except tk.TclError:
                pass

    # ================================================================ Firmware tab
    def build_firmware_tab(self, parent, view):
        tab = tk.Frame(parent, bg=CARD, padx=20, pady=18)
        core.label(tab, "Firmware", bold=True, size=12).pack(fill="x")
        grid = tk.Frame(tab, bg=CARD)
        grid.pack(fill="x", pady=(8, 0))
        fw = {"board": tk.StringVar(), "toolbox": tk.StringVar(), "state": tk.StringVar()}
        for r, (title, key) in enumerate((("On this ESP32", "board"), ("In the Toolbox", "toolbox"))):
            core.label(grid, title, color=MUTED, size=9).grid(row=r, column=0, sticky="w", pady=2)
            tk.Label(grid, textvariable=fw[key], bg=CARD, fg=TEXT, font=(FONT, 10), anchor="w").grid(
                row=r, column=1, sticky="w", padx=(16, 0), pady=2)
        fw["state_lbl"] = tk.Label(tab, textvariable=fw["state"], bg=CARD, fg=MUTED, font=(FONT, 9, "bold"),
                                   anchor="w", justify="left", wraplength=440)
        fw["state_lbl"].pack(fill="x", pady=(8, 0))
        fw["install"] = core.button(tab, "Install", lambda: self.install(view.module))
        fw["install"].pack(fill="x", pady=(12, 0))
        note = core.label(tab, "Before installing, the project that runs on the ESP32 is backed up on this PC. "
                               "After installing, the last settings of this project are put back.",
                          color=MUTED, size=9)
        note.configure(wraplength=440, justify="left")
        note.pack(fill="x", pady=(6, 0))

        tk.Frame(tab, bg=SURFACE3, height=1).pack(fill="x", pady=(16, 12))
        core.label(tab, "Backups of this project for this ESP32", bold=True, size=10).pack(fill="x")
        table = tk.Frame(tab, bg=CARD)
        table.pack(fill="both", expand=True, pady=(6, 0))
        tree = ttk.Treeview(table, columns=("time", "size"), show="headings", height=4)
        tree.heading("time", text="Made", anchor="w")
        tree.heading("size", text="Size", anchor="w")
        tree.column("time", width=220, anchor="w")
        tree.column("size", width=80, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        fw["tree"] = tree
        fw["paths"] = {}
        brow = tk.Frame(tab, bg=CARD)
        brow.pack(fill="x", pady=(8, 0))
        fw["restore"] = core.ghost(brow, "Restore selected", lambda: self.restore_selected(view), small=True)
        fw["restore"].pack(side="left")
        core.ghost(brow, "Open folder", self.open_backup_folder, small=True).pack(side="left", padx=(8, 0))
        fw["empty"] = core.label(tab, "", color=MUTED, size=9)
        fw["empty"].pack(fill="x", pady=(6, 0))
        self.fw = fw
        return tab

    def _refresh_firmware(self):
        fw, view = self.fw, self.view
        if not fw or view is None:
            return
        m, b = view.module, self.board
        built = bool(m.firmware_files())
        fw["toolbox"].set(f"{m.name} {m.version}" if built else f"{m.name} {m.version} (firmware not built yet)")
        if not b:
            fw["board"].set("not connected")
        elif not b.get("app"):
            fw["board"].set("no Toolbox project")
        else:
            fw["board"].set(f"{self._app_name(b['app'])} {b.get('fw') or '?'}"
                            + (" (installed by the Toolbox)" if b.get("external") else ""))
        label, state, color, primary = "Install", "", MUTED, True
        command = lambda: self.install(m)  # noqa: E731
        online = self.online_update(m)
        if online:
            label, color = f"Download {online}", SECONDARY
            state = (f"Update available online: {online} (the Toolbox has {m.version}). Download it, then "
                     "install it on the ESP32.")
            command = lambda: self.download_update(m)  # noqa: E731
        elif not b:
            state = "Connect the ESP32 to install this project."
        elif not built:
            state = ("Download the firmware again: About tab, Check for update." if m.external else
                     "Build the firmware first: build_exe.bat or python admin\\build_modules.py.")
        elif b.get("app") != m.id:
            label = f"Install {m.name} on the ESP32"
            state = (f"Installing replaces {self._app_name(b['app'])} on the ESP32 (it is backed up first)."
                     if b.get("app") else "")
        elif b.get("fw") != m.version:
            label, state, color = f"Update to {m.version}", f"Update available: {b.get('fw')} → {m.version}.", \
                SECONDARY
        else:
            label, state, color, primary = f"Reinstall {m.version}", "Installed and up to date.", SUCCESS, False
        enabled = (online or (b and built)) and not self.busy
        fw["install"].configure(text=label, command=command, bg=PRIMARY if primary else SURFACE2,
                                fg=core.ON_PRIMARY if primary else PRIMARY,
                                activebackground=core.PRIMARY_LIGHT if primary else SURFACE3,
                                state="normal" if enabled else "disabled")
        fw["state"].set(state)
        fw["state_lbl"].configure(fg=color)

        tree = fw["tree"]
        tree.delete(*tree.get_children())
        fw["paths"] = {}
        mac = b.get("mac") if b else None
        paths = sorted(core.glob.glob(os.path.join(core.mac_folder(mac), f"{m.id}-*.zip")),
                       reverse=True) if mac else []
        for p in paths:
            stamp = os.path.basename(p)[len(m.id) + 1:-4]
            try:
                when = time.strftime("%d-%m-%Y %H:%M", time.strptime(stamp, "%Y%m%d-%H%M%S"))
            except ValueError:
                when = stamp
            item = tree.insert("", "end", values=(when, f"{os.path.getsize(p) // 1024 + 1} kB"))
            fw["paths"][item] = p
        if not mac:
            fw["empty"].configure(text="Connect the ESP32 to see its backups.")
        elif not paths:
            fw["empty"].configure(text="No backups yet. One is made automatically before another project is "
                                       "installed.")
        else:
            fw["empty"].configure(text="")
        fw["restore"].configure(state="normal" if paths and self._runs(m.id) and not self.busy else "disabled")

    def restore_selected(self, view):
        item = self.fw["tree"].focus() if self.fw else None
        path = self.fw["paths"].get(item) if item else None
        if not path:
            self.set_status("Select a backup first.", SECONDARY)
            return
        port = self.port_for(view.module.id)
        if not port:
            return self.no_port_message(view.module)
        view.restore_backup_file(port, path)

    def open_backup_folder(self):
        folder = core.mac_folder(self.board.get("mac")) if self.board and self.board.get("mac") \
            else core.BACKUP_DIR
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    # ================================================================ status / tasks (also used by modules)
    def set_status(self, text, color=IDLE):
        self.status_kind = {PRIMARY: "busy", SUCCESS: "ok", SECONDARY: "err"}.get(color, "idle")
        self.status_text = text           # plain copy, also read by modules' threads
        self.status_var.set(text)
        self.dot.itemconfigure(self.dot_id, fill=color)

    def _own_buttons(self):
        out = [self.search_btn, self.back_btn]
        if self.fw:
            out += [self.fw["install"], self.fw["restore"]]
        if getattr(self, "banner_btn", None) is not None and self.view is not None:
            out.append(self.banner_btn)
        return out

    def run_bg(self, work, done, busy_text, progress_total=None):
        """Runs work(report) in a thread; report(n) moves the progress bar; done(result) on the Tk thread."""
        if self.busy:
            return
        self.busy = True
        for b in self._own_buttons() + (list(self.view.action_buttons) if self.view else []):
            try:
                b.configure(state="disabled")
            except tk.TclError:
                pass
        self.port_box.configure(state="disabled")
        self.set_status(busy_text, PRIMARY)
        if progress_total:
            self.progress.configure(maximum=progress_total, value=0)
            self.progress.pack(fill="x", pady=(10, 0))
        q = queue.Queue()

        def worker():
            try:
                q.put(("done", work(lambda n: q.put(("progress", n))), None))
            except Exception as exc:  # noqa: BLE001
                q.put(("done", None, exc))

        def poll():
            try:
                while True:
                    item = q.get_nowait()
                    if item[0] == "progress":
                        if item[1] > float(self.progress.cget("maximum")):
                            self.progress.configure(maximum=item[1] * 1.2)
                        self.progress.configure(value=item[1])
                    else:
                        self._finish(done, item[1], item[2])
                        return
            except queue.Empty:
                self.after(50, poll)

        threading.Thread(target=worker, daemon=True).start()
        self.after(50, poll)

    def _finish(self, done, result, error):
        self.busy = False
        for b in self._own_buttons():
            try:
                b.configure(state="normal")
            except tk.TclError:
                pass
        self.port_box.configure(state="readonly")
        self.progress.pack_forget()
        self._apply_lock()
        self._refresh_firmware()
        if error:
            msg = str(error)
            if isinstance(error, core.serial.SerialException):
                msg = "Cannot open the port. Is the serial monitor or another program still using it?"
            self.set_status(msg, SECONDARY)
        else:
            done(result)

    def port(self):
        v = self.port_var.get()
        return v.split(" — ")[0] if v else None

    def port_for(self, module_id):
        """The port, only when the board there runs this project."""
        return self.board["port"] if self._runs(module_id) else None

    def no_port_message(self, module):
        b = self.board
        if not b:
            self.set_status("Connect the ESP32 first.", SECONDARY)
        else:
            self.set_status(f"{module.name} does not run on the ESP32. Install it first (Firmware tab).", SECONDARY)

    # ================================================================ connecting
    def refresh_ports(self):
        if self.busy:
            return
        ports = core.find_ports()
        labels = [label for _, label, _ in ports]
        self.port_box["values"] = labels
        esp = [label for _, label, is_esp in ports if is_esp]
        if esp:
            self.port_var.set(esp[0])
            self.connect()
        else:
            self.port_var.set(labels[0] if labels else "")
            self.board = None
            self._board_changed()
            self.set_status("No ESP32 found. Connect the board and click Search.", SECONDARY)

    def connect(self):
        port = self.port()
        if not port:
            return

        def work(_r):
            try:
                return core.send_command(port, "INFO")
            except TimeoutError:      # board without a Toolbox project: it can still get one
                return None

        def done(reply):
            info = {}
            if reply:
                try:
                    info = core.parse_kv(reply, "INFO")
                except ValueError:
                    info = {}
            self._set_board(port, reply, info)
            b = self.board
            if info:
                core.set_board_record(b["mac"], b["app"], b["fw"])
                self.set_status(f"Connected to {port}", SUCCESS)
            else:
                rec = core.board_record(b["mac"])
                m = self._module(rec["app"]) if rec else None
                if m is not None and m.external:
                    b.update(app=m.id, fw=rec.get("fw"), external=True)
                    self.set_status(f"Connected to {port}. {m.name} was installed on this ESP32 by the Toolbox.",
                                    SUCCESS)
                else:
                    self.set_status(f"{port}: no Toolbox project recognised on this ESP32. Open a project and "
                                    "install it.", SECONDARY)
            self._board_changed()

        self.run_bg(work, done, f"Connecting to {port}…")

    def _set_board(self, port, reply, info, mac=None):
        self.board = {"port": port, "app": core.app_of(info), "fw": info.get("fw"),
                      "mac": core.norm_mac(info.get("mac") or mac or core.port_mac(port)), "reply": reply}
        self.connection += 1

    def _board_changed(self):
        """Board line, project cards / banner, lock, Firmware tab; tells the open project it is connected."""
        b = self.board
        if not b:
            self.board_var.set("not connected")
        elif not b.get("app"):
            self.board_var.set(f"{b['port']}  ·  no Toolbox project")
        else:
            self.board_var.set(f"{self._app_name(b['app'])} {b.get('fw') or ''}  ·  {b['port']}"
                               + ("  ·  installed by the Toolbox" if b.get("external") else ""))
        if self.view is None:
            if not self.busy:
                self.show_home()           # badges
            return
        self._update_banner()
        self._apply_lock()
        self._refresh_firmware()
        if self._runs(self.view.module.id) and b.get("reply") and self.notified != (self.view, self.connection):
            self.notified = (self.view, self.connection)
            self.view.connected(b["port"], b["reply"])

    # ================================================================ installing
    def install(self, module):
        port = self.port()
        if not port or self.busy:
            if not port:
                self.set_status("Connect the ESP32 first.", SECONDARY)
            return
        b = self.board or {}
        old_app = b.get("app")
        same = old_app == module.id
        steps = []
        if old_app:
            steps.append(f"Backup of “{self._app_name(old_app)}” (settings and files) on this PC")
        steps.append(f"Flash “{module.name}” {module.version} (about a minute; do not unplug the board)")
        if not same:
            steps.append(f"Put the last settings of “{module.name}” back, if there is a backup for this ESP32")
        text = f"Install “{module.name}” on the ESP32 on {port}?\n\n" + \
            "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        if not messagebox.askyesno("Install project", text):
            return

        if self.view is not None:
            self.view.save_state()
        mac0 = b.get("mac") or core.port_mac(port)
        restore_from = core.latest_backup(mac0, module.id) if module.external and not same and mac0 else None
        backup = None
        if old_app:
            old = self._module(old_app)
            try:
                backup = old.load_class().backup_board if old else core.BaseModule.backup_board
            except Exception:  # noqa: BLE001  (broken module: back up all files)
                backup = core.BaseModule.backup_board
        total = core.flash_size(module)

        def work(report):
            tmp = None
            if backup:
                tmp = os.path.join(tempfile.gettempdir(), f"toolbox-backup-{old_app}.zip")
                backup(port, tmp)
            extra, labels = external.restore_parts(module, restore_from) if restore_from else ([], [])
            try:
                mac = core.flash(port, module, report, extra)
            except Exception:
                if tmp:     # keep the backup even when flashing fails
                    shutil.move(tmp, core.new_backup_path(b.get("mac"), old_app))
                raise
            mac = core.norm_mac(mac0 or mac)
            saved = None
            if tmp:
                saved = core.new_backup_path(mac, old_app)
                shutil.move(tmp, saved)
                core.prune_backups(mac, old_app)
            reply = None if module.external else core.wait_for_info(port)
            return mac, saved, reply, labels

        def done(result):
            mac, saved, reply, labels = result
            if module.external:
                self._installed_external(port, module, mac, saved, labels)
            else:
                self._installed(port, module, mac, saved, reply, same)

        self.run_bg(work, done, f"Installing {module.name}…", progress_total=total)

    def _installed_external(self, port, module, mac, saved, labels):
        self.board = {"port": port, "app": module.id, "fw": module.version, "mac": mac, "reply": None,
                      "external": True}
        self.connection += 1
        core.set_board_record(mac, module.id, module.version)
        note = f" Backup of the previous project: {os.path.relpath(saved, core.BASE)}." if saved else ""
        kept = f" Its saved settings ({', '.join(labels)}) were put back." if labels else ""
        self.set_status(f"{module.name} installed.{kept}{note}", SUCCESS)
        self._board_changed()

    def _installed(self, port, module, mac, saved, reply, same):
        try:
            info = core.parse_kv(reply, "INFO")
        except ValueError:
            info = {}
        self._set_board(port, reply, info, mac)
        core.set_board_record(self.board["mac"], self.board["app"], self.board["fw"])
        note = f" Backup of the previous project: {os.path.relpath(saved, core.BASE)}." if saved else ""
        if core.app_of(info) != module.id:
            self._board_changed()
            self.set_status(f"Flashed, but the ESP32 reports “{core.app_of(info)}” instead of “{module.id}”.",
                            SECONDARY)
            return
        backup = None if same else core.latest_backup(self.board["mac"], module.id)
        view = self.view if self.view is not None and self.view.module.id == module.id else None
        if view is not None and backup:
            self.notified = (view, self.connection)        # restore_after_install connects it
        self.set_status(f"{module.name} {info.get('fw', '')} installed.{note}", SUCCESS)
        self._board_changed()
        if view is not None:
            view.show_firmware(False)
        if view is not None and backup:
            view.restore_after_install(port, reply, backup)

    # ================================================================ updates of external projects
    UPDATE_EVERY = 6 * 3600          # seconds between automatic checks per project

    def online_update(self, m):
        """Newer version of an external project found online, or None."""
        if not m.external:
            return None
        u = core.load_settings().get("updates", {}).get(m.id) or {}
        latest = u.get("latest")
        return latest if latest and latest != m.version and u.get("for") == m.version else None

    def _save_update(self, module_id, version_for, latest):
        settings = core.load_settings()
        settings.setdefault("updates", {})[module_id] = {"checked": time.time(), "for": version_for,
                                                        "latest": latest}
        core.save_settings(settings)

    def check_updates(self, force=False):
        """Checks the external projects for a newer version in the background (no busy state)."""
        cache = core.load_settings().get("updates", {})
        todo = [m for m in self.modules if m.external and
                (force or time.time() - (cache.get(m.id) or {}).get("checked", 0) > self.UPDATE_EVERY)]
        if not todo:
            return
        q = queue.Queue()

        def worker():
            for m in todo:
                try:
                    found = external.find_manifest(m.source.get("manifest") or m.source.get("url"))
                    q.put((m.id, m.version, str(found["manifest"].get("version") or "")))
                except Exception:  # noqa: BLE001  (offline: try again next time)
                    q.put((m.id, m.version, None))
            q.put(None)

        def poll():
            changed = False
            try:
                while True:
                    item = q.get_nowait()
                    if item is None:
                        if changed:
                            self._updates_changed()
                        return
                    mid, version_for, latest = item
                    if latest:
                        self._save_update(mid, version_for, latest)
                        changed = changed or latest != version_for
            except queue.Empty:
                pass
            self.after(300, poll)

        threading.Thread(target=worker, daemon=True).start()
        self.after(300, poll)

    def _updates_changed(self):
        names = [m.name for m in self.modules if self.online_update(m)]
        if names:
            self.set_status(f"Update available for {', '.join(names)}.", SECONDARY)
        if self.view is None:
            if not self.busy:
                self.show_home()
        else:
            self._refresh_firmware()

    def check_update_now(self, m):
        """About tab: check this project now and say the result."""
        def work(_r):
            return str(external.find_manifest(m.source.get("manifest") or m.source.get("url"))["manifest"]
                       .get("version") or "")

        def done(latest):
            self._save_update(m.id, m.version, latest)
            if latest and latest != m.version:
                self.set_status(f"{m.name} {latest} is available. Download it in the Firmware tab.", SECONDARY)
                if self.view is not None:
                    self.view.show_firmware(True)
            else:
                self.set_status(f"{m.name} is up to date ({m.version}).", SUCCESS)
            self._refresh_firmware()

        self.run_bg(work, done, f"Checking {m.name} for an update\u2026")

    def download_update(self, m):
        def work(report):
            found = external.find_manifest(m.source.get("manifest") or m.source.get("url"))
            found["url"] = m.source.get("url") or found["url"]
            external.download(found, report, m.id)
            return str(found["manifest"].get("version") or "?")

        def done(version):
            self._save_update(m.id, version, version)
            self.reload_project(m.id, f"Downloaded {m.name} {version}. Install it on the ESP32 with the button "
                                      "below; its saved settings stay.")

        self.run_bg(work, done, f"Downloading {m.name}\u2026", progress_total=8 * 1024 * 1024)

    # ================================================================ external projects
    def reload_modules(self):
        self.modules = core.find_modules()

    def reload_project(self, module_id, message=None):
        self.reload_modules()
        m = self._module(module_id)
        if m:
            self.open_project(m)
            if self.view is not None:
                self.view.show_firmware(True)
        if message:
            self.set_status(message, SUCCESS)

    def remove_project(self, module):
        if self.busy:
            return
        self._clear_area()
        shutil.rmtree(module.folder, ignore_errors=True)
        self.reload_modules()
        self.show_home()
        self.set_status(f"{module.name} removed from the Toolbox.", SUCCESS)

    def add_project(self):
        if self.busy:
            return
        dlg = tk.Toplevel(self, bg=CARD, padx=20, pady=18)
        dlg.title("Add project")
        dlg.transient(self)
        dlg.resizable(False, False)
        core.label(dlg, "Add project", bold=True, size=12).pack(fill="x")
        intro = core.label(dlg, "Paste the link of an ESP32 project with a web installer: its website, install "
                                "page, GitHub repository or manifest.json.", color=MUTED, size=9)
        intro.configure(wraplength=420, justify="left")
        intro.pack(fill="x", pady=(2, 10))
        url_var = tk.StringVar()
        entry = ttk.Entry(dlg, textvariable=url_var, font=(FONT, 10), width=52)
        entry.pack(fill="x")
        entry.focus_set()
        result = tk.Label(dlg, text="", bg=CARD, fg=TEXT, font=(FONT, 9), anchor="w", justify="left",
                          wraplength=420)
        result.pack(fill="x", pady=(10, 0))
        warn = core.label(dlg, "", color=SECONDARY, size=9)
        warn.configure(wraplength=420, justify="left")
        warn.pack(fill="x", pady=(6, 0))
        row = tk.Frame(dlg, bg=CARD)
        row.pack(fill="x", pady=(14, 0))
        state = {"found": None}

        def find():
            url = url_var.get().strip()
            if not url or self.busy:
                return
            state["found"] = None
            add_btn.configure(state="disabled")
            result.configure(text="Searching…", fg=MUTED)
            warn.configure(text="")

            def done(found):
                self.set_status(f"Found {found['manifest'].get('name', 'the project')}.", SUCCESS)
                if not dlg.winfo_exists():
                    return
                try:
                    lines = external.describe(found)
                except ValueError as exc:
                    result.configure(text=str(exc), fg=SECONDARY)
                    self.set_status(str(exc), SECONDARY)
                    return
                state["found"] = found
                result.configure(text="\n".join(lines), fg=TEXT)
                board = str(found["manifest"].get("board_name") or found["manifest"].get("board") or "")
                if board and not re.search(r"1\.8|amoled", board, re.I):
                    warn.configure(text="This firmware is made for another board. It may not work on yours.")
                else:
                    warn.configure(text="The firmware runs on your ESP32: only add projects you trust.")
                add_btn.configure(state="normal")

            def failed_ok(_r):
                try:
                    return external.find_manifest(url)
                except Exception as exc:  # noqa: BLE001
                    return exc

            def failed(exc):
                self.set_status(str(exc), SECONDARY)
                if dlg.winfo_exists():
                    result.configure(text=str(exc), fg=SECONDARY)

            self.run_bg(failed_ok, lambda r: failed(r) if isinstance(r, Exception) else done(r),
                        "Looking for the project's installer…")

        def add():
            found = state["found"]
            if not found or self.busy:
                return
            m = found["manifest"]
            mid = external.free_id(m.get("name") or "project", self.modules, found["manifest_url"])
            dlg.destroy()
            self.run_bg(lambda r: external.download(found, r, mid),
                        lambda _f: self.reload_project(mid, f"{m.get('name')} added. Install it on the ESP32 "
                                                            "with the button below."),
                        f"Downloading {m.get('name')}…", progress_total=8 * 1024 * 1024)

        core.button(row, "Find", find, small=True).pack(side="left")
        add_btn = core.button(row, "Add project", add, small=True)
        add_btn.pack(side="left", padx=(8, 0))
        add_btn.configure(state="disabled")
        core.ghost(row, "Cancel", dlg.destroy, small=True).pack(side="right")
        entry.bind("<Return>", lambda e: find())
        dlg.grab_set()

    def _quit(self):
        if self.view is not None:
            try:
                self.view.close()
            except Exception:  # noqa: BLE001
                pass
        self.destroy()


if __name__ == "__main__":
    Toolbox().mainloop()
