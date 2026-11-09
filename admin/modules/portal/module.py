"""WiFi Portal module for the ESP32 Toolbox.

WiFi settings, portals (landing pages with the page editor), surveys, results and
backups of the ESP32-Portal firmware (the PlatformIO project in the repository root).
Loaded by admin/toolbox.pyw; see admin/modules/README.md for the module interface.
"""

import copy
import hashlib
import html
import csv
import io
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
import zipfile
from tkinter import filedialog, messagebox, simpledialog, ttk

import espcore as core
from espcore import (BG, CARD, SURFACE2, SURFACE3, OUTLINE, TEXT, MUTED, PRIMARY, PRIMARY_LIGHT,  # noqa: F401
                     ON_PRIMARY, SECONDARY, SUCCESS, IDLE, FONT, to_hex, from_hex, send_command)
import portal_editor as pe

MODULE_ID = "portal"
if os.environ.get("PORTAL_ADMIN_DIR"):          # testing with a separate content folder
    HERE = os.environ["PORTAL_ADMIN_DIR"]
else:
    HERE = core.data_dir(MODULE_ID)             # admin/data/portal (stays on this PC)
CONTENT_FILE = os.path.join(HERE, "portal_content.json")
ASSETS_DIR = os.path.join(HERE, "portal_assets")


def _migrate_old_files():
    """The Portal Admin app (before the Toolbox) kept its content in the admin folder itself."""
    if os.environ.get("PORTAL_ADMIN_DIR") or os.path.exists(CONTENT_FILE):
        return
    for name in ("portal_content.json", "portal_content.backup.json", "portal_assets"):
        old = os.path.join(core.BASE, name)
        if os.path.exists(old) and not os.path.exists(os.path.join(HERE, name)):
            shutil.move(old, os.path.join(HERE, name))


_migrate_old_files()

DEFAULT_CONTENT = {
    "title": "Welcome",
    "footer": "",
    "bg": "#121212",
    "card": "#1E1E1E",
    "fg": "#E1E1E1",
    "accent": "#9C89D7",
    "font": "sans",
    "layout": "card",
    "width": "normal",
    "blocks": [
        {"type": "heading", "text": "Welcome", "size": "l", "align": "left"},
        {"type": "text", "align": "left",
         "html": "<p>You are connected to this local WiFi network.</p>"
                 "<p>This page is stored on the device itself; there is no internet connection.</p>"},
    ],
    # survey
    "survey_on": False,
    "survey_type": "stars",
    "survey_question": "How many stars do you give this photo?",
    "survey_yes": "Yes",
    "survey_no": "No",
    "survey_choices": ["Option A", "Option B", "", ""],
    "survey_placeholder": "Type your answer here",
    "thanks_heading": "Thank you!",
    "thanks_text": "Your answer has been saved.",
}


SURVEY_TYPES = [("stars", "Star rating (1–5)"), ("yesno", "Yes / No"), ("choice", "Multiple choice (2–4)"),
                ("open", "Open question (text, max. 240 characters)")]
OPEN_MAX = 240          # must match SURVEY_MAX_TEXT in the firmware
SURVEY_LOG = "/responses.csv"
BOARD_CONTENT = "/portal.json"      # page + survey settings, so another PC can download them
SURVEY_KEYS = ("survey_on", "survey_type", "survey_question", "survey_yes", "survey_no", "survey_choices",
               "survey_placeholder", "thanks_heading", "thanks_text")


# =========================================================================== serial

class Board(core.Board):
    """Board connection with the portal-specific WIFI command."""

    def wifi(self):
        """{"ssid", "pass", "ap"} from the board, or None for firmware older than 2.5."""
        reply = self.cmd("WIFI")
        if not reply.startswith("WIFI"):
            return None
        vals = dict(part.partition("=")[::2] for part in reply.split()[1:])
        return {"ssid": from_hex(vals.get("ssid", "")), "pass": from_hex(vals.get("pass", "")),
                "ap": vals.get("ap", "1") == "1"}


def parse_info(reply):
    info = core.parse_kv(reply, "INFO")
    info["ssid"] = from_hex(info.get("ssid", ""))
    info["open"] = info.get("open") == "1"
    return info


def content_from(saved):
    """Complete, validated content from a saved dict (local file or the board's copy)."""
    content = copy.deepcopy(DEFAULT_CONTENT)
    if isinstance(saved, dict):
        if "blocks" not in saved:
            content.pop("blocks")          # old file: build blocks from heading/text/image
        content.update(saved)
    pe.migrate(content)
    for old in ("heading", "text", "image"):
        content.pop(old, None)
    content.update(pe.normalize_edit(content, DEFAULT_CONTENT))
    if content["survey_type"] not in dict(SURVEY_TYPES):
        content["survey_type"] = "stars"
    content["survey_on"] = bool(content["survey_on"])
    for key in SURVEY_KEYS[2:]:
        if key != "survey_choices":
            content[key] = str(content.get(key) or "")[:300]
    choices = content["survey_choices"] if isinstance(content["survey_choices"], list) else []
    content["survey_choices"] = ([str(x)[:100] for x in choices] + [""] * 4)[:4]
    return content


# --- portals (profiles): the store holds up to MAX_PORTALS portals, each a full content dict
#     plus "id" (4 hex), "name" and "ssid" (empty = the general network name)
MAX_PORTALS = 8          # must match MAX_PORTALS in the firmware
PORTAL_ID_RE = re.compile(r"^[0-9a-f]{4}$")
PORTAL_FILE_RE = re.compile(r"^p-[0-9a-f]{4}-(index\.html|thanks\.html|survey\.cfg)$")
PORTAL_KEYS = ("id", "name", "ssid")


def new_portal_id(taken):
    while True:
        pid = os.urandom(2).hex()
        if pid not in taken:
            return pid


def clean_ssid(text):
    text = clean_line(text)
    while len(text.encode("utf-8")) > 32:
        text = text[:-1]
    return text


def portal_from(saved, taken=(), name="Portal 1"):
    """One validated portal; gets a new id when it has none (or a duplicate one)."""
    p = content_from(saved)
    pid = p.get("id")
    p["id"] = pid if isinstance(pid, str) and PORTAL_ID_RE.match(pid) and pid not in taken else new_portal_id(taken)
    p["name"] = clean_line(p.get("name") or name)[:40] or name
    p["ssid"] = clean_ssid(p.get("ssid") or "")
    for key in ("board_files", "synced_hash", "portals", "current"):   # store-level keys
        p.pop(key, None)
    return p


def store_from(saved):
    """Validated store from the local file or the board's copy (also the old single-page format)."""
    store = {"portals": []}
    if isinstance(saved, dict) and isinstance(saved.get("portals"), list):
        items = saved["portals"][:MAX_PORTALS]
    else:
        # single page from app versions before portals; a fixed id, so the copy on this PC and
        # the one on the board (both migrated separately) still compare equal
        items = [dict(saved, id=saved.get("id") or "0001") if isinstance(saved, dict) else saved]
    for i, item in enumerate(items):
        store["portals"].append(portal_from(item, {q["id"] for q in store["portals"]}, f"Portal {i + 1}"))
    if not store["portals"]:
        store["portals"].append(portal_from(None))
    ids = [q["id"] for q in store["portals"]]
    if isinstance(saved, dict):
        store["current"] = saved.get("current") if saved.get("current") in ids else ids[0]
        store["board_files"] = [n for n in saved.get("board_files", []) if isinstance(n, str)]
        if isinstance(saved.get("synced_hash"), str):
            store["synced_hash"] = saved["synced_hash"]
    else:
        store["current"] = ids[0]
    return store


