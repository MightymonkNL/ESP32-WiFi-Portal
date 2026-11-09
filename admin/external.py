"""External projects: firmware from a website or GitHub, added with a link.

Most ESP32 projects with a browser installer publish an ESP Web Tools manifest
(manifest.json: name, version and per chip the .bin parts with their flash offsets).
The Toolbox finds that manifest from a link (the manifest itself, the project's website
or installer page, or its GitHub repository), downloads the parts into
admin/external/<id>/firmware and writes a module.json with "kind": "external".

External firmware does not speak BoardLink, so:
- the Toolbox remembers per board (MAC address) which project it installed;
- settings are backed up with esptool: the board's partition table is read and its
  settings partitions (NVS and other small data partitions) are saved;
- when the project is installed again, those partitions are written back together with
  the firmware (only when the partition table still has them at the same place).
No code from an external project ever runs on this PC: its screen is ExternalModule.
"""

import json
import os
import re
import shutil
import tempfile
import time
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from html.parser import HTMLParser
from tkinter import messagebox, ttk

import espcore as core
from espcore import CARD, MUTED, PRIMARY, SECONDARY, SUCCESS, TEXT, FONT

UA = {"User-Agent": "ESP32-Toolbox/1.0 (+ESP Web Tools manifest import)"}
MAX_PAGE = 3 * 1024 * 1024
MAX_FIRMWARE = 16 * 1024 * 1024          # all parts together: the board has 16 MB flash
MAX_TRIES = 30
CHIPS = {"ESP32": "esp32", "ESP32-S2": "esp32s2", "ESP32-S3": "esp32s3", "ESP32-C2": "esp32c2",
         "ESP32-C3": "esp32c3", "ESP32-C5": "esp32c5", "ESP32-C6": "esp32c6", "ESP32-H2": "esp32h2",
         "ESP32-P4": "esp32p4", "ESP8266": "esp8266"}
TOOLBOX_CHIP = "ESP32-S3"


# ============================================================================ finding a manifest

def _fetch(url, limit=MAX_PAGE):
    if not url.lower().startswith("https://"):
        raise ValueError("Only https links are used.")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"{url} is too large.")
        return data, r.geturl(), r.headers.get("Content-Type", "")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.manifests, self.links = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("manifest"):                       # <esp-web-install-button manifest="...">
            self.manifests.append(a["manifest"])
        for key in ("href", "src", "data-manifest"):
            if a.get(key):
                self.links.append(a[key])


GITHUB_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
GITHUB_PAGES = re.compile(r"^https://([A-Za-z0-9-]+)\.github\.io/([A-Za-z0-9_.-]+)")


def _github_repo(url):
    m = GITHUB_REPO.match(url)
    if m and m.group(1) not in ("orgs", "sponsors", "features", "topics", "about", "pricing"):
        return m.group(1), re.sub(r"\.git$", "", m.group(2))
    m = GITHUB_PAGES.match(url)
    if m:
        return m.group(1), m.group(2)
    return None


def _github_info(owner, repo):
    try:
        data, _, _ = _fetch(f"https://api.github.com/repos/{owner}/{repo}")
        j = json.loads(data)
    except Exception:  # noqa: BLE001  (rate limit, offline: info is optional)
        return {"repo": f"https://github.com/{owner}/{repo}"}
    lic = j.get("license") or {}
    name = lic.get("name") if lic.get("spdx_id") not in (None, "NOASSERTION") else None
    if not name and lic:
        name = _license_title(owner, repo)
    return {"repo": j.get("html_url") or f"https://github.com/{owner}/{repo}",
            "homepage": j.get("homepage") or "", "description": j.get("description") or "",
            "license": name or "see the LICENSE file in the repository"}


