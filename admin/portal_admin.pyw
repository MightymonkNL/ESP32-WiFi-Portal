"""ESP32-Portal admin: change WiFi settings and the landing page over USB-C.

Start: double-click 'Portal Admin.exe' or 'admin/Portal Admin.bat' (or: python portal_admin.pyw).
Requires Python 3 with pyserial (python -m pip install --user pyserial).
"""

import base64
import sys
import csv
import hashlib
import html
import io
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import colorchooser, filedialog, messagebox, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror("Portal Admin",
                         "pyserial is missing.\n\nInstall with:\npython -m pip install --user pyserial")
    raise SystemExit(1)

# Interface-kleuren — Material Design dark theme
# (achtergrond #121212, hogere elevatie = lichter oppervlak, gedesatureerde primaire kleur,
#  tekst 87% / 60% wit)
BG = "#121212"            # achtergrond (0dp)
CARD = "#1E1E1E"          # oppervlak 1dp
SURFACE2 = "#2C2C2C"      # oppervlak 8dp (knoppen, invoervelden, tabbladen)
SURFACE3 = "#383838"      # hover
OUTLINE = "#3D3D3D"
TEXT = "#E1E1E1"          # high emphasis (87%)
MUTED = "#9E9E9E"         # medium emphasis (60%)
PRIMARY = "#9C89D7"
PRIMARY_LIGHT = "#B6A8E5"
ON_PRIMARY = "#121212"
SECONDARY = "#FF9E71"
SUCCESS = "#5DBB6A"
IDLE = "#5A5A5A"
FONT = "Segoe UI"

ESPRESSIF_VID = 0x303A
BAUD = 115200
CHUNK = 512                       # bytes per FDATA-regel
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # LittleFS-partitie is ± 3,4 MB

if getattr(sys, "frozen", False):
    # Running as "Portal Admin.exe" (PyInstaller): keep the editor content in
    # the admin folder next to the exe, not in the temporary unpack folder.
    _exe_dir = os.path.dirname(sys.executable)
    HERE = os.path.join(_exe_dir, "admin") if os.path.isdir(os.path.join(_exe_dir, "admin")) else _exe_dir
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
CONTENT_FILE = os.path.join(HERE, "portal_content.json")
ASSETS_DIR = os.path.join(HERE, "portal_assets")

DEFAULT_CONTENT = {
    "title": "Welcome",
    "heading": "Welcome",
    "text": "You are connected to this local WiFi network.\n\nThis page is stored on the device itself; there is no internet connection.",
    "footer": "",
    "image": "",
    "bg": "#121212",
    "card": "#1E1E1E",
    "fg": "#E1E1E1",
    "accent": "#9C89D7",
    # survey
    "survey_on": False,
    "survey_type": "stars",
    "survey_question": "How many stars do you give this photo?",
    "survey_yes": "Yes",
    "survey_no": "No",
    "survey_choices": ["Option A", "Option B", "", ""],
    "thanks_heading": "Thank you!",
    "thanks_text": "Your answer has been saved.",
}

IMAGE_TYPES = {".png": "/image.png", ".jpg": "/image.jpg", ".jpeg": "/image.jpg"}

SURVEY_TYPES = [("stars", "Star rating (1–5)"), ("yesno", "Yes / No"), ("choice", "Multiple choice (2–4)")]
SURVEY_LOG = "/responses.csv"


# =========================================================================== serieel

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


class Board:
    """Eén seriële sessie met het bordje (gebruik met 'with')."""

    def __init__(self, port):
        self.s = serial.Serial()
        self.s.port = port
        self.s.baudrate = BAUD
        self.s.timeout = 0.2
        self.s.dtr = False      # laag houden: voorkomt resetten bij openen
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
            # Lees wat er is (min. 1 byte) i.p.v. te wachten op een vol blok
            self.buf += self.s.read(max(1, self.s.in_waiting))
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                text = raw.decode("utf-8", errors="replace").strip()
                if text.startswith("@"):
                    return text[1:]
        raise TimeoutError("No response from the board. Is the latest firmware installed?")

    def ok(self, line, timeout=3.0):
        reply = self.cmd(line, timeout)
        if not reply.startswith("OK"):
            raise RuntimeError(reply[4:] if reply.startswith("ERR") else reply)

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


def parse_info(reply):
    if not reply.startswith("INFO"):
        raise ValueError(f"Unexpected response: {reply}")
    info = {}
    for part in reply.split()[1:]:
        key, _, value = part.partition("=")
        info[key] = value
    info["ssid"] = from_hex(info.get("ssid", ""))
    info["open"] = info.get("open") == "1"
    return info


# =========================================================================== portal-inhoud

def load_content():
    content = dict(DEFAULT_CONTENT)
    try:
        with open(CONTENT_FILE, encoding="utf-8") as f:
            content.update(json.load(f))
    except (OSError, ValueError):
        pass
    return content


def save_content(content):
    with open(CONTENT_FILE, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)


def image_path(content):
    return os.path.join(ASSETS_DIR, content["image"]) if content.get("image") else ""


def clean_line(text):
    return " ".join(str(text).replace("\t", " ").split())


def survey_options(c):
    if c["survey_type"] == "stars":
        return ["1", "2", "3", "4", "5"]
    if c["survey_type"] == "yesno":
        return [clean_line(c["survey_yes"]) or "Yes", clean_line(c["survey_no"]) or "No"]
    return [clean_line(o) for o in c["survey_choices"] if clean_line(o)][:4]


def survey_id(c):
    key = "\n".join([c["survey_type"], clean_line(c["survey_question"])] + survey_options(c))
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:8]


def survey_cfg(c):
    return (f"{survey_id(c)}\n{c['survey_type']}\n{clean_line(c['survey_question'])}\n"
            + "\t".join(survey_options(c)) + "\n")