def load_store():
    try:
        with open(CONTENT_FILE, encoding="utf-8") as f:
            return store_from(json.load(f))
    except (OSError, ValueError):
        return store_from(None)


def page_data(c):
    """One portal as stored on the board: page, survey, name and network name."""
    return {k: c.get(k) for k in pe.EDIT_KEYS + SURVEY_KEYS + PORTAL_KEYS}


def store_data(store):
    return {"portals": [page_data(p) for p in store["portals"]]}


def store_hash(store):
    raw = json.dumps(store_data(store), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def save_store(store):
    tmp = CONTENT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONTENT_FILE)


def all_images(store):
    out = []
    for p in store["portals"]:
        for n in pe.used_files(p["blocks"]):
            if n not in out:
                out.append(n)
    return out


def portals_cfg(store):
    return "".join(f"{p['id']}\t{clean_line(p['name'])}\t{p['ssid']}\n" for p in store["portals"])


def asset_path(name):
    return os.path.join(ASSETS_DIR, name)


def clean_line(text):
    return " ".join(str(text).replace("\t", " ").split())


def survey_options(c):
    if c["survey_type"] == "stars":
        return ["1", "2", "3", "4", "5"]
    if c["survey_type"] == "yesno":
        return [clean_line(c["survey_yes"]) or "Yes", clean_line(c["survey_no"]) or "No"]
    if c["survey_type"] == "open":
        return []
    return [clean_line(o) for o in c["survey_choices"] if clean_line(o)][:4]


def survey_id(c):
    """<portal id>-<hash of the question>: answers can be traced back to their portal."""
    key = "\n".join([c["survey_type"], clean_line(c["survey_question"])] + survey_options(c))
    return f"{c.get('id', 'xxxx')}-{hashlib.md5(key.encode('utf-8')).hexdigest()[:8]}"


def survey_cfg(c):
    return (f"{survey_id(c)}\n{c['survey_type']}\n{clean_line(c['survey_question'])}\n"
            + ("\t".join(survey_options(c)) or clean_line(c.get("survey_placeholder", ""))) + "\n")


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
               "var a=f.querySelector('textarea'),n=f.querySelector('.cnt');"
               "if(a&&n)a.addEventListener('input',function(){n.textContent=a.value.length+' / '+a.maxLength;});"
               "f.addEventListener('submit',function(){setTimeout(function(){"
               "var b=f.querySelectorAll('button');for(var i=0;i<b.length;i++)b[i].disabled=true;},0);});"
               "fetch('/voted',{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){"
               "if(j.voted)f.innerHTML='<p class=\"q\">" + ALREADY_TEXT + "</p>';}).catch(function(){});"
               "})();</script>")


def _page(c, inner, footer="", script=""):
    esc = html.escape
    acc, on_acc = c["accent"], on_color(c["accent"])
    font = pe.FONTS.get(c.get("font"), pe.FONTS["sans"])
    width = pe.WIDTHS.get(c.get("width"), 560)
    flat = c.get("layout") == "flat"
    card_css = (f"background:{c['bg']};padding:8px 4px" if flat else
                f"background:{c['card']};border-radius:16px;padding:28px 24px;box-shadow:0 2px 12px rgba(0,0,0,.25)")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(c['title'])}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;font-family:{font};
background:{c['bg']};color:{c['fg']};line-height:1.55}}
main{{max-width:{width}px;margin:0 auto;padding:32px 16px}}
.card{{{card_css}}}
h1{{margin:0 0 12px;font-size:28px;color:{acc}}}
{pe.blocks_css(c)}
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
.open textarea{{display:block;width:100%;min-height:110px;padding:12px;border-radius:12px;font:inherit;font-size:16px;
color:inherit;background:rgba(127,127,127,.12);border:1px solid rgba(127,127,127,.35);resize:vertical}}
.open textarea:focus{{outline:none;border-color:{acc}}}
.cnt{{text-align:right;font-size:12px;opacity:.6;margin:4px 0 10px}}
.send{{width:100%;min-height:50px;border:0;border-radius:12px;background:{acc};color:{on_acc};
font-size:17px;font-weight:600;cursor:pointer}}
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
    elif c["survey_type"] == "open":
        block = (f'<div class="open"><textarea name="txt" maxlength="{OPEN_MAX}" required '
                 f'placeholder="{esc(c.get("survey_placeholder", ""))}" aria-label="Your answer"></textarea>'
                 f'<div class="cnt">0 / {OPEN_MAX}</div><button type="submit" class="send">Send</button></div>')
    else:
        block = '<div class="opts">' + "".join(
            f'<button type="submit" name="a" value="{i + 1}">{esc(o)}</button>' for i, o in enumerate(opts)) + "</div>"
    privacy = "Your answer is stored anonymously."
    if c["survey_type"] == "open":
        privacy += " Please do not include your name or other personal details."
    return (f'<form class="survey" method="post" action="/answer" accept-charset="utf-8">'
            f'<p class="q">{esc(c["survey_question"])}</p>{block}'
            f'<input type="hidden" name="t" id="t">'
            f'<p class="privacy">{privacy}</p></form>')


def render_html(c):
    """Landing page; images are referenced by their plain file name (same folder)."""
    esc = html.escape
    footer = f"<footer>{esc(c['footer'])}</footer>" if c.get("footer", "").strip() else ""
    survey = render_survey(c) if c.get("survey_on") else ""
    return _page(c, f"{pe.render_blocks(c)}\n{survey}", footer, TIME_SCRIPT if survey else "")


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


# =========================================================================== backup

BACKUP_FORMAT = 1
MAX_BACKUP_LOG = 3 * 1024 * 1024


def merge_logs(*logs):
    """Survey logs combined: one header, every answer once, in order."""
    header, seen, out = None, set(), []
    for data in logs:
        lines = data.decode("utf-8", errors="replace").splitlines()
        if not lines:
            continue
        header = header or lines[0]
        for line in lines[1:]:
            if line.strip() and line not in seen:
                seen.add(line)
                out.append(line)
    return ("\n".join([header] + out) + "\n").encode("utf-8") if header else b""


def write_backup(path, store, board, log, extra_images=None):
    """board: {"fw", "wifi", "active_portal"} or None (PC only).
    extra_images: {name: bytes} for images that are not on this PC (read from the board)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        meta = {"format": BACKUP_FORMAT, "app": "ESP32 Toolbox / WiFi Portal", "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "board": board, "answers": max(0, len(log.splitlines()) - 1) if log else 0}
        z.writestr("backup.json", json.dumps(meta, ensure_ascii=False, indent=2))
        z.writestr("portals.json", json.dumps(dict(store_data(store), current=store["current"]),
                                              ensure_ascii=False, indent=2))
        for name in all_images(store):
            if os.path.exists(asset_path(name)):
                z.write(asset_path(name), "images/" + name)
            elif extra_images and name in extra_images:
                z.writestr("images/" + name, extra_images[name])
        if log:
            z.writestr("responses.csv", log)


def read_backup(path):
    """(meta, store, images {name: bytes}, log bytes); raises ValueError for an invalid file."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "backup.json" not in names or "portals.json" not in names:
                raise ValueError("This is not a Portal Admin backup.")
            meta = json.loads(z.read("backup.json"))
            if meta.get("format") != BACKUP_FORMAT:
                raise ValueError("This backup was made with another version of Portal Admin.")
            store = store_from(json.loads(z.read("portals.json")))
            images = {}
            for name in all_images(store):
                info = z.getinfo("images/" + name) if "images/" + name in names else None
                if info is None or info.file_size > pe.MAX_IMAGE_BYTES:
                    continue
                data = z.read(info)
                if pe.image_ext(data):
                    images[name] = data
            log = b""
            if "responses.csv" in names and z.getinfo("responses.csv").file_size <= MAX_BACKUP_LOG:
                log = z.read("responses.csv")
                if not log.startswith(b"survey_id,"):
                    log = b""
            return meta, store, images, log
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"The backup file is damaged ({exc}).") from exc


