"""Empty project (template) for the ESP32 Toolbox.

Copy this folder to start a module for a new project; see ../README.md.
The Firmware tab (install, update, backups) comes from the Toolbox; backup/restore of the
board's files comes from core.BaseModule (all LittleFS files).
"""

import tkinter as tk
from tkinter import ttk

import espcore as core
from espcore import CARD, MUTED, SUCCESS, Board


class Module(core.BaseModule):
    def __init__(self, parent, host, module):
        super().__init__(parent, host, module)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.add_firmware_tab(nb)
        nb.add(self._build_board(nb), text="Board")

    def _build_board(self, parent):
        tab = tk.Frame(parent, bg=CARD, padx=20, pady=18)
        core.label(tab, "Empty project", bold=True, size=12).pack(fill="x")
        intro = core.label(tab, "This is a placeholder: the firmware only talks to the Toolbox. Build your "
                                "own project from projects/template and this module folder.",
                           color=MUTED, size=9)
        intro.configure(wraplength=440, justify="left")
        intro.pack(fill="x", pady=(4, 14))
        self.info_var = tk.StringVar(value="Not connected to an ESP32 with this project.")
        tk.Label(tab, textvariable=self.info_var, bg=CARD, fg=core.TEXT, font=(core.FONT, 10),
                 anchor="w", justify="left").pack(fill="x")
        self.files_var = tk.StringVar(value="")
        tk.Label(tab, textvariable=self.files_var, bg=CARD, fg=MUTED, font=(core.FONT, 9),
                 anchor="w", justify="left", wraplength=440).pack(fill="x", pady=(8, 0))

        actions = tk.Frame(tab, bg=CARD)
        actions.pack(side="bottom", fill="x", pady=(16, 0))
        self.refresh_btn = core.button(actions, "Refresh", self.refresh)
        self.refresh_btn.pack(fill="x")
        self.hello_btn = core.ghost(actions, "Send HELLO (example command)", self.hello)
        self.hello_btn.pack(fill="x", pady=(8, 0))
        self.action_buttons = [self.refresh_btn, self.hello_btn]
        return tab

    def connected(self, port, reply):
        self._show(core.parse_kv(reply, "INFO"))
        self.refresh(quiet=True)

    def _show(self, info):
        self.info_var.set(f"Firmware {info.get('fw', '?')}  ·  MAC {info.get('mac', '?')}  ·  "
                          f"running {info.get('uptime', '?')} s")

    def refresh(self, quiet=False):
        port = self.port()
        if not port:
            return self.no_port()

        def work(_r):
            with Board(port) as b:
                return b.info(), b.list_files() or {}, b.cmd("FS")

        def done(result):
            info, files, fs = result
            self._show(info)
            vals = dict(p.split("=") for p in fs.split()[1:]) if fs.startswith("FS") else {}
            used = int(vals.get("used", 0)) // 1024
            total = int(vals.get("total", 0)) // 1024
            names = ", ".join(sorted(files)) or "none"
            self.files_var.set(f"Files on the board ({used} of {total} kB in use): {names}")
            self.set_status(f"Connected to {port}" if quiet else "Board info refreshed.", SUCCESS)

        self.run_bg(work, done, "Reading the board…")

    def hello(self):
        port = self.port()
        if not port:
            return self.no_port()
        self.run_bg(lambda _r: core.send_command(port, "HELLO"),
                    lambda reply: self.set_status(f"Board says: {reply}", SUCCESS), "Sending HELLO…")