def _license_title(owner, repo):
    """Name of a license GitHub does not recognise, from the LICENSE file (e.g. a "# Title" line)."""
    try:
        data, _, _ = _fetch(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/LICENSE", 64 * 1024)
    except Exception:  # noqa: BLE001
        return None
    for line in data.decode("utf-8", errors="replace").splitlines()[:40]:
        m = re.match(r"^\s*#+\s*(.*license.*)$", line, re.I) or             re.match(r"^\s*((?:PolyForm|Creative Commons|MIT|Apache|GNU|BSD|Mozilla)[^.]{0,60}License[^.]{0,20})", line)
        if m:
            return m.group(1).strip()
    return None


def _github_candidates(owner, repo, info):
    out = [f"https://{owner}.github.io/{repo}/manifest.json",
           f"https://{owner}.github.io/{repo}/install/manifest.json"]
    if info.get("homepage"):
        out.append(info["homepage"])
    try:
        data, _, _ = _fetch(f"https://api.github.com/repos/{owner}/{repo}/releases/latest")
        for asset in json.loads(data).get("assets", []):
            if asset.get("name", "").lower().endswith(".json") and "manifest" in asset["name"].lower():
                out.append(asset["browser_download_url"])
    except Exception:  # noqa: BLE001
        pass
    return out


def find_manifest(url, log=None):
    """Follows a link to an ESP Web Tools manifest.
    Returns {"manifest", "manifest_url", "url", "repo", "license", "description", "homepage"}."""
    url = url.strip()
    if not re.match(r"^[a-z]+://", url, re.I):
        url = "https://" + url
    if url.lower().startswith("http://"):
        url = "https://" + url[7:]
    queue, seen, info = [(url, 0)], set(), {}
    while queue and len(seen) < MAX_TRIES:
        u, depth = queue.pop(0)
        u = u.split("#")[0]
        if u in seen or not u.lower().startswith("https://"):
            continue
        seen.add(u)
        if log:
            log(u)
        repo = _github_repo(u)
        if repo and not info.get("repo"):
            info.update(_github_info(*repo))
            for c in _github_candidates(*repo, info):
                queue.insert(0, (c, depth + 1))
            if GITHUB_REPO.match(u):
                continue                         # the repository page itself has no manifest
        try:
            data, final, ctype = _fetch(u)
        except Exception:  # noqa: BLE001
            continue
        text = data.decode("utf-8", errors="replace")
        if text.lstrip().startswith("{"):
            try:
                j = json.loads(text)
            except ValueError:
                j = None
            if isinstance(j, dict) and isinstance(j.get("builds"), list):
                if not info.get("repo"):
                    repo = _github_repo(final)
                    if repo:
                        info.update(_github_info(*repo))
                return dict(info, manifest=j, manifest_url=final, url=url)
            continue
        if depth >= 2:
            continue
        p = _Links()
        try:
            p.feed(text)
        except Exception:  # noqa: BLE001
            continue
        found = [urllib.parse.urljoin(final, m) for m in p.manifests]
        for link in p.links:
            full = urllib.parse.urljoin(final, link)
            low = full.lower()
            if low.endswith(".json") and "manifest" in low:
                found.append(full)
            elif _github_repo(full) and GITHUB_REPO.match(full) and full.count("/") <= 4:
                found.append(full)
            elif "install" in low and not low.endswith((".js", ".css", ".png", ".jpg", ".svg", ".ico")):
                found.append(full)
        for f in found:
            queue.append((f, depth + 1))
    raise ValueError("No installer (ESP Web Tools manifest.json) found for this link. Try the link of the "
                     "project's install page, its GitHub repository or its manifest.json.")


def choose_build(manifest, chip=TOOLBOX_CHIP):
    for build in manifest.get("builds", []):
        if str(build.get("chipFamily", "")).upper() == chip:
            return build
    families = ", ".join(str(b.get("chipFamily")) for b in manifest.get("builds", [])) or "none"
    raise ValueError(f"This project has no firmware for the {chip} (it has: {families}).")


def describe(found):
    """Summary lines for the confirmation in the Add project dialog."""
    m = found["manifest"]
    build = choose_build(m)
    parts = build.get("parts", [])
    lines = [f"{m.get('name', '?')}  ·  {m.get('version', '?')}"]
    if m.get("board_name") or m.get("board"):
        lines.append(f"Made for: {m.get('board_name') or m.get('board')}")
    lines.append(f"{len(parts)} firmware part(s) for the {build.get('chipFamily')}")
    lines.append(f"Installer: {found['manifest_url']}")
    if found.get("repo"):
        lines.append(f"Source: {found['repo']}")
    if found.get("license"):
        lines.append(f"License: {found['license']}")
    return lines


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:32] or "project"