def on_color(hex_color):
    """Readable text colour on top of hex_color."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return "#121212" if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.55 else "#FFFFFF"


ALREADY_TEXT = "You have already answered this question. Thank you!"

# Survey script: phone time into the form, block double taps, and hide the
# question when this device has already answered (the board checks this too).
TIME_SCRIPT = ("<script>(function(){var d=new Date(),p=function(n){return(n<10?'0':'')+n};"
               "var t=document.getElementById('t');if(t)t.value=d.getFullYear()+'-'+p(d.getMonth()+1)+'-'"
               "+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());"
               "var f=document.querySelector('form.survey');if(!f)return;"
               "f.addEventListener('submit',function(){setTimeout(function(){"
               "var b=f.querySelectorAll('button');for(var i=0;i<b.length;i++)b[i].disabled=true;},0);});"
               "fetch('/voted',{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){"
               "if(j.voted)f.innerHTML='<p class=\"q\">" + ALREADY_TEXT + "</p>';}).catch(function(){});"
               "})();</script>")


def _page(c, inner, footer="", script=""):
    esc = html.escape
    acc, on_acc = c["accent"], on_color(c["accent"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(c['title'])}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
background:{c['bg']};color:{c['fg']};line-height:1.55}}
main{{max-width:560px;margin:0 auto;padding:32px 16px}}
.card{{background:{c['card']};border-radius:16px;padding:28px 24px;box-shadow:0 2px 12px rgba(0,0,0,.25)}}
.hero{{display:block;max-width:100%;height:auto;margin:0 auto 20px;border-radius:12px}}
h1{{margin:0 0 12px;font-size:28px;color:{acc}}}
p{{margin:0 0 12px}}p:last-child{{margin-bottom:0}}
footer{{text-align:center;font-size:13px;opacity:.7;padding:20px 16px 0}}
.survey{{margin-top:22px;padding-top:20px;border-top:1px solid rgba(127,127,127,.25)}}
.q{{font-weight:600;font-size:18px;margin:0 0 14px}}
.stars{{display:flex;flex-direction:row-reverse;justify-content:center;gap:2px}}
.stars button{{background:none;border:0;padding:2px 6px;font-size:44px;line-height:1;color:{acc};opacity:.3;cursor:pointer}}
.stars button:hover,.stars button:hover~button,.stars button:focus,.stars button:focus~button{{opacity:1}}
.scale{{display:flex;justify-content:space-between;font-size:12px;opacity:.6;max-width:270px;margin:2px auto 0}}
.opts{{display:flex;flex-wrap:wrap;gap:10px}}
.opts button{{flex:1 1 40%;min-height:50px;padding:12px;border:0;border-radius:12px;background:{acc};
color:{on_acc};font-size:17px;font-weight:600;cursor:pointer}}
.privacy{{font-size:12px;opacity:.6;margin:14px 0 0 !important}}
.back{{display:inline-block;margin-top:8px;color:{acc};font-weight:600}}
</style>
</head>
<body>
<main>
<div class="card">
{inner}
</div>
{footer}
</main>
{script}
</body>
</html>
"""


def render_survey(c):
    esc = html.escape
    opts = survey_options(c)
    if c["survey_type"] == "stars":
        buttons = "".join(
            f'<button type="submit" name="a" value="{i}" aria-label="{i} star{"s" if i > 1 else ""}">&#9733;</button>'
            for i in range(5, 0, -1))
        block = f'<div class="stars">{buttons}</div><div class="scale"><span>1</span><span>5</span></div>'
    else:
        block = '<div class="opts">' + "".join(
            f'<button type="submit" name="a" value="{i + 1}">{esc(o)}</button>' for i, o in enumerate(opts)) + "</div>"
    return (f'<form class="survey" method="post" action="/answer">'
            f'<p class="q">{esc(c["survey_question"])}</p>{block}'
            f'<input type="hidden" name="t" id="t">'
            f'<p class="privacy">Your answer is stored anonymously.</p></form>')