# =========================================================================== UI

class Module(core.BaseModule):
    def __init__(self, parent, host, module):
        super().__init__(parent, host, module)
        self.has_password = False
        self.had_local_file = os.path.exists(CONTENT_FILE)
        self.store = load_store()
        self.content = self._current_portal()   # the portal being edited (editor + Survey tab)
        self.board_portal = None                # id of the portal active on the board
        self.synced_ports = set()         # ports whose page was compared with this PC this session
        self.lock = threading.RLock()     # content is shared with the editor's web server
        self.rev = 1                      # bumped on every content change
        self.calls = queue.Queue()        # callables from other threads, run on the Tk thread
        self.editor = None
        self.closed = False

        self._build()
        self._load_editor()
        self.after(150, self._run_calls)

    @property
    def status_kind(self):
        return self.host.status_kind

    def _current_portal(self):
        return next(q for q in self.store["portals"] if q["id"] == self.store["current"])

    def _save(self):
        with self.lock:
            save_store(self.store)

    def _run_calls(self):
        if self.closed:
            return
        try:
            while True:
                self.calls.get_nowait()()
        except queue.Empty:
            pass
        self.after(150, self._run_calls)

    def close(self):
        self.closed = True
        try:
            self._collect()
        except Exception:  # noqa: BLE001
            pass
        if self.editor:
            self.editor.stop()
            self.editor = None

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
        # Portal (profile) being edited
        bar = tk.Frame(self, bg=CARD, padx=20, pady=12)
        bar.pack(fill="x", padx=16, pady=(0, 16))
        self._label(bar, "PORTAL", bold=True, color=PRIMARY, size=8).pack(fill="x")
        prow = tk.Frame(bar, bg=CARD)
        prow.pack(fill="x", pady=(4, 0))
        self.portal_var = tk.StringVar()
        self.portal_box = ttk.Combobox(prow, textvariable=self.portal_var, state="readonly", font=(FONT, 10))
        self.portal_box.pack(side="left", fill="x", expand=True)
        self.portal_box.bind("<<ComboboxSelected>>", lambda e: self.switch_portal(self.portal_box.current()))
        self._ghost(prow, "Rename", self.rename_portal, small=True).pack(side="left", padx=(8, 0))
        self._ghost(prow, "New", self.new_portal, small=True).pack(side="left", padx=(6, 0))
        self._ghost(prow, "Duplicate", self.duplicate_portal, small=True).pack(side="left", padx=(6, 0))
        self._ghost(prow, "Delete", self.delete_portal, fg=SECONDARY, small=True).pack(side="left", padx=(6, 0))
        arow = tk.Frame(bar, bg=CARD)
        arow.pack(fill="x", pady=(8, 0))
        self.portal_hint = self._label(arow, "", color=MUTED, size=9)
        self.portal_hint.pack(side="left", fill="x", expand=True)
        self.activate_btn = self._ghost(arow, "Make active on ESP32", self.activate_portal, small=True)
        self.activate_btn.pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.add_firmware_tab(nb)
        nb.add(self._build_wifi(nb), text="WiFi")
        nb.add(self._build_portal(nb), text="Portal")
        nb.add(self._build_survey(nb), text="Survey")
        nb.add(self._build_results(nb), text="Results")
        nb.add(self._build_backup(nb), text="Backup")
        self.action_buttons = [self.save_btn, self.upload_btn, self.survey_upload_btn, self.results_btn,
                               self.activate_btn, self.backup_btn, self.restore_btn]

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

        # network name of the portal chosen at the top (changes with the portal)
        self.pssid_title = self._label(tab, "Network name (SSID)")
        self.pssid_title.pack(fill="x", pady=(0, 4))
        self.pssid_var = tk.StringVar()
        self.pssid_entry = ttk.Entry(tab, textvariable=self.pssid_var, font=(FONT, 11))
        self.pssid_entry.pack(fill="x")
        self.pssid_entry.bind("<FocusOut>", lambda e: (self._collect(), self._update_wifi_labels()))
        self.pssid_entry.bind("<KeyRelease>", lambda e: self._update_wifi_labels())
        self.pssid_hint = self._label(tab, "", color=MUTED, size=9)
        self.pssid_hint.configure(justify="left", wraplength=420)
        self.pssid_hint.pack(fill="x", pady=(4, 0))
        # shown when another portal is active on the board
        self.ssid_note = self._label(tab, "", color=SECONDARY, size=9)
        self.ssid_note.configure(justify="left", wraplength=420)
        self.open_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(tab, text="Open network (no password)", variable=self.open_var,
                        command=self._toggle_pass).pack(anchor="w", pady=(12, 0))
        self._label(tab, "WiFi password").pack(fill="x", pady=(10, 4))
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(tab, textvariable=self.pass_var, show="•", font=(FONT, 11))
        self.pass_entry.pack(fill="x")
        self.pass_hint = self._label(tab, "", color=MUTED, size=9)
        self.pass_hint.pack(fill="x", pady=(4, 0))
        self._label(tab, "Default network name (for portals without their own name)").pack(
            fill="x", pady=(12, 4))
        self.ssid_var = tk.StringVar()
        self.ssid_entry = ttk.Entry(tab, textvariable=self.ssid_var, font=(FONT, 10))
        self.ssid_entry.pack(fill="x")
        self.ssid_entry.bind("<KeyRelease>", lambda e: self._update_wifi_labels())
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

        self.pname_var = tk.StringVar()
        self._label(tab, "Portal name (shown on the ESP32 display)").pack(fill="x", pady=(0, 4))
        name_entry = ttk.Entry(tab, textvariable=self.pname_var, font=(FONT, 10))
        name_entry.pack(fill="x")
        name_entry.bind("<FocusOut>", lambda e: self._collect())
        name_entry.bind("<Return>", lambda e: self._collect())
        self._label(tab, "Its network name is set in the WiFi tab.", color=MUTED, size=9).pack(
            fill="x", pady=(4, 14))

        box = tk.Frame(tab, bg=SURFACE2, padx=16, pady=14)
        box.pack(fill="x")
        self._label(box, "Landing page", bold=True, size=12, bg=SURFACE2).pack(fill="x")
        self.page_summary = self._label(box, "", color=MUTED, size=9, bg=SURFACE2)
        self.page_summary.pack(fill="x", pady=(2, 0))
        self.page_colors = tk.Frame(box, bg=SURFACE2)
        self.page_colors.pack(fill="x", pady=(10, 0))

        self._button(tab, "Open page editor", self.open_editor, PRIMARY, active=PRIMARY_LIGHT).pack(
            fill="x", pady=(14, 0))
        self._label(tab, "The editor opens in its own window: headings, formatted text, images, "
                         "galleries, highlights, colours and fonts, with a live preview. "
                         "Changes are saved automatically. Large photos are resized for you.",
                    color=MUTED, size=9).pack(fill="x", pady=(10, 0))
        tab.bind("<Configure>", lambda e: self._wrap_labels(tab, e.width))

        actions = tk.Frame(tab, bg=CARD)
        actions.pack(side="bottom", fill="x", pady=(16, 0))
        self.upload_btn = self._button(actions, "Upload all portals to ESP32", self.upload_portal,
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
        self.placeholder_var = tk.StringVar()
        self.open_frame = tk.Frame(self.options_frame, bg=CARD)
        self._label(self.open_frame, "Hint text in the empty answer field", size=9).pack(fill="x", pady=(0, 4))
        ttk.Entry(self.open_frame, textvariable=self.placeholder_var, font=(FONT, 10)).pack(fill="x")
        self._label(self.open_frame, f"Visitors type an answer of up to {OPEN_MAX} characters. The answers "
                                     "appear in the Results tab and in the CSV export.",
                    color=MUTED, size=9).pack(fill="x", pady=(6, 0))

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
        self.survey_upload_btn = self._button(actions, "Upload all portals to ESP32", self.upload_portal,
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
        for f in (self.stars_hint, self.yesno_frame, self.choice_frame, self.open_frame):
            f.pack_forget()
        t = self._survey_type_key()
        {"stars": self.stars_hint, "yesno": self.yesno_frame, "choice": self.choice_frame,
         "open": self.open_frame}[t].pack(fill="x")

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
        frow = tk.Frame(tab, bg=CARD)
        frow.pack(fill="x", pady=(0, 8))
        self._label(frow, "Show", size=9).pack(side="left")
        self.results_filter = tk.StringVar(value="All portals")
        self.filter_box = ttk.Combobox(frow, textvariable=self.results_filter, state="readonly", width=30)
        self.filter_box.pack(side="left", padx=(8, 0))
        self.filter_box.bind("<<ComboboxSelected>>", lambda e: self._show_results())

        table = tk.Frame(tab, bg=CARD)
        table.pack(fill="both", expand=True)
        cols = (("time", "Time", 120), ("portal", "Portal", 90), ("device", "Device", 100),
                ("question", "Question", 130), ("answer", "Answer", 80))
        self.tree = ttk.Treeview(table, columns=[c for c, _, _ in cols], show="headings", height=11)
        for key, title, width in cols:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, anchor="w", stretch=True)
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", self._show_answer)
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

    def _build_backup(self, parent):
        tab = tk.Frame(parent, bg=CARD, padx=20, pady=18)
        self._label(tab, "Backup", bold=True, size=12).pack(fill="x")
        self._label(tab, "One file with everything you need to set up the portal again, for example after "
                         "you installed another program on the ESP32:", color=MUTED, size=9).pack(fill="x", pady=(4, 8))
        for line in ("all portals: pages, images, surveys and thank-you pages",
                     "WiFi settings: network name, password, network on/off",
                     "which portal is active",
                     "all survey answers"):
            self._label(tab, "  \u2022  " + line, size=10).pack(fill="x", pady=1)
        self._label(tab, "Restoring puts everything back on this PC and on the connected ESP32 (first flash "
                         "this firmware again). Answers already on the ESP32 are kept; the answers from the "
                         "backup are added.", color=MUTED, size=9).pack(fill="x", pady=(12, 0))
        self._label(tab, "The backup contains the WiFi password and survey answers. Store it in a safe place, "
                         "not in a shared folder.", color=SECONDARY, size=9).pack(fill="x", pady=(10, 0))
        tab.bind("<Configure>", lambda e: [w.configure(wraplength=max(200, e.width - 50), justify="left")
                                           for w in tab.winfo_children() if isinstance(w, tk.Label)])

        actions = tk.Frame(tab, bg=CARD)
        actions.pack(side="bottom", fill="x", pady=(16, 0))
        self.backup_btn = self._button(actions, "Create backup…", self.create_backup, PRIMARY,
                                       active=PRIMARY_LIGHT)
        self.backup_btn.pack(fill="x")
        self.restore_btn = self._ghost(actions, "Restore backup…", self.restore_backup)
        self.restore_btn.pack(fill="x", pady=(8, 0))
        return tab

    # ------------------------------------------------------------ WiFi
    def _toggle_pass(self):
        if self.open_var.get():
            self.pass_entry.state(["disabled"])
            self.pass_hint.configure(text="Anyone can connect without a password (all portals).")
        else:
            self.pass_entry.state(["!disabled"])
            hint = "8–63 characters. The same password for all portals."
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
            self.no_port()
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
                          + self._battery_text(info)
                          + (f"  ·  WiFi power {info['txp']}%" if "txp" in info else ""))
        self.board_portal = info.get("portal") if info.get("portal") not in (None, "-") else None
        self.board_fw_portals = "portal" in info
        self.broadcast_ssid = from_hex(info["essid"]) if info.get("essid") else info["ssid"]
        self.board_default_ssid = info["ssid"]
        self._update_wifi_labels()
        self._refresh_portal_bar()

    def _portal_name(self, pid):
        return next((q["name"] for q in self.store["portals"] if q["id"] == pid), "?")

    def _update_wifi_labels(self):
        """WiFi tab: the network name of the portal chosen at the top, and what the board broadcasts."""
        c = self.content
        self.pssid_title.configure(text=f"Network name of “{c['name']}”")
        default = self.ssid_var.get().strip() or "?"
        own = self.pssid_var.get().strip()
        self.pssid_hint.configure(text=(f"Empty: this portal uses the default name “{default}” (below)."
                                        if not own else "Own name of this portal; empty = the default name."))
        broadcast = getattr(self, "broadcast_ssid", None)
        if broadcast and self.board_portal and self.board_portal != c["id"]:
            self.ssid_note.configure(text=f"Active on the ESP32 now: “{self._portal_name(self.board_portal)}”, "
                                          f"broadcasting “{broadcast}”. This name is used when "
                                          f"“{c['name']}” is active.")
            self.ssid_note.pack(after=self.pssid_hint, fill="x", pady=(4, 0))
        else:
            self.ssid_note.pack_forget()

    @staticmethod
    def _battery_text(info):
        bat = info.get("bat")
        if bat is None:
            return ""
        if bat == "-1":
            return "  ·  no battery"
        return f"  ·  battery {bat}%" + (" (charging)" if info.get("chg") == "1" else "")

    def connected(self, port, reply):
        """The Toolbox found this project on `port`: show its info and fetch its portals once."""
        info = parse_info(reply)
        self._show_info(info)
        if port in self.synced_ports:
            return
        self.synced_ports.add(port)

        def work(_r):
            with Board(port) as b:
                return b.get_file(BOARD_CONTENT)

        self.run_bg(work, lambda data: self._sync_from_board(port, data), "Reading the portals on the board…")

    def read_info(self):
        port = self.port()
        if not port:
            self.no_port()
            return

        def work(_r):
            with Board(port) as b:
                return parse_info(b.cmd("INFO"))

        def done(info):
            self._show_info(info)
            self.set_status(f"Connected to {port}", SUCCESS)

        self.run_bg(work, done, f"Reading {port}…")

    def _sync_from_board(self, port, data):
        """Use the portals stored on the board, unless this PC has newer changes (then ask)."""
        if not data:
            self.set_status(f"Connected to {port}. The board has no page settings from this app yet; "
                            "they are stored on the next upload.", SUCCESS)
            return
        try:
            board = store_from(json.loads(data.decode("utf-8")))
        except ValueError:
            self.set_status("Connected, but the page settings on the board are unreadable.", SECONDARY)
            return
        self._collect()
        with self.lock:
            local = copy.deepcopy(self.store)
        missing = [n for n in all_images(board) if pe.valid_file(n) and not os.path.exists(asset_path(n))]
        if store_hash(board) == store_hash(local) and not missing:
            with self.lock:
                self.store["synced_hash"] = store_hash(local)
                save_store(self.store)
            self.set_status(f"Connected to {port}. The portals on this PC match the board.", SUCCESS)
            return
        local_edited = (self.had_local_file and store_hash(local) != local.get("synced_hash")
                        and store_hash(local) != store_hash(store_from(None))
                        and store_hash(local) != store_hash(board))
        if local_edited and not messagebox.askyesno(
                "Portals on the board",
                "The board has different portals than this PC, and this PC has changes that "
                "were not uploaded.\n\n"
                "Yes: use the board's portals (the version on this PC is saved as a backup).\n"
                "No: keep the version on this PC (upload it to replace the board's portals)."):
            self.set_status(f"Connected to {port}. Kept the portals on this PC; they differ from the board.",
                            SECONDARY)
            return
        if self.had_local_file and store_hash(local) != store_hash(board) and os.path.exists(CONTENT_FILE):
            shutil.copyfile(CONTENT_FILE, os.path.join(HERE, "portal_content.backup.json"))

        def work(report):
            got = 0
            with Board(port) as b:
                for name in missing:
                    start = got
                    blob = b.get_file("/" + name, lambda n: report(start + n))
                    if not blob:
                        raise FileNotFoundError(f"Image {name} is missing on the board.")
                    if not pe.image_ext(blob):
                        raise ValueError(f"Image {name} on the board is damaged.")
                    os.makedirs(ASSETS_DIR, exist_ok=True)
                    with open(asset_path(name), "wb") as f:
                        f.write(blob)
                    got += len(blob)

        def done(_):
            with self.lock:
                ids = [q["id"] for q in board["portals"]]
                board["current"] = local["current"] if local["current"] in ids else ids[0]
                board["board_files"] = all_images(board)
                board["synced_hash"] = store_hash(board)
                self.store = board
                self.content = self._current_portal()
                self.rev += 1
                save_store(board)
            self.had_local_file = True
            self._load_editor()
            n = len(board["portals"])
            self.set_status(f"Connected to {port}. Downloaded {n} portal{'s' if n != 1 else ''} from the board"
                            + (f" ({len(missing)} image(s))." if missing else "."), SUCCESS)

        if missing:
            self.run_bg(work, done, f"Downloading {len(missing)} image(s) from the board…",
                        progress_total=max(1, 64 * 1024 * len(missing)))
        else:
            done(None)

    def _wait_for_restart(self, port):
        try:
            return parse_info(core.wait_for_info(port))
        except TimeoutError as exc:
            raise TimeoutError(f"{exc} Click Refresh.") from None

    def save_wifi(self):
        """Saves the portal's own network name (with an upload of the portals) and, when changed, the
        default name and password (SET, the board restarts)."""
        port = self.port()
        if not port:
            self.no_port()
            return
        own = self.pssid_var.get().strip()
        if len(own.encode("utf-8")) > 32:
            messagebox.showwarning("Portal Admin", "Network name must be at most 32 characters.")
            return
        ssid = self.ssid_var.get().strip()
        if not 1 <= len(ssid.encode("utf-8")) <= 32:
            messagebox.showwarning("Portal Admin", "Default network name must be 1 to 32 characters.")
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
        password_same = (pass_arg == "-" and not self.has_password) or (pass_arg == "=")
        need_set = ssid != getattr(self, "board_default_ssid", None) or not password_same
        self._collect()
        with self.lock:
            need_upload = store_hash(self.store) != self.store.get("synced_hash")

        def set_work(_r):
            reply = send_command(port, f"SET {to_hex(ssid)} {pass_arg}")
            if reply.startswith("ERR"):
                raise ValueError(reply[4:] or "Save failed")
            return self._wait_for_restart(port)

        def info_work(_r):
            with Board(port) as b:
                return parse_info(b.cmd("INFO"))

        def done(info):
            self._show_info(info)
            name = self.content["ssid"] or info["ssid"]
            if self.board_portal == self.content["id"]:
                self.set_status(f"Saved. The ESP32 broadcasts “{self.broadcast_ssid}”.", SUCCESS)
            else:
                self.set_status(f"Saved. “{self.content['name']}” uses “{name}” when it is active; the ESP32 "
                                f"broadcasts “{self.broadcast_ssid}” now.", SUCCESS)

        def board_step():
            self.run_bg(set_work if need_set else info_work, done,
                        "Saving and restarting…" if need_set else "Reading the board…")

        if need_upload:
            self.upload_portal(then=board_step)
        else:
            board_step()

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

    # ------------------------------------------------------------ portals
    def _refresh_portal_bar(self):
        names = []
        for q in self.store["portals"]:
            mark = "   ● active on ESP32" if q["id"] == self.board_portal else ""
            names.append(f"{q['name']}{mark}")
        self.portal_box["values"] = names
        idx = [q["id"] for q in self.store["portals"]].index(self.store["current"])
        self.portal_box.current(idx)
        n = len(self.store["portals"])
        on_board = next((q["name"] for q in self.store["portals"] if q["id"] == self.board_portal), None)
        self.portal_hint.configure(
            text=f"{n} portal{'s' if n != 1 else ''} (max. {MAX_PORTALS})  ·  "
                 + (f"active on the ESP32: {on_board}" if on_board else "not on the ESP32 yet: upload to add")
                 + "  ·  choose on the display with the PORTAL button")

    def switch_portal(self, index):
        if index < 0 or index >= len(self.store["portals"]):
            return
        self._collect()
        with self.lock:
            self.store["current"] = self.store["portals"][index]["id"]
            self.content = self._current_portal()
            self.rev += 1
            save_store(self.store)
        self._load_editor()

    def _add_portal(self, portal):
        if len(self.store["portals"]) >= MAX_PORTALS:
            messagebox.showinfo("Portal Admin", f"You can have at most {MAX_PORTALS} portals.")
            return
        self._collect()
        with self.lock:
            self.store["portals"].append(portal)
        self.switch_portal(len(self.store["portals"]) - 1)

    def new_portal(self):
        taken = {q["id"] for q in self.store["portals"]}
        self._add_portal(portal_from(None, taken, f"Portal {len(self.store['portals']) + 1}"))

    def duplicate_portal(self):
        self._collect()
        copy_ = copy.deepcopy(self.content)
        copy_["id"] = new_portal_id({q["id"] for q in self.store["portals"]})
        copy_["name"] = (copy_["name"] + " (copy)")[:40]
        self._add_portal(copy_)

    def rename_portal(self):
        self._collect()
        name = simpledialog.askstring("Rename portal", "New name for this portal (shown on the ESP32 display):",
                                      initialvalue=self.content["name"], parent=self)
        name = clean_line(name or "")[:40]
        if not name or name == self.content["name"]:
            return
        self.pname_var.set(name)
        self._collect()
        self.set_status(f"Renamed to “{name}”. Upload all portals to show the new name on the ESP32.",
                        SUCCESS)

    def delete_portal(self):
        if len(self.store["portals"]) == 1:
            messagebox.showinfo("Portal Admin", "This is the only portal; it cannot be deleted.")
            return
        if not messagebox.askyesno("Delete portal",
                                   f"Delete the portal “{self.content['name']}” from this PC?\n\n"
                                   "It is removed from the ESP32 on the next upload. Survey answers stay in "
                                   "the log."):
            return
        with self.lock:
            idx = self.store["portals"].index(self.content)
            self.store["portals"].pop(idx)
            self.store["current"] = self.store["portals"][max(0, idx - 1)]["id"]
            self.content = self._current_portal()
            self.rev += 1
            save_store(self.store)
        self._load_editor()

    def activate_portal(self):
        port = self.port()
        if not port:
            self.no_port()
            return
        pid, name = self.content["id"], self.content["name"]

        def work(_r):
            with Board(port) as b:
                reply = b.cmd(f"PORTAL {pid}", timeout=8)
                if reply.startswith("ERR"):
                    raise RuntimeError(f"“{name}” is not on the ESP32 yet. Upload all portals first."
                                       if "Unknown portal" in reply else reply[4:])
                return parse_info(b.cmd("INFO"))

        def done(info):
            self._show_info(info)
            self.set_status(f"“{name}” is now active on the ESP32.", SUCCESS)

        self.run_bg(work, done, f"Activating “{name}”…")

    # ------------------------------------------------------------ Portal-editor
    def _load_editor(self):
        c = self.content
        self.pname_var.set(c["name"])
        self.pssid_var.set(c["ssid"])
        self._update_wifi_labels()
        self._refresh_portal_bar()
        self._update_summary()
        self.survey_on_var.set(bool(c["survey_on"]))
        self.survey_type_var.set(dict(SURVEY_TYPES).get(c["survey_type"], SURVEY_TYPES[0][1]))
        self.survey_q_var.set(c["survey_question"])
        self.yes_var.set(c["survey_yes"])
        self.no_var.set(c["survey_no"])
        for v, val in zip(self.choice_vars, list(c["survey_choices"]) + [""] * 4):
            v.set(val)
        self.placeholder_var.set(c.get("survey_placeholder", ""))
        self.thanks_h_var.set(c["thanks_heading"])
        self.thanks_t_var.set(c["thanks_text"])
        self._show_option_fields()

    def _collect(self):
        """Copy the survey fields into the content, save it and return a snapshot."""
        with self.lock:
            c = self.content
            old = json.dumps(c, sort_keys=True)
            c["name"] = clean_line(self.pname_var.get())[:40] or c["name"]
            c["ssid"] = clean_ssid(self.pssid_var.get())
            c["survey_on"] = self.survey_on_var.get()
            c["survey_type"] = self._survey_type_key()
            c["survey_question"] = self.survey_q_var.get().strip()
            c["survey_yes"] = self.yes_var.get().strip()
            c["survey_no"] = self.no_var.get().strip()
            c["survey_choices"] = [v.get().strip() for v in self.choice_vars]
            c["survey_placeholder"] = self.placeholder_var.get().strip()
            c["thanks_heading"] = self.thanks_h_var.get().strip() or "Thank you!"
            c["thanks_text"] = self.thanks_t_var.get().strip()
            changed = json.dumps(c, sort_keys=True) != old
            if changed:
                self.rev += 1
            save_store(self.store)
            snapshot = copy.deepcopy(c)
        if changed:
            self._refresh_portal_bar()
        return snapshot

    def _update_summary(self):
        with self.lock:
            c = copy.deepcopy(self.content)
        blocks = c["blocks"]
        files = pe.used_files(blocks)
        size = sum(os.path.getsize(asset_path(n)) for n in files if os.path.exists(asset_path(n)))
        parts = [f"{len(blocks)} block{'s' if len(blocks) != 1 else ''}",
                 f"{len(files)} image{'s' if len(files) != 1 else ''}" + (f" ({size / 1024:.0f} kB)" if files else "")]
        if c.get("survey_on"):
            parts.append("survey on")
        heading = next((b["text"] for b in blocks if b["type"] == "heading" and b["text"].strip()), "")
        self.page_summary.configure(text=(f"“{heading}”   ·   " if heading else "") + "   ·   ".join(parts),
                                    wraplength=420, justify="left")
        for w in self.page_colors.winfo_children():
            w.destroy()
        for key, label in (("bg", "Background"), ("card", "Card"), ("fg", "Text"), ("accent", "Accent")):
            border = tk.Frame(self.page_colors, bg="#6A6A6A", padx=1, pady=1)
            border.pack(side="left")
            tk.Frame(border, bg=c[key], width=16, height=16).pack()
            self._label(self.page_colors, label, size=9, color=MUTED, bg=SURFACE2).pack(side="left", padx=(5, 12))

    # --- editor web server (these callbacks run on the server's threads)
    def _editor_content(self):
        with self.lock:
            return copy.deepcopy(self.content), self.rev

    def _editor_update(self, edit):
        with self.lock:
            self.content.update(pe.normalize_edit(edit, self.content))
            self.rev += 1
            save_store(self.store)
            rev = self.rev
        self.calls.put(self._update_summary)
        return rev

    def _editor_render(self):
        with self.lock:
            return render_html(copy.deepcopy(self.content))

    def _editor_status(self):
        with self.lock:
            rev = self.rev
        return {"status": getattr(self, "status_text", ""), "kind": self.status_kind,
                "busy": self.busy, "rev": rev}

    def open_editor(self):
        if self.editor is None:
            self._collect()
            try:
                self.editor = pe.EditorServer(ASSETS_DIR, self._editor_content, self._editor_update,
                                              self._editor_render, self._editor_status,
                                              lambda: self.calls.put(self.upload_portal))
            except OSError as exc:
                messagebox.showerror("Portal Admin", f"Cannot start the editor: {exc}")
                return
        self.editor.open_window()

    def reset_editor(self):
        if not messagebox.askyesno("Restore defaults",
                                   f"Reset the portal “{self.content['name']}” (all blocks, images, "
                                   "colours and its survey) to the default content?"):
            return
        with self.lock:
            fresh = copy.deepcopy(DEFAULT_CONTENT)
            for key in PORTAL_KEYS:
                fresh[key] = self.content[key]
            idx = self.store["portals"].index(self.content)
            self.store["portals"][idx] = fresh
            self.content = fresh
            self.rev += 1
        self._load_editor()
        self._collect()

    def preview(self):
        c = self._collect()
        folder = os.path.join(tempfile.gettempdir(), "esp32-portal-preview")
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, exist_ok=True)
        for name in pe.used_files(c["blocks"]):
            if os.path.exists(asset_path(name)):
                shutil.copyfile(asset_path(name), os.path.join(folder, name))
        out = os.path.join(folder, "index.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(render_html(c))
        with open(os.path.join(folder, "thanks.html"), "w", encoding="utf-8") as f:
            f.write(render_thanks(c))
        webbrowser.open("file:///" + out.replace("\\", "/"))

    def upload_portal(self, then=None):
        """Upload all portals; then() runs after a successful upload (used by restore)."""
        if self.busy:
            return
        port = self.port()
        if not port:
            self.no_port()
            return
        self._collect()
        with self.lock:
            store = copy.deepcopy(self.store)
        for c in store["portals"]:
            if c["survey_on"] and not c["survey_question"]:
                messagebox.showwarning("Portal Admin", f"Enter a survey question for “{c['name']}” "
                                                       "(Survey tab), or turn its survey off.")
                return
            if c["survey_on"] and c["survey_type"] != "open" and len(survey_options(c)) < 2:
                messagebox.showwarning("Portal Admin", f"Enter at least 2 answers for “{c['name']}” "
                                                       "(Survey tab).")
                return
        try:
            images = []
            for name in all_images(store):
                if not os.path.exists(asset_path(name)):
                    raise FileNotFoundError(f"Image {name} is missing from {ASSETS_DIR}. "
                                            "Choose it again in the editor.")
                with open(asset_path(name), "rb") as f:
                    images.append((name, f.read()))
        except FileNotFoundError as exc:
            messagebox.showwarning("Portal Admin", str(exc))
            return
        used = [n for n, _ in images]
        files = []                                  # (name, bytes) per portal
        for c in store["portals"]:
            pre = f"p-{c['id']}-"
            files.append((pre + "index.html", render_html(c).encode("utf-8")))
            files.append((pre + "thanks.html", render_thanks(c).encode("utf-8")))
            if c["survey_on"]:
                files.append((pre + "survey.cfg", survey_cfg(c).encode("utf-8")))
        cfg = portals_cfg(store).encode("utf-8")
        settings = json.dumps(store_data(store), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        keep = {n for n, _ in files} | set(used) | {"portals.cfg", "portal.json"}
        total = sum(len(d) for _, d in images + files) + len(cfg) + len(settings)

        def work(report):
            sent = 0
            with Board(port) as b:

                def put(name, data):
                    nonlocal sent
                    start = sent
                    b.put_file("/" + name, data, lambda n: report(start + n))
                    sent = start + len(data)

                if "portal" not in parse_info(b.cmd("INFO")):
                    raise RuntimeError("The ESP32 needs firmware 2.4 or newer for portals. "
                                       "Flash the firmware first (see README).")
                remote = b.list_files() or {}
                for name in sorted(remote):          # free space: images no longer used
                    if pe.is_image_name(name) and name not in keep:
                        b.ok(f"FDEL /{name}")
                for name, data in images:
                    if remote.get(name) == len(data):
                        sent += len(data)            # same content-hash name and size: already there
                        report(sent)
                        continue
                    put(name, data)
                for name, data in files:
                    put(name, data)
                put("portals.cfg", cfg)              # switches the board to the new portal files
                # files of deleted portals, surveys that were turned off, and the old single-portal files
                for name in sorted(remote):
                    old_portal_file = PORTAL_FILE_RE.match(name) and name not in keep
                    if old_portal_file or name in ("index.html", "thanks.html", "survey.cfg"):
                        b.ok(f"FDEL /{name}")
                put("portal.json", settings)
                return b.cmd("FS"), parse_info(b.cmd("INFO"))

        def done(result):
            fs, info = result
            with self.lock:
                self.store["board_files"] = used
                self.store["synced_hash"] = store_hash(store)
                save_store(self.store)
            self._show_info(info)
            used_kb = total_kb = "?"
            if fs.startswith("FS"):
                vals = dict(p.split("=") for p in fs.split()[1:])
                used_kb = int(vals.get("used", 0)) // 1024
                total_kb = int(vals.get("total", 0)) // 1024
            n = len(store["portals"])
            self.set_status(f"Uploaded {n} portal{'s' if n != 1 else ''} ({total / 1024:.0f} kB, "
                            f"{len(used)} image(s)). Storage: {used_kb} of {total_kb} kB in use.", SUCCESS)
            if then:
                then()

        self.run_bg(work, done, "Uploading portals…", progress_total=total)

    # ------------------------------------------------------------ Backup
    def create_backup(self):
        self._collect()
        port = self.port()
        if not port and not messagebox.askyesno(
                "Create backup", "No ESP32 is connected. The backup then only contains the portals on this "
                                 "PC, without the WiFi settings and survey answers.\n\nContinue?"):
            return
        missing = [n for n in all_images(self.store) if not os.path.exists(asset_path(n))]
        if missing and not messagebox.askyesno(
                "Create backup", f"{len(missing)} image(s) are missing on this PC and will not be in the "
                                 "backup. Continue?"):
            return
        path = filedialog.asksaveasfilename(
            title="Save backup", defaultextension=".zip", filetypes=[("Portal backup", "*.zip")],
            initialfile=f"portal-backup-{time.strftime('%Y-%m-%d')}.zip")
        if not path:
            return
        with self.lock:
            store = copy.deepcopy(self.store)

        def work(_r):
            board, log = None, b""
            if port:
                with Board(port) as b:
                    info = parse_info(b.cmd("INFO"))
                    wifi = b.wifi()
                    log = b.get_file(SURVEY_LOG)
                board = {"fw": info.get("fw"), "wifi": wifi,
                         "active_portal": info.get("portal") if info.get("portal") not in (None, "-") else None}
            write_backup(path, store, board, log)
            return board, log

        def done(result):
            board, log = result
            answers = max(0, len(log.splitlines()) - 1) if log else 0
            extra = ""
            if board and not board["wifi"]:
                extra = " WiFi settings not included: flash firmware 2.5 or newer for that."
            self.set_status(f"Backup saved: {os.path.basename(path)} ({len(store['portals'])} portal(s), "
                            f"{answers} answer(s)).{extra}", SUCCESS if not extra else SECONDARY)

        self.run_bg(work, done, "Creating backup…")

    def restore_backup(self):
        path = filedialog.askopenfilename(title="Restore backup", filetypes=[("Portal backup", "*.zip")])
        if path:
            self._restore_path(path)

    def _restore_path(self, path, confirm=True):
        try:
            meta, store, images, log = read_backup(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Restore backup", str(exc))
            return
        board = meta.get("board") or {}
        wifi = board.get("wifi")
        port = self.port()
        answers = max(0, len(log.splitlines()) - 1) if log else 0
        lines = [f"Backup from {meta.get('created', '?')}:",
                 f"  \u2022  {len(store['portals'])} portal(s): " + ", ".join(q["name"] for q in store["portals"]),
                 f"  \u2022  {len(images)} image(s), {answers} survey answer(s)"]
        if wifi:
            lines.append(f"  \u2022  WiFi: \u201c{wifi['ssid']}\u201d, "
                         + ("open network" if not wifi["pass"] else "with password"))
        lines.append("")
        lines.append("This replaces the portals on this PC (the current ones are saved as a backup file).")
        if port:
            lines.append(f"Everything is then put on the ESP32 on {port}"
                         + (" and it restarts with the WiFi settings from the backup." if wifi else "."))
        else:
            lines.append("No ESP32 is connected: only this PC is restored. Connect the ESP32 and restore "
                         "again to put everything on it.")
        if confirm and not messagebox.askyesno("Restore backup", "\n".join(lines) + "\n\nRestore now?"):
            return

        # 1. this PC
        if os.path.exists(CONTENT_FILE):
            shutil.copyfile(CONTENT_FILE, os.path.join(HERE, "portal_content.backup.json"))
        os.makedirs(ASSETS_DIR, exist_ok=True)
        for name, data in images.items():
            with open(asset_path(name), "wb") as f:
                f.write(data)
        with self.lock:
            store.pop("synced_hash", None)
            self.store = store
            self.content = self._current_portal()
            self.rev += 1
            save_store(store)
        self.had_local_file = True
        self._load_editor()
        if not port:
            self.set_status("Backup restored on this PC. Connect the ESP32 and restore again to put it on "
                            "the board.", SUCCESS)
            return

        # 2. the ESP32: portals first, then answers, active portal and WiFi
        def board_work(_r):
            with Board(port) as b:
                current = b.get_file(SURVEY_LOG)
                merged = merge_logs(log, current) if log else current
                if merged and merged != current:
                    b.put_file(SURVEY_LOG, merged)
                active = board.get("active_portal")
                ids = [q["id"] for q in store["portals"]]
                b.ok(f"PORTAL {active if active in ids else ids[0]}", timeout=8)   # also recounts the votes
                restart = False
                if wifi:
                    now = b.wifi()
                    if now is not None and not wifi["ap"] and now["ap"]:
                        b.ok("AP 0", timeout=5)
                    if now is not None and wifi["ap"] and not now["ap"]:
                        b.ok("AP 1", timeout=8)
                    if now is None or (now["ssid"], now["pass"]) != (wifi["ssid"], wifi["pass"]):
                        pass_arg = to_hex(wifi["pass"]) if wifi["pass"] else "-"
                        reply = b.cmd(f"SET {to_hex(wifi['ssid'])} {pass_arg}")
                        if reply.startswith("ERR"):
                            raise RuntimeError("WiFi settings not restored: " + reply[4:])
                        restart = True
            info = self._wait_for_restart(port) if restart else parse_info(send_command(port, "INFO"))
            return info, merged

        def board_done(result):
            info, merged = result
            self.synced_ports.add(port)
            self._show_info(info)
            n = max(0, len(merged.splitlines()) - 1) if merged else 0
            self.set_status(f"Backup restored on this PC and the ESP32 ({len(store['portals'])} portal(s), "
                            f"{n} answer(s) on the board).", SUCCESS)

        self.upload_portal(then=lambda: self.run_bg(board_work, board_done, "Restoring answers and WiFi…"))

    # ------------------------------------------------------------ Toolbox: switching projects
    def save_state(self):
        self._collect()

    def restore_backup_file(self, port, path):
        self._restore_path(path)

    @staticmethod
    def backup_board(port, path):
        """Backup of what is on the board (portals, images, WiFi, answers) before another project
        is installed. Uses the board's own portal settings; images missing on this PC are read
        from the board. Runs in a thread, without the UI."""
        local = load_store()
        with Board(port) as b:
            info = parse_info(b.cmd("INFO"))
            wifi = b.wifi()
            log = b.get_file(SURVEY_LOG)
            data = b.get_file(BOARD_CONTENT)
            store = local
            if data:
                try:
                    store = store_from(json.loads(data.decode("utf-8")))
                    store["current"] = local["current"] if local["current"] in \
                        [q["id"] for q in store["portals"]] else store["portals"][0]["id"]
                except ValueError:
                    store = local
            extra = {}
            for name in all_images(store):
                if not os.path.exists(asset_path(name)) and pe.valid_file(name):
                    blob = b.get_file("/" + name)
                    if blob:
                        extra[name] = blob
        board = {"fw": info.get("fw"), "wifi": wifi,
                 "active_portal": info.get("portal") if info.get("portal") not in (None, "-") else None}
        write_backup(path, store, board, log, extra)

    def restore_after_install(self, port, reply, path):
        """The portal was installed again. Flashing keeps the files and settings on the board when
        the other project used the same partition table, so usually everything is still there.
        When the portals are gone, or the WiFi settings differ from the backup (another project
        overwrote the settings area), the last backup is put back."""
        try:
            meta = read_backup(path)[0]
            wanted = (meta.get("board") or {}).get("wifi")
        except (OSError, ValueError):
            wanted = None

        def work(_r):
            with Board(port) as b:
                return b.get_file(BOARD_CONTENT), b.wifi()

        def done(result):
            data, wifi = result
            wifi_lost = bool(wanted and wifi and (wifi["ssid"], wifi["pass"]) != (wanted["ssid"], wanted["pass"]))
            if data and not wifi_lost:
                self.set_status("WiFi Portal installed; the portals and settings were still on the board.",
                                SUCCESS)
                self.connected(port, reply)
                return
            self._show_info(parse_info(reply))
            why = "The portals are gone from the board" if not data else "The WiFi settings were changed"
            self.set_status(f"{why}: restoring the last backup…", PRIMARY)
            self._restore_path(path, confirm=False)

        self.run_bg(work, done, "Checking the portals on the board…")

    # ------------------------------------------------------------ Results
    def load_results(self):
        port = self.port()
        if not port:
            self.no_port()
            return

        def work(_r):
            with Board(port) as b:
                return b.get_file(SURVEY_LOG)

        def done(data):
            self.log_raw = data
            self.log_rows = list(csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace"))))
            self._show_results()
            self.set_status(f"Loaded {len(self.log_rows)} answer(s) from the ESP32.", SUCCESS)

        self.run_bg(work, done, "Reading results…")

    def _portal_of(self, row):
        """Portal name for a log row (survey ids start with the portal id)."""
        pid = (row.get("survey_id") or "").split("-")[0]
        return next((q["name"] for q in self.store["portals"] if q["id"] == pid),
                    "(deleted portal)" if "-" in (row.get("survey_id") or "") else "(before portals)")

    def _show_results(self):
        names = ["All portals"] + [q["name"] for q in self.store["portals"]]
        self.filter_box["values"] = names
        if self.results_filter.get() not in names:
            self.results_filter.set("All portals")
        chosen = self.results_filter.get()
        rows = [r for r in self.log_rows if chosen == "All portals" or self._portal_of(r) == chosen]
        self.tree.delete(*self.tree.get_children())
        for r in reversed(rows):
            self.tree.insert("", "end", values=(r.get("time", ""), self._portal_of(r), r.get("device", ""),
                                                r.get("question", ""), r.get("answer", "")))
        portal = next((q for q in self.store["portals"] if q["name"] == chosen), self.content)
        self._summarise(rows, portal)

    def _show_answer(self, _event=None):
        item = self.tree.focus()
        if item:
            t, portal, device, question, answer = self.tree.item(item, "values")
            messagebox.showinfo("Answer", f"{answer}\n\n{t}  ·  {portal}  ·  {device}\n{question}")

    def _summarise(self, rows, portal=None):
        if not rows:
            self.results_summary.configure(text="No answers yet.")
            self.results_detail.configure(text="")
            return
        portal = portal or self.content
        sid = survey_id(portal)
        current = [r for r in rows if r.get("survey_id") == sid] or \
                  [r for r in rows if r.get("survey_id") == rows[-1].get("survey_id")]
        question = current[-1].get("question", "")
        answers = [r.get("answer", "") for r in current]
        if current[-1].get("answer_index") == "0":       # open question
            summary = f"{len(answers)} answer{'s' if len(answers) != 1 else ''}"
            dist = "Double-click a row to read the full answer."
        elif all(a in "12345" and a for a in answers) and portal.get("survey_type") == "stars":
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
        extra = f"   ·   {older} other answer(s) in this view" if older else ""
        self.results_detail.configure(text=f"{self._portal_of(current[-1])}:  \u201c{question}\u201d   "
                                           f"{dist}{extra}", wraplength=440,
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
        fields = ["portal"] + list(self.log_rows[0].keys()) if self.log_rows else ["portal"]
        with open(path, "w", encoding="utf-8-sig", newline="") as f:   # BOM: Excel opens UTF-8 correctly
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in self.log_rows:
                w.writerow(dict(r, portal=self._portal_of(r)))
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
            self._show_results()
            self.set_status("Survey log cleared.", SUCCESS)

        self.run_bg(work, done, "Clearing log…")