def download(found, report=None, module_id=None):
    """Downloads the parts and writes the module folder; returns its path."""
    m = found["manifest"]
    build = choose_build(m)
    parts = build.get("parts", [])
    if not parts:
        raise ValueError("The manifest lists no firmware parts.")
    flash, blobs, total = {}, [], 0
    used = set()
    for i, part in enumerate(parts):
        url = urllib.parse.urljoin(found["manifest_url"], part["path"])
        offset = int(part["offset"])
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(urllib.parse.urlparse(url).path)) or "part.bin"
        if not name.endswith(".bin"):
            name += ".bin"
        if name in used:
            name = f"{i}-{name}"
        used.add(name)
        data, _, _ = _fetch(url, MAX_FIRMWARE)
        total += len(data)
        if total > MAX_FIRMWARE:
            raise ValueError("The firmware is larger than the 16 MB flash of the board.")
        blobs.append((name, data))
        flash[hex(offset)] = name
        if report:
            report(total)
    module_id = module_id or _slug(m.get("name") or found["manifest_url"])
    os.makedirs(core.EXTERNAL_DIR, exist_ok=True)
    folder = os.path.join(core.EXTERNAL_DIR, module_id)
    tmp = folder + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "firmware"))
    for name, data in blobs:
        with open(os.path.join(tmp, "firmware", name), "wb") as f:
            f.write(data)
    with open(os.path.join(tmp, "firmware", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
    meta = {
        "id": module_id,
        "name": m.get("name") or module_id,
        "description": found.get("description") or f"External project from {urllib.parse.urlparse(found['url']).netloc}.",
        "version": str(m.get("version") or "?"),
        "chip": CHIPS.get(str(build.get("chipFamily", "")).upper(), "esp32s3"),
        "kind": "external",
        "source": {"url": found["url"], "manifest": found["manifest_url"], "repo": found.get("repo", ""),
                   "license": found.get("license", ""), "board": m.get("board_name") or m.get("board") or "",
                   "added": time.strftime("%Y-%m-%d")},
        "flash": flash,
    }
    with open(os.path.join(tmp, "module.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    if os.path.exists(folder):
        shutil.rmtree(folder)
    os.replace(tmp, folder)
    return folder


def free_id(name, modules, source_manifest):
    """Module id for a new external project (reuses the id when it is the same source)."""
    base = _slug(name)
    for m in modules:
        if m.id == base and m.source.get("manifest") == source_manifest:
            return base
    taken = {m.id for m in modules}
    mid, n = base, 2
    while mid in taken:
        mid, n = f"{base}-{n}", n + 1
    return mid


# ============================================================================ settings backup (esptool)

def backup_flash(port, chip, path, after="hard-reset"):
    """Saves the settings partitions of the firmware on the board (read from its partition table)."""
    tmp = tempfile.mkdtemp(prefix="toolbox-read-")
    try:
        table_file = os.path.join(tmp, "partitions.bin")
        mac = core.read_flash(port, chip, 0x8000, 0xC00, table_file, after="no-reset")
        with open(table_file, "rb") as f:
            table = f.read()
        regions = core.settings_partitions(core.parse_partitions(table))
        if not regions:
            raise RuntimeError("No settings partitions found on the board.")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for i, p in enumerate(regions):
                out = os.path.join(tmp, f"{p['label']}.bin")
                core.read_flash(port, chip, p["offset"], p["size"], out,
                                after=after if i == len(regions) - 1 else "no-reset")
                z.write(out, f"regions/{p['label']}.bin")
            z.writestr("partitions.bin", table)
            z.writestr("toolbox-backup.json", json.dumps(
                {"format": 1, "kind": "flash", "created": time.strftime("%Y-%m-%d %H:%M:%S"), "mac": mac,
                 "regions": regions}, indent=2))
        return mac, regions
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def restore_parts(module, path):
    """[(offset, file)] to write with the firmware: the settings partitions from a backup whose
    label, offset and size are the same in the module's partition table. Also returns the labels."""
    if not path:
        return [], []
    table_name = module.flash.get("0x8000")
    if not table_name:
        return [], []
    with open(os.path.join(module.folder, "firmware", table_name), "rb") as f:
        new = {(p["label"], p["offset"], p["size"]) for p in core.parse_partitions(f.read())}
    out, labels = [], []
    tmp = tempfile.mkdtemp(prefix="toolbox-restore-")
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("toolbox-backup.json"))
        if meta.get("kind") != "flash":
            return [], []
        for p in meta.get("regions", []):
            if (p["label"], p["offset"], p["size"]) not in new:
                continue
            data = z.read(f"regions/{p['label']}.bin")
            if len(data) != p["size"]:
                continue
            f = os.path.join(tmp, f"{p['label']}.bin")
            with open(f, "wb") as fh:
                fh.write(data)
            out.append((p["offset"], f))
            labels.append(p["label"])
    return out, labels


# ============================================================================ project screen

class ExternalModule(core.BaseModule):
    """Screen of an external project: the Toolbox's Firmware tab and an About tab."""

    def __init__(self, parent, host, module):
        super().__init__(parent, host, module)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.add_firmware_tab(nb)
        nb.add(self._build_about(nb), text="About")

    def _build_about(self, parent):
        m, src = self.module, self.module.source
        tab = tk.Frame(parent, bg=CARD, padx=20, pady=18)
        core.label(tab, m.name, bold=True, size=12).pack(fill="x")
        desc = core.label(tab, m.description, color=MUTED, size=9)
        desc.configure(wraplength=440, justify="left")
        desc.pack(fill="x", pady=(2, 12))
        grid = tk.Frame(tab, bg=CARD)
        grid.pack(fill="x")
        rows = [("Version", m.version), ("Made for", src.get("board") or "—"),
                ("License", src.get("license") or "—"), ("Added", src.get("added") or "—")]
        for r, (k, v) in enumerate(rows):
            core.label(grid, k, color=MUTED, size=9).grid(row=r, column=0, sticky="nw", pady=2)
            lbl = tk.Label(grid, text=v, bg=CARD, fg=TEXT, font=(FONT, 10), anchor="w", justify="left",
                           wraplength=330)
            lbl.grid(row=r, column=1, sticky="w", padx=(16, 0), pady=2)
        links = tk.Frame(tab, bg=CARD)
        links.pack(fill="x", pady=(10, 0))
        for text, url in (("Website", src.get("url")), ("Source code", src.get("repo")),
                          ("Installer manifest", src.get("manifest"))):
            if url:
                a = tk.Label(links, text=f"{text} ↗", bg=CARD, fg=PRIMARY, font=(FONT, 9, "underline"),
                             cursor="hand2")
                a.pack(side="left", padx=(0, 16))
                a.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
        note = core.label(tab, "External project: the Toolbox cannot talk to its firmware. The ESP32 shows it as "
                               "running when the Toolbox installed it there last. Before another project is "
                               "installed, its settings (NVS) are saved with esptool and they are put back when "
                               "you install it again. Its firmware is downloaded from the links above; the "
                               "license of the project applies.", color=MUTED, size=9)
        note.configure(wraplength=440, justify="left")
        note.pack(fill="x", pady=(14, 0))

        actions = tk.Frame(tab, bg=CARD)
        actions.pack(side="bottom", fill="x", pady=(16, 0))
        self.update_btn = core.button(actions, "Check for update", self.check_update)
        self.update_btn.pack(fill="x")
        self.remove_btn = core.ghost(actions, "Remove project from the Toolbox", self.remove, fg=SECONDARY)
        self.remove_btn.pack(fill="x", pady=(8, 0))
        return tab

    @staticmethod
    def backup_board(port, path):
        backup_flash(port, "esp32s3", path)

    def check_update(self):
        if self.busy:
            return
        m = self.module

        def work(_r):
            return find_manifest(m.source.get("manifest") or m.source.get("url"))

        def done(found):
            new = str(found["manifest"].get("version") or "?")
            if new == m.version:
                self.set_status(f"{m.name} is up to date ({m.version}).", SUCCESS)
                return
            if not messagebox.askyesno("Update available",
                                       f"{m.name} {new} is available (the Toolbox has {m.version}).\n\n"
                                       "Download it now? Install it afterwards in the Firmware tab."):
                return
            found["url"] = m.source.get("url") or found["url"]
            self.run_bg(lambda r: download(found, r, m.id),
                        lambda _f: self.host.reload_project(m.id, f"Downloaded {m.name} {new}. Install it in the "
                                                                  "Firmware tab."),
                        f"Downloading {m.name} {new}…", progress_total=4 * 1024 * 1024)

        self.run_bg(work, done, "Checking for an update…")

    def remove(self):
        m = self.module
        if not messagebox.askyesno("Remove project",
                                   f"Remove {m.name} from the Toolbox? Its downloaded firmware is deleted from "
                                   "this PC. The ESP32 itself and the backups are not changed."):
            return
        self.host.remove_project(m)