def render_html(c, image_url=""):
    esc = html.escape
    paragraphs = [p.strip() for p in c["text"].split("\n\n") if p.strip()]
    body = "\n".join(f"<p>{esc(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)
    img = f'<img class="hero" src="{esc(image_url)}" alt="">' if image_url else ""
    footer = f"<footer>{esc(c['footer'])}</footer>" if c.get("footer", "").strip() else ""
    heading = f"<h1>{esc(c['heading'])}</h1>" if c["heading"].strip() else ""
    survey = render_survey(c) if c.get("survey_on") else ""
    return _page(c, f"{img}\n{heading}\n{body}\n{survey}", footer, TIME_SCRIPT if survey else "")


def render_thanks(c):
    esc = html.escape
    inner = (f"<h1 id=\"th\">{esc(c['thanks_heading'])}</h1>"
             f"<p id=\"tt\">{esc(c['thanks_text']).replace(chr(10), '<br>')}</p>"
             f'<a class="back" href="/">&larr; Back</a>')
    # /thanks.html?dup=1: the board refused a second answer from this device
    script = ("<script>if(location.search.indexOf('dup')>=0){"
              "document.getElementById('th').textContent='Already answered';"
              f"document.getElementById('tt').textContent='{ALREADY_TEXT}';}}</script>")
    return _page(c, inner, script=script)


# =========================================================================== UI

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Portal Admin")
        self.configure(bg=BG)
        self.busy = False
        self.has_password = False
        self.content = load_content()
        self.thumb = None

        self._style()
        self._build()
        self._load_editor()
        self.update_idletasks()
        w = max(self.winfo_reqwidth(), int(self.winfo_fpixels("5.2i")))
        h = self.winfo_reqheight()
        self.geometry(f"{w}x{h}")
        self.minsize(w, h)
        self.after(100, self.refresh_ports)

    # ------------------------------------------------------------ opmaak
    def _style(self):
        st = ttk.Style(self)
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
        self.option_add("*TCombobox*Listbox.background", SURFACE2)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", PRIMARY)
        self.option_add("*TCombobox*Listbox.selectForeground", ON_PRIMARY)
        st.configure("TCheckbutton", background=CARD, foreground=TEXT, font=(FONT, 10),
                     indicatorbackground=SURFACE2, indicatorforeground=PRIMARY, bordercolor=OUTLINE)
        st.map("TCheckbutton", background=[("active", CARD)],
               indicatorbackground=[("selected", SURFACE2)])
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(16, 0, 16, 0),
                     bordercolor=CARD, lightcolor=CARD, darkcolor=CARD)
        st.configure("TNotebook.Tab", padding=(18, 8), font=(FONT, 10, "bold"),
                     background=BG, foreground=MUTED, borderwidth=0, lightcolor=BG, bordercolor=BG)
        st.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", PRIMARY)],
               lightcolor=[("selected", CARD)])
        st.configure("Horizontal.TProgressbar", troughcolor=SURFACE2, background=PRIMARY,
                     bordercolor=SURFACE2, lightcolor=PRIMARY, darkcolor=PRIMARY)
        st.configure("Treeview", background=SURFACE2, fieldbackground=SURFACE2, foreground=TEXT,
                     bordercolor=OUTLINE, lightcolor=OUTLINE, darkcolor=OUTLINE, rowheight=24,
                     font=(FONT, 9))
        st.map("Treeview", background=[("selected", PRIMARY)], foreground=[("selected", ON_PRIMARY)])
        st.configure("Treeview.Heading", background=CARD, foreground=MUTED, relief="flat",
                     bordercolor=OUTLINE, lightcolor=CARD, darkcolor=CARD, font=(FONT, 9, "bold"))
        st.map("Treeview.Heading", background=[("active", SURFACE3)])
        st.configure("Vertical.TScrollbar", background=SURFACE2, troughcolor=CARD, bordercolor=CARD,
                     lightcolor=SURFACE2, darkcolor=SURFACE2, arrowcolor=MUTED)

    def _button(self, parent, text, command, bg, fg=ON_PRIMARY, active=None, small=False):
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=active or bg, activeforeground=fg, relief="flat", bd=0,
                         padx=12 if small else 16, pady=6 if small else 10, cursor="hand2",
                         font=(FONT, 9 if small else 10, "bold"))

    def _ghost(self, parent, text, command, fg=PRIMARY, small=False):
        return self._button(parent, text, command, SURFACE2, fg=fg, active=SURFACE3, small=small)

    def _label(self, parent, text, bold=False, color=TEXT, size=10, bg=CARD):
        return tk.Label(parent, text=text, bg=bg, fg=color, anchor="w",
                        font=(FONT, size, "bold" if bold else "normal"))

    # ------------------------------------------------------------ opbouw
    def _build(self):
        header = tk.Frame(self, bg=SURFACE2, padx=20, pady=16)
        header.pack(fill="x")
        tk.Label(header, text="ESP32-PORTAL", bg=SURFACE2, fg=PRIMARY,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        tk.Label(header, text="Portal Admin", bg=SURFACE2, fg=TEXT,
                 font=(FONT, 17, "bold")).pack(anchor="w")

        # Verbinding
        conn = tk.Frame(self, bg=CARD, padx=20, pady=14)
        conn.pack(fill="x", padx=16, pady=16)
        row = tk.Frame(conn, bg=CARD)
        row.pack(fill="x")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly")
        self.port_box.pack(side="left", fill="x", expand=True)
        self.port_box.bind("<<ComboboxSelected>>", lambda e: self.read_info())
        self._button(row, "Search", self.refresh_ports, PRIMARY, active=PRIMARY_LIGHT, small=True).pack(
            side="left", padx=(8, 0))
        srow = tk.Frame(conn, bg=CARD)
        srow.pack(fill="x", pady=(10, 0))
        self.dot = tk.Canvas(srow, width=12, height=12, bg=CARD, highlightthickness=0)
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=IDLE, outline="")
        self.dot.pack(side="left")
        self.status_var = tk.StringVar(value="Not connected")
        tk.Label(srow, textvariable=self.status_var, bg=CARD, fg=TEXT, font=(FONT, 10),
                 anchor="w", justify="left", wraplength=420).pack(side="left", padx=(8, 0), fill="x")
        self.progress = ttk.Progressbar(conn, mode="determinate", style="Horizontal.TProgressbar")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        nb.add(self._build_wifi(nb), text="WiFi")
        nb.add(self._build_portal(nb), text="Portal")
        nb.add(self._build_survey(nb), text="Survey")
        nb.add(self._build_results(nb), text="Results")
        self.action_buttons = [self.save_btn, self.upload_btn, self.survey_upload_btn, self.results_btn]

    def _build_wifi(self, parent):
        tab = tk.Frame(parent, bg=CARD, padx=20, pady=18)

        # Netwerk aan/uit
        ap_row = tk.Frame(tab, bg=CARD)
        ap_row.pack(fill="x")
        self._label(ap_row, "Network").pack(side="left")
        self.ap_state = tk.Label(ap_row, text="—", bg=CARD, fg=MUTED, font=(FONT, 10, "bold"))
        self.ap_state.pack(side="left", padx=(10, 0))
        self.ap_on = True
        self.ap_btn = self._ghost(ap_row, "Turn network off", self.toggle_ap, fg=SECONDARY, small=True)
        self.ap_btn.pack(side="right")
        tk.Frame(tab, bg=OUTLINE, height=1).pack(fill="x", pady=(14, 14))

        self._label(tab, "Network name (SSID)").pack(fill="x", pady=(0, 4))
        self.ssid_var = tk.StringVar()
        ttk.Entry(tab, textvariable=self.ssid_var, font=(FONT, 11)).pack(fill="x")
        self.open_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="Open network (no password)", variable=self.open_var,
                        command=self._toggle_pass).pack(anchor="w", pady=(12, 0))
        self._label(tab, "WiFi password").pack(fill="x", pady=(10, 4))
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(tab, textvariable=self.pass_var, show="•", font=(FONT, 11))
        self.pass_entry.pack(fill="x")
        self.pass_hint = self._label(tab, "", color=MUTED, size=9)
        self.pass_hint.pack(fill="x", pady=(4, 0))
        self.info_var = tk.StringVar()
        tk.Label(tab, textvariable=self.info_var, bg=CARD, fg=MUTED, font=(FONT, 9),
                 anchor="w").pack(fill="x", pady=(10, 0))

        actions = tk.Frame(tab, bg=CARD)
        actions.pack(side="bottom", fill="x", pady=(16, 0))
        self.save_btn = self._button(actions, "Save WiFi to ESP32", self.save_wifi,
                                     PRIMARY, active=PRIMARY_LIGHT)
        self.save_btn.pack(fill="x")
        sub = tk.Frame(actions, bg=CARD)
        sub.pack(fill="x", pady=(8, 0))
        self._ghost(sub, "Refresh", self.read_info).pack(side="left", fill="x", expand=True)
        self._ghost(sub, "Factory reset", self.factory_reset, fg=SECONDARY).pack(
            side="left", fill="x", expand=True, padx=(8, 0))
        self._toggle_pass()
        return tab

    def _build_portal(self, parent):
        tab = tk.Frame(parent, bg=CARD, padx=20, pady=18)

        grid = tk.Frame(tab, bg=CARD)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        self.title_var = tk.StringVar()
        self.heading_var = tk.StringVar()
        self._label(grid, "Heading").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self._label(grid, "Page title (browser tab)").grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 4))
        ttk.Entry(grid, textvariable=self.heading_var, font=(FONT, 10)).grid(row=1, column=0, sticky="ew")
        ttk.Entry(grid, textvariable=self.title_var, font=(FONT, 10)).grid(row=1, column=1, sticky="ew", padx=(10, 0))

        self._label(tab, "Text  (empty line = new paragraph)").pack(fill="x", pady=(12, 4))
        self.text_box = tk.Text(tab, height=6, wrap="word", font=(FONT, 10), relief="flat", bd=0,
                                bg=SURFACE2, fg=TEXT, insertbackground=TEXT, selectbackground=PRIMARY,
                                selectforeground=ON_PRIMARY, highlightthickness=1,
                                highlightbackground=OUTLINE, highlightcolor=PRIMARY, padx=8, pady=6)
        self.text_box.pack(fill="x")

        self._label(tab, "Footer (optional)").pack(fill="x", pady=(12, 4))
        self.footer_var = tk.StringVar()
        ttk.Entry(tab, textvariable=self.footer_var, font=(FONT, 10)).pack(fill="x")

        # Afbeelding
        self._label(tab, "Image (PNG or JPG, top of the page)").pack(fill="x", pady=(12, 4))
        img_row = tk.Frame(tab, bg=CARD)
        img_row.pack(fill="x")
        self.thumb_label = tk.Label(img_row, bg=SURFACE2, width=12, height=5, text="none",
                                    fg=MUTED, font=(FONT, 9))
        self.thumb_label.pack(side="left")
        img_btns = tk.Frame(img_row, bg=CARD)
        img_btns.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self.image_info = self._label(img_btns, "", color=MUTED, size=9)
        self.image_info.pack(fill="x")
        b = tk.Frame(img_btns, bg=CARD)
        b.pack(fill="x", pady=(6, 0))
        self._ghost(b, "Choose image…", self.pick_image, small=True).pack(side="left")
        self._ghost(b, "Remove", self.remove_image, fg=SECONDARY, small=True).pack(side="left", padx=(8, 0))

        # Kleuren
        self._label(tab, "Colours").pack(fill="x", pady=(12, 4))
        colors = tk.Frame(tab, bg=CARD)
        colors.pack(fill="x")
        self.color_buttons = {}
        for key, label in (("bg", "Background"), ("card", "Card"), ("fg", "Text"), ("accent", "Heading")):
            cell = tk.Frame(colors, bg=CARD)
            cell.pack(side="left", padx=(0, 14))
            border = tk.Frame(cell, bg="#6A6A6A", padx=1, pady=1)   # zichtbare rand
            border.pack(side="left")
            sw = tk.Button(border, width=3, relief="flat", bd=0, cursor="hand2",
                           command=lambda k=key: self.pick_color(k))
            sw.pack()
            self._label(cell, label, size=9).pack(side="left", padx=(6, 0))
            self.color_buttons[key] = sw

        actions = tk.Frame(tab, bg=CARD)
        actions.pack(side="bottom", fill="x", pady=(16, 0))
        self.upload_btn = self._button(actions, "Upload portal to ESP32", self.upload_portal,
                                       PRIMARY, active=PRIMARY_LIGHT)
        self.upload_btn.pack(fill="x")
        sub = tk.Frame(actions, bg=CARD)
        sub.pack(fill="x", pady=(8, 0))
        self._ghost(sub, "Preview in browser", self.preview).pack(side="left", fill="x", expand=True)
        self._ghost(sub, "Restore defaults", self.reset_editor, fg=SECONDARY).pack(
            side="left", fill="x", expand=True, padx=(8, 0))
        return tab

    def _build_survey(self, parent):
        tab = tk.Frame(parent, bg=CARD, padx=20, pady=18)
        self.survey_on_var = tk.BooleanVar()
        ttk.Checkbutton(tab, text="Show a survey on the portal page", variable=self.survey_on_var).pack(anchor="w")

        self._label(tab, "Question type").pack(fill="x", pady=(12, 4))
        self.survey_type_var = tk.StringVar()
        box = ttk.Combobox(tab, textvariable=self.survey_type_var, state="readonly",
                           values=[label for _, label in SURVEY_TYPES])
        box.pack(fill="x")
        box.bind("<<ComboboxSelected>>", lambda e: self._show_option_fields())

        self._label(tab, "Question").pack(fill="x", pady=(12, 4))
        self.survey_q_var = tk.StringVar()
        ttk.Entry(tab, textvariable=self.survey_q_var, font=(FONT, 10)).pack(fill="x")

        self._label(tab, "Answers").pack(fill="x", pady=(12, 4))
        self.options_frame = tk.Frame(tab, bg=CARD)
        self.options_frame.pack(fill="x")
        self.stars_hint = self._label(self.options_frame, "Visitors tap 1 to 5 stars.", color=MUTED, size=9)
        self.yes_var, self.no_var = tk.StringVar(), tk.StringVar()
        self.yesno_frame = tk.Frame(self.options_frame, bg=CARD)
        self.yesno_frame.columnconfigure(0, weight=1)
        self.yesno_frame.columnconfigure(1, weight=1)
        ttk.Entry(self.yesno_frame, textvariable=self.yes_var, font=(FONT, 10)).grid(row=0, column=0, sticky="ew")
        ttk.Entry(self.yesno_frame, textvariable=self.no_var, font=(FONT, 10)).grid(
            row=0, column=1, sticky="ew", padx=(10, 0))
        self.choice_vars = [tk.StringVar() for _ in range(4)]
        self.choice_frame = tk.Frame(self.options_frame, bg=CARD)
        self.choice_frame.columnconfigure(0, weight=1)
        self.choice_frame.columnconfigure(1, weight=1)
        for i, v in enumerate(self.choice_vars):
            ttk.Entry(self.choice_frame, textvariable=v, font=(FONT, 10)).grid(
                row=i // 2, column=i % 2, sticky="ew", padx=(0 if i % 2 == 0 else 10, 0), pady=(0, 8))

        self._label(tab, "Thank-you page").pack(fill="x", pady=(12, 4))
        grid = tk.Frame(tab, bg=CARD)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self.thanks_h_var, self.thanks_t_var = tk.StringVar(), tk.StringVar()
        ttk.Entry(grid, textvariable=self.thanks_h_var, font=(FONT, 10), width=16).grid(row=0, column=0, sticky="ew")
        ttk.Entry(grid, textvariable=self.thanks_t_var, font=(FONT, 10)).grid(row=0, column=1, sticky="ew", padx=(10, 0))

        self._label(tab, "The image and colours come from the Portal tab. Changing the question or answers "
                         "starts a new count; earlier answers stay in the log.",
                    color=MUTED, size=9).pack(fill="x", pady=(14, 0))
        tab.bind("<Configure>", lambda e: self._wrap_labels(tab, e.width))

        actions = tk.Frame(tab, bg=CARD)
        actions.pack(side="bottom", fill="x", pady=(16, 0))
        self.survey_upload_btn = self._button(actions, "Upload portal + survey to ESP32", self.upload_portal,
                                              PRIMARY, active=PRIMARY_LIGHT)
        self.survey_upload_btn.pack(fill="x")
        sub = tk.Frame(actions, bg=CARD)
        sub.pack(fill="x", pady=(8, 0))
        self._ghost(sub, "Preview in browser", self.preview).pack(side="left", fill="x", expand=True)
        return tab

    def _wrap_labels(self, frame, width):
        for w in frame.winfo_children():
            if isinstance(w, tk.Label) and w.cget("fg") == MUTED:
                w.configure(wraplength=max(200, width - 50), justify="left")

    def _show_option_fields(self):
        for f in (self.stars_hint, self.yesno_frame, self.choice_frame):
            f.pack_forget()
        t = self._survey_type_key()
        {"stars": self.stars_hint, "yesno": self.yesno_frame, "choice": self.choice_frame}[t].pack(fill="x")

    def _survey_type_key(self):
        label = self.survey_type_var.get()
        return next((k for k, l in SURVEY_TYPES if l == label), "stars")

    def _build_results(self, parent):
        tab = tk.Frame(parent, bg=CARD, padx=20, pady=18)
        self.results_summary = tk.Label(tab, text="Click Load results to read the log from the ESP32.",
                                        bg=CARD, fg=TEXT, font=(FONT, 12, "bold"), anchor="w", justify="left")
        self.results_summary.pack(fill="x")
        self.results_detail = self._label(tab, "", color=MUTED, size=9)
        self.results_detail.pack(fill="x", pady=(2, 10))

        table = tk.Frame(tab, bg=CARD)
        table.pack(fill="both", expand=True)
        cols = (("time", "Time", 130), ("device", "Device", 130), ("question", "Question", 150),
                ("answer", "Answer", 80))
        self.tree = ttk.Treeview(table, columns=[c for c, _, _ in cols], show="headings", height=11)
        for key, title, width in cols:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, anchor="w", stretch=True)
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.log_rows = []
        self.log_raw = b""

        actions = tk.Frame(tab, bg=CARD)
        actions.pack(side="bottom", fill="x", pady=(16, 0))
        self.results_btn = self._button(actions, "Load results", self.load_results, PRIMARY, active=PRIMARY_LIGHT)
        self.results_btn.pack(fill="x")
        sub = tk.Frame(actions, bg=CARD)
        sub.pack(fill="x", pady=(8, 0))
        self._ghost(sub, "Export CSV…", self.export_results).pack(side="left", fill="x", expand=True)
        self._ghost(sub, "Clear log", self.clear_results, fg=SECONDARY).pack(
            side="left", fill="x", expand=True, padx=(8, 0))
        return tab

    # ------------------------------------------------------------ status / taken
    def set_status(self, text, color=IDLE):
        self.status_var.set(text)
        self.dot.itemconfigure(self.dot_id, fill=color)

    def run_bg(self, work, done, busy_text, progress_total=None):
        """Voert work(report) uit in een thread; report(n) werkt de voortgangsbalk bij."""
        if self.busy:
            return
        self.busy = True
        for b in self.action_buttons:
            b.configure(state="disabled")
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
        for b in self.action_buttons:
            b.configure(state="normal")
        self.progress.pack_forget()
        if error:
            msg = str(error)
            if isinstance(error, serial.SerialException):
                msg = "Cannot open the port. Is the serial monitor or another program still using it?"
            self.set_status(msg, SECONDARY)
        else:
            done(result)

    def port(self):
        v = self.port_var.get()
        return v.split(" — ")[0] if v else None

    # ------------------------------------------------------------ WiFi
    def _toggle_pass(self):
        if self.open_var.get():
            self.pass_entry.state(["disabled"])
            self.pass_hint.configure(text="Anyone can connect without a password.")
        else:
            self.pass_entry.state(["!disabled"])
            hint = "8–63 characters."
            if self.has_password:
                hint += " Leave empty to keep the current password."
            self.pass_hint.configure(text=hint)

    def _show_ap(self):
        if self.ap_on:
            self.ap_state.configure(text="ON", fg=SUCCESS)
            self.ap_btn.configure(text="Turn network off", fg=SECONDARY, activeforeground=SECONDARY)
        else:
            self.ap_state.configure(text="OFF", fg=SECONDARY)
            self.ap_btn.configure(text="Turn network on", fg=PRIMARY, activeforeground=PRIMARY)

    def toggle_ap(self):
        port = self.port()
        if not port:
            self.set_status("Select a port first.", SECONDARY)
            return
        turn_on = not self.ap_on

        def work(_r):
            with Board(port) as b:
                b.ok(f"AP {1 if turn_on else 0}", timeout=5)
                return parse_info(b.cmd("INFO"))

        def done(info):
            self._show_info(info)
            if self.ap_on:
                self.set_status(f"Network '{info['ssid']}' is on.", SUCCESS)
            else:
                self.set_status("Network is off. It stays off after a restart until you turn it on again.",
                                SECONDARY)

        self.run_bg(work, done, "Turning network on…" if turn_on else "Turning network off…")

    def _show_info(self, info):
        self.ap_on = info.get("ap", "1") == "1"
        self._show_ap()
        self.ssid_var.set(info["ssid"])
        self.has_password = not info["open"]
        self.open_var.set(info["open"])
        self.pass_var.set("")
        self._toggle_pass()
        self.info_var.set(f"IP {info.get('ip', '?')}  ·  {info.get('clients', '0')} connected device(s)"
                          f"  ·  firmware {info.get('fw', '?')}"
                          + (f"  ·  display {info['lcd']}" if "lcd" in info else "")
                          + self._battery_text(info))

    @staticmethod
    def _battery_text(info):
        bat = info.get("bat")
        if bat is None:
            return ""
        if bat == "-1":
            return "  ·  no battery"
        return f"  ·  battery {bat}%" + (" (charging)" if info.get("chg") == "1" else "")

    def refresh_ports(self):
        ports = find_ports()
        labels = [label for _, label, _ in ports]
        self.port_box["values"] = labels
        esp = [label for _, label, is_esp in ports if is_esp]
        if esp:
            self.port_var.set(esp[0])
            self.read_info()
        else:
            self.port_var.set(labels[0] if labels else "")
            self.set_status("No ESP32 found. Connect the board and click Search.", SECONDARY)

    def read_info(self):
        port = self.port()
        if not port:
            return

        def done(info):
            self._show_info(info)
            self.set_status(f"Connected to {port}", SUCCESS)

        self.run_bg(lambda _r: parse_info(send_command(port, "INFO")), done, f"Connecting to {port}…")

    def _wait_for_restart(self, port):
        time.sleep(2.5)
        deadline = time.time() + 20
        last = None
        while time.time() < deadline:
            try:
                return parse_info(send_command(port, "INFO", timeout=1.5))
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1)
        raise TimeoutError(f"Board not responding after restart ({last}). Click Refresh.")

    def save_wifi(self):
        port = self.port()
        if not port:
            self.set_status("Select a port first.", SECONDARY)
            return
        ssid = self.ssid_var.get().strip()
        if not 1 <= len(ssid.encode("utf-8")) <= 32:
            messagebox.showwarning("Portal Admin", "Network name must be 1 to 32 characters.")
            return
        if self.open_var.get():
            pass_arg = "-"
        else:
            pw = self.pass_var.get()
            if not pw:
                if not self.has_password:
                    messagebox.showwarning("Portal Admin", "Enter a password, or choose 'Open network'.")
                    return
                pass_arg = "="
            elif not 8 <= len(pw.encode("utf-8")) <= 63:
                messagebox.showwarning("Portal Admin", "Password must be 8 to 63 characters.")
                return
            else:
                pass_arg = to_hex(pw)

        def work(_r):
            reply = send_command(port, f"SET {to_hex(ssid)} {pass_arg}")
            if reply.startswith("ERR"):
                raise ValueError(reply[4:] or "Save failed")
            return self._wait_for_restart(port)

        def done(info):
            self._show_info(info)
            self.set_status(f"Saved. The network is now called '{info['ssid']}'.", SUCCESS)

        self.run_bg(work, done, "Saving and restarting…")

    def factory_reset(self):
        port = self.port()
        if not port or not messagebox.askyesno(
                "Factory reset", "Reset network name and password to the default values?"):
            return

        def work(_r):
            send_command(port, "RESET")
            return self._wait_for_restart(port)

        def done(info):
            self._show_info(info)
            self.set_status(f"Defaults restored ('{info['ssid']}').", SUCCESS)

        self.run_bg(work, done, "Factory reset…")

    # ------------------------------------------------------------ Portal-editor
    def _load_editor(self):
        c = self.content
        self.title_var.set(c["title"])
        self.heading_var.set(c["heading"])
        self.footer_var.set(c.get("footer", ""))
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", c["text"])
        for key, btn in self.color_buttons.items():
            btn.configure(bg=c[key], activebackground=c[key])
        self._update_thumb()
        self.survey_on_var.set(bool(c["survey_on"]))
        self.survey_type_var.set(dict(SURVEY_TYPES).get(c["survey_type"], SURVEY_TYPES[0][1]))
        self.survey_q_var.set(c["survey_question"])
        self.yes_var.set(c["survey_yes"])
        self.no_var.set(c["survey_no"])
        for v, val in zip(self.choice_vars, list(c["survey_choices"]) + [""] * 4):
            v.set(val)
        self.thanks_h_var.set(c["thanks_heading"])
        self.thanks_t_var.set(c["thanks_text"])
        self._show_option_fields()

    def _collect(self):
        c = self.content
        c["title"] = self.title_var.get().strip() or "Welcome"
        c["heading"] = self.heading_var.get().strip()
        c["footer"] = self.footer_var.get().strip()
        c["text"] = self.text_box.get("1.0", "end").strip()
        c["survey_on"] = self.survey_on_var.get()
        c["survey_type"] = self._survey_type_key()
        c["survey_question"] = self.survey_q_var.get().strip()
        c["survey_yes"] = self.yes_var.get().strip()
        c["survey_no"] = self.no_var.get().strip()
        c["survey_choices"] = [v.get().strip() for v in self.choice_vars]
        c["thanks_heading"] = self.thanks_h_var.get().strip() or "Thank you!"
        c["thanks_text"] = self.thanks_t_var.get().strip()
        save_content(c)
        return c

    def _update_thumb(self):
        path = image_path(self.content)
        if not path or not os.path.exists(path):
            self.content["image"] = ""
            self.thumb = None
            self.thumb_label.configure(image="", text="none", width=12, height=5)
            self.image_info.configure(text="No image selected.")
            return
        size_kb = os.path.getsize(path) / 1024
        self.image_info.configure(text=f"{self.content['image']}  ·  {size_kb:.0f} kB")
        try:
            img = tk.PhotoImage(file=path)   # Tk leest PNG; JPG krijgt geen miniatuur
            factor = max(1, -(-max(img.width(), img.height()) // 90))
            self.thumb = img.subsample(factor, factor)
            self.thumb_label.configure(image=self.thumb, text="", width=96, height=96)
        except tk.TclError:
            self.thumb = None
            self.thumb_label.configure(image="", text="JPG", width=12, height=5)

    def pick_image(self):
        path = filedialog.askopenfilename(
            title="Choose an image",
            filetypes=[("Images", "*.png *.jpg *.jpeg"), ("PNG", "*.png"), ("JPG", "*.jpg *.jpeg")])
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in IMAGE_TYPES:
            messagebox.showwarning("Portal Admin", "Choose a PNG or JPG file.")
            return
        size = os.path.getsize(path)
        if size > MAX_IMAGE_BYTES:
            messagebox.showwarning(
                "Portal Admin",
                f"This image is {size / 1024:.0f} kB. The maximum is {MAX_IMAGE_BYTES // 1024} kB.\n\n"
                "Resize the image (e.g. to 800 px wide) and try again.")
            return
        os.makedirs(ASSETS_DIR, exist_ok=True)
        name = "image" + (".jpg" if ext == ".jpeg" else ext)
        for old in os.listdir(ASSETS_DIR):
            os.remove(os.path.join(ASSETS_DIR, old))
        shutil.copyfile(path, os.path.join(ASSETS_DIR, name))
        self.content["image"] = name
        self._update_thumb()
        self._collect()

    def remove_image(self):
        self.content["image"] = ""
        self._update_thumb()
        self._collect()

    def pick_color(self, key):
        _, hex_color = colorchooser.askcolor(color=self.content[key], title="Choose a colour")
        if hex_color:
            self.content[key] = hex_color.upper()
            self.color_buttons[key].configure(bg=hex_color, activebackground=hex_color)
            self._collect()

    def reset_editor(self):
        if not messagebox.askyesno("Restore defaults",
                                   "Reset all text, colours and the image in the editor?"):
            return
        self.content = dict(DEFAULT_CONTENT)
        self._load_editor()
        self._collect()

    def preview(self):
        c = self._collect()
        folder = os.path.join(tempfile.gettempdir(), "esp32-portal-preview")
        os.makedirs(folder, exist_ok=True)
        url = ""
        src = image_path(c)
        if src:
            shutil.copyfile(src, os.path.join(folder, c["image"]))
            url = c["image"]
        out = os.path.join(folder, "index.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(render_html(c, url))
        with open(os.path.join(folder, "thanks.html"), "w", encoding="utf-8") as f:
            f.write(render_thanks(c))
        webbrowser.open("file:///" + out.replace("\\", "/"))

    def upload_portal(self):
        port = self.port()
        if not port:
            self.set_status("Select a port first.", SECONDARY)
            return
        c = self._collect()
        if c["survey_on"]:
            if not c["survey_question"]:
                messagebox.showwarning("Portal Admin", "Enter a survey question (Survey tab).")
                return
            if len(survey_options(c)) < 2:
                messagebox.showwarning("Portal Admin", "Enter at least 2 answers (Survey tab).")
                return
        src = image_path(c)
        image_data = b""
        target = ""
        if src:
            with open(src, "rb") as f:
                image_data = f.read()
            target = IMAGE_TYPES[os.path.splitext(src)[1].lower()]
        page = render_html(c, f"{target[1:]}?v={int(time.time())}" if target else "").encode("utf-8")
        thanks = render_thanks(c).encode("utf-8")
        cfg = survey_cfg(c).encode("utf-8") if c["survey_on"] else b""
        total = len(image_data) + len(page) + len(thanks) + len(cfg)

        def work(report):
            with Board(port) as b:
                # also clean up image names used by older versions of this app
                for name in set(IMAGE_TYPES.values()) | {"/afbeelding.png", "/afbeelding.jpg"}:
                    if name != target:
                        b.ok(f"FDEL {name}")
                if image_data:
                    b.put_file(target, image_data, report)
                done_bytes = len(image_data)
                b.put_file("/index.html", page, lambda n: report(done_bytes + n))
                done_bytes += len(page)
                b.put_file("/thanks.html", thanks, lambda n: report(done_bytes + n))
                if cfg:
                    b.put_file("/survey.cfg", cfg)
                else:
                    b.ok("FDEL /survey.cfg")
                return b.cmd("FS")

        def done(fs):
            used = total_kb = ""
            if fs.startswith("FS"):
                vals = dict(p.split("=") for p in fs.split()[1:])
                used = int(vals.get("used", 0)) // 1024
                total_kb = int(vals.get("total", 0)) // 1024
            self.set_status(f"Portal uploaded ({total / 1024:.0f} kB). Storage: {used} of {total_kb} kB in use.",
                            SUCCESS)

        self.run_bg(work, done, "Uploading portal…", progress_total=total)

    # ------------------------------------------------------------ Results
    def load_results(self):
        port = self.port()
        if not port:
            self.set_status("Select a port first.", SECONDARY)
            return

        def work(_r):
            data = b""
            with Board(port) as b:
                while True:
                    reply = b.cmd(f"FREAD {SURVEY_LOG} {len(data)}")
                    if reply.startswith("EOF"):
                        break
                    if not reply.startswith("DATA"):
                        raise RuntimeError(reply[4:] if reply.startswith("ERR") else reply)
                    chunk = base64.b64decode(reply[5:].strip())
                    if not chunk:
                        break
                    data += chunk
            return data

        def done(data):
            self.log_raw = data
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace"))))
            self.log_rows = rows
            self.tree.delete(*self.tree.get_children())
            for r in reversed(rows):
                self.tree.insert("", "end", values=(r.get("time", ""), r.get("device", ""),
                                                    r.get("question", ""), r.get("answer", "")))
            self._summarise(rows)
            self.set_status(f"Loaded {len(rows)} answer(s) from the ESP32.", SUCCESS)

        self.run_bg(work, done, "Reading results…")

    def _summarise(self, rows):
        if not rows:
            self.results_summary.configure(text="No answers yet.")
            self.results_detail.configure(text="")
            return
        sid = survey_id(self.content)
        current = [r for r in rows if r.get("survey_id") == sid] or \
                  [r for r in rows if r.get("survey_id") == rows[-1].get("survey_id")]
        question = current[-1].get("question", "")
        answers = [r.get("answer", "") for r in current]
        if all(a in "12345" and a for a in answers) and self.content.get("survey_type") == "stars":
            avg = sum(int(a) for a in answers) / len(answers)
            dist = "  ".join(f"{i}\u2605 {answers.count(str(i))}" for i in range(5, 0, -1))
            summary = f"\u2605 {avg:.1f} / 5   ({len(answers)} votes)"
        else:
            counts = {}
            for a in answers:
                counts[a] = counts.get(a, 0) + 1
            dist = "   ".join(f"{a}: {n} ({n * 100 // len(answers)}%)" for a, n in counts.items())
            summary = f"{len(answers)} votes"
        self.results_summary.configure(text=summary)
        older = len(rows) - len(current)
        extra = f"   ·   {older} answer(s) from earlier surveys" if older else ""
        self.results_detail.configure(text=f"\u201c{question}\u201d   {dist}{extra}", wraplength=440,
                                      justify="left")

    def export_results(self):
        if not self.log_raw:
            messagebox.showinfo("Portal Admin", "Load the results first.")
            return
        path = filedialog.asksaveasfilename(title="Export results", defaultextension=".csv",
                                            initialfile="responses.csv",
                                            filetypes=[("CSV (Excel)", "*.csv")])
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as f:   # BOM: Excel opens UTF-8 correctly
            f.write(self.log_raw.decode("utf-8", errors="replace"))
        self.set_status(f"Exported to {path}", SUCCESS)

    def clear_results(self):
        port = self.port()
        if not port or not messagebox.askyesno(
                "Clear log", "Delete all survey answers on the ESP32? Devices that already answered "
                             "can then answer again. This cannot be undone.\n\n"
                             "Tip: export the CSV first."):
            return

        def work(_r):
            with Board(port) as b:
                b.ok("LOGCLEAR")

        def done(_):
            self.log_rows, self.log_raw = [], b""
            self.tree.delete(*self.tree.get_children())
            self._summarise([])
            self.set_status("Survey log cleared.", SUCCESS)

        self.run_bg(work, done, "Clearing log…")


if __name__ == "__main__":
    App().mainloop()
