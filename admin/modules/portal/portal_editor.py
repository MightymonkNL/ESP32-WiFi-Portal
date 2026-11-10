"""Block-based page editor for the Portal Admin app.

Runs a small web server on 127.0.0.1 (random port, random token) and serves a
single-page editor that opens in its own window (Edge app mode) or the default
browser. The editor edits the 'blocks' and style fields of the portal content;
the Tk app owns the content and uploads it to the board.
"""

import hashlib
import html
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import uuid
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# --------------------------------------------------------------------------- content model

BLOCK_TYPES = ("heading", "text", "image", "gallery", "callout", "divider", "spacer")
EDIT_KEYS = ("title", "footer", "bg", "card", "fg", "accent", "font", "layout", "width", "blocks")
FONTS = {
    "sans": '-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif',
    "serif": 'Georgia,Cambria,"Times New Roman",Times,serif',
    "rounded": 'ui-rounded,"SF Pro Rounded","Segoe UI Variable Display","Segoe UI",Roboto,Arial,sans-serif',
}
WIDTHS = {"narrow": 460, "normal": 560, "wide": 760}
TONES = {"orange": "#FF612B", "green": "#38A84A"}   # "accent" uses the page accent colour

MAX_IMAGE_BYTES = 2 * 1024 * 1024        # largest image stored for the board
MAX_UPLOAD_BYTES = 20 * 1024 * 1024      # largest file accepted from the editor (GIFs are shrunk)
# Animated GIFs are stored as they are when small; larger ones are scaled down (all frames kept)
GIF_KEEP_BYTES = 600 * 1024              # smaller and not wider than GIF_KEEP_SIDE: unchanged
GIF_KEEP_SIDE = 640
GIF_SIDES = (480, 400, 320, 240, 180)    # tried in this order until the GIF fits GIF_TARGET_BYTES
GIF_TARGET_BYTES = 700 * 1024
MAX_GALLERY = 12
FILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}\.(png|jpg|gif|webp)$")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def is_image_name(name):
    return name.lower().endswith(IMAGE_EXT)


def valid_file(name):
    return isinstance(name, str) and bool(FILE_RE.match(name))


def image_ext(data):
    """File extension from the image's magic bytes, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def gif_info(data):
    """(width, height, frames) of a GIF, from its header and frame markers (no Pillow needed)."""
    w = int.from_bytes(data[6:8], "little")
    h = int.from_bytes(data[8:10], "little")
    frames = max(1, data.count(b"\x00\x2c"))        # end of a block + image descriptor (estimate)
    return w, h, frames


def _gif_frames(im, side):
    from PIL import Image, ImageSequence
    w, h = im.size
    scale = min(1.0, side / max(w, h))
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    frames, durations = [], []
    for frame in ImageSequence.Iterator(im):
        durations.append(frame.info.get("duration", im.info.get("duration", 100)) or 100)
        frames.append(frame.convert("RGBA").resize(size, Image.LANCZOS))
    return frames, durations


def shrink_gif(data):
    """Scales an animated GIF down until it fits on the board; keeps all frames and timing.
    Returns (bytes, note) — note is a message for the editor, or ""."""
    w, h, _ = gif_info(data)
    if len(data) <= GIF_KEEP_BYTES and max(w, h) <= GIF_KEEP_SIDE:
        return data, ""
    try:
        from PIL import Image
    except ImportError:
        if len(data) <= MAX_IMAGE_BYTES:
            return data, ""
        raise ValueError(f"This GIF is {len(data) // 1024} kB; the maximum is {MAX_IMAGE_BYTES // 1024} kB.")
    try:
        im = Image.open(io.BytesIO(data))
        loop = im.info.get("loop", 0)
        best = None
        for side in GIF_SIDES:
            if side >= max(w, h) and best is not None:
                continue
            frames, durations = _gif_frames(im, side)
            buf = io.BytesIO()
            frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=durations,
                           loop=loop, disposal=2, optimize=True)
            out = buf.getvalue()
            best = (out, frames[0].size, len(frames))
            if len(out) <= GIF_TARGET_BYTES:
                break
    except Exception as exc:  # noqa: BLE001  (damaged or unusual GIF)
        if len(data) <= MAX_IMAGE_BYTES:
            return data, ""
        raise ValueError(f"This GIF could not be made smaller ({exc}).") from exc
    out, size, n = best
    if len(out) >= len(data):
        if len(data) <= MAX_IMAGE_BYTES:
            return data, ""
    if len(out) > MAX_IMAGE_BYTES:
        raise ValueError(f"This GIF is still {len(out) // 1024} kB at {size[0]}×{size[1]} px "
                         f"({n} frames). Use a shorter GIF.")
    return out, (f"Animated GIF resized from {w}×{h} to {size[0]}×{size[1]} px "
                 f"({len(data) // 1024} → {len(out) // 1024} kB, {n} frames).")


def store_image(assets_dir, data):
    """Save image bytes under a content-hash name; returns (name, stored bytes, note)."""
    ext = image_ext(data)
    if not ext:
        raise ValueError("Unsupported image (use PNG, JPG, GIF or WebP).")
    note = ""
    if ext == "gif":
        data, note = shrink_gif(data)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image is larger than {MAX_IMAGE_BYTES // 1024} kB.")
    name = f"img-{hashlib.sha1(data).hexdigest()[:10]}.{ext}"
    os.makedirs(assets_dir, exist_ok=True)
    path = os.path.join(assets_dir, name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return name, data, note


def used_files(blocks):
    """Image files referenced by the blocks, in page order, without duplicates."""
    out = []
    for b in blocks:
        names = [b.get("file", "")] if b.get("type") == "image" else b.get("files", []) \
            if b.get("type") == "gallery" else []
        for n in names:
            if n and n not in out:
                out.append(n)
    return out


def _str(value, limit):
    return str(value if value is not None else "")[:limit]


def _pick(value, options, default):
    return value if value in options else default


def normalize_blocks(blocks):
    out = []
    for b in blocks if isinstance(blocks, list) else []:
        if not isinstance(b, dict) or b.get("type") not in BLOCK_TYPES:
            continue
        t = b["type"]
        nb = {"type": t, "id": _str(b.get("id"), 16) or uuid.uuid4().hex[:8]}
        if t == "heading":
            nb.update(text=_str(b.get("text"), 300), size=_pick(b.get("size"), ("l", "m", "s"), "m"),
                      align=_pick(b.get("align"), ("left", "center"), "left"))
        elif t == "text":
            nb.update(html=sanitize_html(_str(b.get("html"), 20000)),
                      align=_pick(b.get("align"), ("left", "center"), "left"))
        elif t == "image":
            f = b.get("file", "")
            nb.update(file=f if valid_file(f) else "", caption=_str(b.get("caption"), 300),
                      alt=_str(b.get("alt"), 200), width=_pick(b.get("width"), (100, 75, 50), 100))
        elif t == "gallery":
            files = [f for f in b.get("files", []) if valid_file(f)] if isinstance(b.get("files"), list) else []
            nb.update(files=files[:MAX_GALLERY], cols=_pick(b.get("cols"), (2, 3), 2))
        elif t == "callout":
            nb.update(text=_str(b.get("text"), 2000), tone=_pick(b.get("tone"), ("accent", "orange", "green"), "accent"))
        elif t == "spacer":
            nb.update(size=_pick(b.get("size"), ("s", "m", "l"), "m"))
        out.append(nb)
    return out


def normalize_edit(src, base):
    """Validated copy of the editable fields in src; invalid values fall back to base."""
    out = {}
    for key in ("title", "footer"):
        out[key] = _str(src.get(key, base.get(key, "")), 200)
    for key in ("bg", "card", "fg", "accent"):
        v = src.get(key)
        out[key] = v.upper() if isinstance(v, str) and HEX_RE.match(v) else base.get(key, "#000000")
    out["font"] = _pick(src.get("font"), tuple(FONTS), "sans")
    out["layout"] = _pick(src.get("layout"), ("card", "flat"), "card")
    out["width"] = _pick(src.get("width"), tuple(WIDTHS), "normal")
    out["blocks"] = normalize_blocks(src.get("blocks", []))
    return out


def migrate(content):
    """Convert the old single image + heading + text layout to blocks (in place)."""
    if isinstance(content.get("blocks"), list):
        return content
    blocks = []
    if content.get("image"):
        name = content["image"]
        if valid_file(name):
            blocks.append({"type": "image", "file": name, "width": 100})
    if str(content.get("heading", "")).strip():
        blocks.append({"type": "heading", "text": content["heading"], "size": "l"})
    paras = [p.strip() for p in str(content.get("text", "")).split("\n\n") if p.strip()]
    if paras:
        blocks.append({"type": "text", "html": "".join(
            f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)})
    content["blocks"] = normalize_blocks(blocks)
    return content


# --------------------------------------------------------------------------- rich text

class _Sanitizer(HTMLParser):
    ALLOWED = {"b", "strong", "i", "em", "u", "br", "p", "ul", "ol", "li", "a"}
    RENAME = {"div": "p"}
    DROP = {"script", "style", "template", "iframe", "object"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP:
            self.skip += 1
            return
        tag = self.RENAME.get(tag, tag)
        if self.skip or tag not in self.ALLOWED:
            return
        if tag == "br":
            self.out.append("<br>")
            return
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if re.match(r"^(https?:|mailto:|tel:|#|/)", href, re.I):
                self.out.append(f'<a href="{html.escape(href)}">')
            else:
                self.out.append("<a>")
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.DROP:
            self.skip = max(0, self.skip - 1)
            return
        tag = self.RENAME.get(tag, tag)
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        self.close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(text):
    s = _Sanitizer()
    s.feed(text)
    return s.result()


# --------------------------------------------------------------------------- rendering

def _rgba(hex_color, alpha):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def blocks_css(c):
    acc = c["accent"]
    return (
        ".blk{margin:0 0 16px}.blk:last-child{margin-bottom:0}"
        f"h2{{margin:0 0 10px;font-size:22px;color:{acc}}}h3{{margin:0 0 8px;font-size:18px;color:{acc}}}"
        ".al-c{text-align:center}"
        ".fig{margin:0 auto 16px}.fig img{display:block;width:100%;height:auto;border-radius:12px}"
        ".w75{width:75%}.w50{width:50%}"
        ".fig figcaption{font-size:13px;opacity:.7;text-align:center;margin-top:6px}"
        ".gal{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.gal.c3{grid-template-columns:repeat(3,1fr)}"
        ".gal img{display:block;width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:10px}"
        ".rt ul,.rt ol{margin:0 0 12px;padding-left:22px}"
        f".rt a{{color:{acc}}}"
        ".callout{padding:14px 16px;border-radius:12px}"
        ".divider{border:0;border-top:1px solid rgba(127,127,127,.3);margin:22px 0}"
        ".sp-s{height:4px}.sp-m{height:20px}.sp-l{height:44px}"
    )


def render_blocks(c):
    esc = html.escape
    parts = []
    for b in c.get("blocks", []):
        t = b["type"]
        al = " al-c" if b.get("align") == "center" else ""
        if t == "heading" and b["text"].strip():
            tag = {"l": "h1", "m": "h2", "s": "h3"}[b["size"]]
            parts.append(f'<{tag} class="blk{al}">{esc(b["text"])}</{tag}>')
        elif t == "text" and b["html"].strip():
            parts.append(f'<div class="blk rt{al}">{b["html"]}</div>')
        elif t == "image" and b["file"]:
            w = {100: "", 75: " w75", 50: " w50"}[b["width"]]
            cap = f"<figcaption>{esc(b['caption'])}</figcaption>" if b["caption"].strip() else ""
            parts.append(f'<figure class="blk fig{w}"><img src="{esc(b["file"])}" alt="{esc(b["alt"])}">{cap}</figure>')
        elif t == "gallery" and b["files"]:
            imgs = "".join(f'<img src="{esc(f)}" alt="" loading="lazy">' for f in b["files"])
            parts.append(f'<div class="blk gal{" c3" if b["cols"] == 3 else ""}">{imgs}</div>')
        elif t == "callout" and b["text"].strip():
            col = c["accent"] if b["tone"] == "accent" else TONES[b["tone"]]
            body = esc(b["text"]).replace("\n", "<br>")
            parts.append(f'<div class="blk callout" style="background:{_rgba(col, .14)};'
                         f'border-left:4px solid {col}">{body}</div>')
        elif t == "divider":
            parts.append('<hr class="divider">')
        elif t == "spacer":
            parts.append(f'<div class="sp-{b["size"]}"></div>')
    return "\n".join(parts)


# --------------------------------------------------------------------------- web server

class EditorServer:
    """Local HTTP server for the editor. All callbacks are thread-safe on the app side."""

    def __init__(self, assets_dir, get_content, update_content, render, get_status, start_upload):
        self.assets_dir = assets_dir
        self.get_content = get_content          # () -> (content copy, rev)
        self.update_content = update_content    # (edit dict) -> rev
        self.render = render                    # () -> html of the current page
        self.get_status = get_status            # () -> dict
        self.start_upload = start_upload        # () -> None
        self.token = secrets.token_urlsafe(18)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.editor = self
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def sizes(self, content):
        out = {}
        for name in used_files(content.get("blocks", [])):
            p = os.path.join(self.assets_dir, name)
            if os.path.exists(p):
                out[name] = os.path.getsize(p)
        return out

    def open_window(self):
        open_app_window(self.url)

    def stop(self):
        """Stops the server (when the module is closed); an open editor window then shows offline."""
        threading.Thread(target=lambda: (self.httpd.shutdown(), self.httpd.server_close()), daemon=True).start()


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.exists(p):
                return p
    return shutil.which("msedge")


def open_app_window(url):
    """Open url in a chromeless Edge window; fall back to the default browser."""
    edge = find_edge()
    if edge:
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen([edge, f"--app={url}", "--window-size=1380,900"], creationflags=flags)
            return
        except OSError:
            pass
    webbrowser.open(url)


class _Handler(BaseHTTPRequestHandler):
    server_version = "PortalEditor"

    def log_message(self, *args):   # keep quiet (no console in the exe)
        pass

    @property
    def ed(self):
        return self.server.editor

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        # Blocks DNS-rebinding: only accept requests addressed to our loopback port.
        return self.headers.get("Host") in (f"127.0.0.1:{self.ed.port}", f"localhost:{self.ed.port}")

    def _token_ok(self, query_token=None):
        given = self.headers.get("X-Token") or query_token or ""
        return secrets.compare_digest(given, self.ed.token)

    def _body(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Request too large")
        return self.rfile.read(n)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        path = u.path
        if path == "/":
            q = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
            if not self._token_ok(q.get("t")):
                return self._send(403, "Open the editor from the Portal Admin app.", "text/plain; charset=utf-8")
            return self._send(200, EDITOR_HTML, "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; "
                                                          "style-src 'self' 'unsafe-inline'; "
                                                          "script-src 'unsafe-inline'; frame-src 'self' about:"})
        # Images for the editor thumbnails and the preview (read-only, loopback only)
        name = path.lstrip("/")
        if valid_file(name):
            p = os.path.join(self.ed.assets_dir, name)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    data = f.read()
                ctype = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}[
                    name.rsplit(".", 1)[1]]
                return self._send(200, data, ctype)
            return self._send(404, {"error": "not found"})
        if not self._token_ok():
            return self._send(403, {"error": "forbidden"})
        if path == "/api/content":
            content, rev = self.ed.get_content()
            edit = {k: content.get(k) for k in EDIT_KEYS}
            return self._send(200, {"content": edit, "rev": rev, "sizes": self.ed.sizes(content),
                                    "portal": content.get("name", ""),
                                    "survey": bool(content.get("survey_on"))})
        if path == "/api/render":
            return self._send(200, self.ed.render(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, self.ed.get_status())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or not self._token_ok():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            if path == "/api/content":
                data = json.loads(self._body(4 * 1024 * 1024) or b"{}")
                rev = self.ed.update_content(data.get("content") or {})
                content, _ = self.ed.get_content()
                return self._send(200, {"rev": rev, "sizes": self.ed.sizes(content)})
            if path == "/api/image":
                data = self._body(MAX_UPLOAD_BYTES + 1)
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ValueError(f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
                name, stored, note = store_image(self.ed.assets_dir, data)
                return self._send(200, {"name": name, "size": len(stored), "note": note})
            if path == "/api/upload":
                self.ed.start_upload()
                return self._send(200, {"ok": True})
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(404, {"error": "not found"})


# --------------------------------------------------------------------------- editor page

EDITOR_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portal editor</title>
<style>
:root{--bg:#121212;--s1:#1E1E1E;--s2:#2C2C2C;--s3:#383838;--line:#3D3D3D;--t:#E1E1E1;--m:#9E9E9E;
--p:#9C89D7;--p2:#B6A8E5;--p-dim:rgba(156,137,215,.16);--o:#FF9E71;--g:#5DBB6A}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--t);font:14px/1.45 "Segoe UI",system-ui,sans-serif;display:flex;flex-direction:column;overflow:hidden}
button,input,select{font:inherit;color:inherit}
header{display:flex;align-items:center;gap:16px;padding:12px 20px;background:var(--s2)}
.brand small{display:block;font-size:10px;font-weight:700;letter-spacing:.08em;color:var(--p)}
.brand b{font-size:17px}
.state{font-size:12px;color:var(--m);margin-left:4px}
.pname{background:var(--p-dim);color:var(--p);font-weight:700;font-size:12px;padding:5px 11px;border-radius:999px}
.pname:empty{display:none}
.state.dirty{color:var(--o)}
.grow{flex:1}
.seg{display:inline-flex;background:var(--s1);border-radius:10px;padding:3px;gap:2px}
.seg button{border:0;background:none;padding:6px 12px;border-radius:8px;cursor:pointer;color:var(--m);font-weight:600;font-size:12px}
.seg button.on{background:var(--s3);color:var(--t)}
.btn{border:0;border-radius:10px;padding:9px 16px;font-weight:700;cursor:pointer;background:var(--s3);color:var(--p)}
.btn:hover{background:#444}
.btn.primary{background:var(--p);color:#121212}.btn.primary:hover{background:var(--p2)}
.btn:disabled{opacity:.45;cursor:default}
.btn.sm{padding:6px 11px;font-size:12px;border-radius:8px}
.btn.warn{color:var(--o)}
main{flex:1;display:flex;min-height:0}
aside{width:420px;flex:none;display:flex;flex-direction:column;background:var(--s1);border-right:1px solid #262626}
.tabs{display:flex;padding:10px 16px 0;gap:4px;border-bottom:1px solid var(--line)}
.tabs button{border:0;background:none;padding:9px 14px;cursor:pointer;color:var(--m);font-weight:700;border-bottom:2px solid transparent}
.tabs button.on{color:var(--p);border-color:var(--p)}
.pane{flex:1;overflow:auto;padding:16px}
.pane[hidden]{display:none}
.card{background:var(--s2);border-radius:12px;margin-bottom:8px;border:1px solid transparent;transition:border-color .15s}
.card.open{border-color:var(--p)}
.card.drop-before{box-shadow:0 -3px 0 var(--p)}.card.drop-after{box-shadow:0 3px 0 var(--p)}
.card.dragging{opacity:.4}
.head{display:flex;align-items:center;gap:8px;padding:9px 10px;cursor:pointer;user-select:none}
.grip{cursor:grab;color:var(--m);padding:2px 4px;letter-spacing:-2px;font-weight:700}
.ico{width:26px;height:26px;border-radius:7px;background:var(--p-dim);color:var(--p);display:grid;place-items:center;font-weight:800;font-size:13px;flex:none}
.what{flex:1;min-width:0}
.what b{display:block;font-size:12px;color:var(--m);font-weight:600}
.what span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tools{display:flex;gap:2px;opacity:.55}.head:hover .tools,.card.open .tools{opacity:1}
.tools button{border:0;background:none;width:26px;height:26px;border-radius:6px;cursor:pointer;color:var(--m);font-size:13px}
.tools button:hover{background:var(--s3);color:var(--t)}
.tools button.del:hover{color:var(--o)}
.body{padding:2px 12px 14px;display:none}
.card.open .body{display:block}
label.f{display:block;font-size:12px;color:var(--m);font-weight:600;margin:10px 0 5px}
input[type=text],textarea,select{width:100%;background:var(--s1);border:1px solid var(--line);border-radius:8px;padding:8px 10px;outline:none}
textarea{resize:vertical;min-height:70px}
input[type=text]:focus,textarea:focus,select:focus,.rte:focus{border-color:var(--p)}
.row{display:flex;gap:10px;align-items:flex-end}.row>*{flex:1}
.bar{display:flex;gap:2px;margin-bottom:6px;flex-wrap:wrap}
.bar button{border:0;background:var(--s1);border-radius:6px;min-width:30px;height:28px;cursor:pointer;color:var(--t)}
.bar button:hover{background:var(--s3)}
.rte{min-height:90px;max-height:320px;overflow:auto;background:var(--s1);border:1px solid var(--line);border-radius:8px;padding:8px 10px;outline:none}
.rte p{margin:0 0 8px}.rte p:last-child{margin:0}.rte a{color:var(--p)}
.thumb{display:block;width:100%;max-height:180px;object-fit:contain;background:var(--s1);border-radius:8px}
.drop{border:1.5px dashed var(--line);border-radius:10px;padding:18px;text-align:center;color:var(--m);cursor:pointer}
.drop:hover,.drop.over{border-color:var(--p);color:var(--p)}
.gal{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:8px}
.gal div{position:relative}
.gal img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px;display:block}
.gal button{position:absolute;top:3px;right:3px;border:0;width:20px;height:20px;border-radius:50%;background:rgba(0,0,0,.7);color:#fff;cursor:pointer;font-size:11px;line-height:1}
.gal .mv{right:auto;left:3px}
.add h4{margin:18px 0 8px;font-size:12px;color:var(--m)}
.chips{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.chip{border:1px solid var(--line);background:var(--s2);border-radius:10px;padding:10px 4px;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:4px;font-size:12px;font-weight:600}
.chip:hover{border-color:var(--p);color:var(--p)}
.chip .ico{width:24px;height:24px}
.hint{font-size:12px;color:var(--m);margin:8px 0 0}
.empty{color:var(--m);text-align:center;padding:30px 10px;border:1.5px dashed var(--line);border-radius:12px}
.colors{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.color{display:flex;align-items:center;gap:8px;background:var(--s2);border-radius:8px;padding:6px 8px}
.color input{width:34px;height:26px;border:0;padding:0;background:none;cursor:pointer}
.presets{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.preset{border:1px solid var(--line);border-radius:10px;padding:6px;cursor:pointer;background:var(--s2);font-size:11px;font-weight:600;text-align:center}
.preset:hover{border-color:var(--p)}
.preset i{display:block;height:28px;border-radius:6px;margin-bottom:5px;position:relative}
.preset i:after{content:"";position:absolute;left:8px;right:8px;top:7px;bottom:7px;border-radius:3px;background:var(--c)}
section.stage{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;padding:22px;overflow:auto;overflow-x:hidden;background:#0B0B0B}
.frame{background:#000;border-radius:34px;padding:10px;box-shadow:0 10px 40px rgba(0,0,0,.6);border:1px solid #2A2A2A;transition:width .2s}
.frame.phone{width:410px;height:min(820px,calc(100vh - 150px))}
.frame.desktop{width:100%;height:calc(100vh - 150px);border-radius:12px;padding:0}
iframe{width:100%;height:100%;border:0;border-radius:26px;background:#fff}
.frame.desktop iframe{border-radius:12px}
footer{display:flex;gap:16px;align-items:center;padding:8px 20px;background:var(--s1);border-top:1px solid #262626;font-size:12px;color:var(--m)}
.dot{width:9px;height:9px;border-radius:50%;background:#5A5A5A;display:inline-block;margin-right:7px}
.dot.ok{background:var(--g)}.dot.err{background:var(--o)}.dot.busy{background:var(--p)}
.toast{position:fixed;left:50%;bottom:50px;transform:translateX(-50%);background:var(--s3);padding:10px 16px;border-radius:10px;box-shadow:0 6px 20px rgba(0,0,0,.5);display:none}
</style>
</head>
<body>
<header>
  <div class="brand"><small>ESP32-PORTAL</small><b>Page editor</b></div>
  <span class="pname" id="pname"></span>
  <span class="state" id="state">Loading…</span>
  <div class="grow"></div>
  <div class="seg" id="view"><button data-v="phone" class="on">Phone</button><button data-v="desktop">Desktop</button></div>
  <button class="btn primary" id="upload">Upload to ESP32</button>
</header>
<main>
  <aside>
    <div class="tabs"><button data-p="content" class="on">Content</button><button data-p="style">Style</button></div>
    <div class="pane" id="p-content">
      <div id="blocks"></div>
      <div class="add">
        <h4>ADD BLOCK</h4>
        <div class="chips" id="chips"></div>
        <p class="hint" id="survey-hint"></p>
      </div>
    </div>
    <div class="pane" id="p-style" hidden></div>
  </aside>
  <section class="stage"><div class="frame phone" id="frame"><iframe id="pv" sandbox="allow-same-origin" title="Preview"></iframe></div></section>
</main>
<footer><span><span class="dot" id="dot"></span><span id="status">…</span></span><span class="grow"></span><span id="storage"></span></footer>
<div class="toast" id="toast"></div>
<script>
"use strict";
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const TYPES = {
  heading:{label:"Heading", ico:"H", make:()=>({text:"New heading", size:"m", align:"left"})},
  text:{label:"Text", ico:"¶", make:()=>({html:"<p>New paragraph</p>", align:"left"})},
  image:{label:"Image", ico:"▣", make:()=>({file:"", caption:"", alt:"", width:100})},
  gallery:{label:"Gallery", ico:"▦", make:()=>({files:[], cols:2})},
  callout:{label:"Highlight", ico:"!", make:()=>({text:"Important information", tone:"accent"})},
  divider:{label:"Divider", ico:"—", make:()=>({})},
  spacer:{label:"Space", ico:"↕", make:()=>({size:"m"})},
};
const PRESETS = [
  {name:"Dark", bg:"#121212", card:"#1E1E1E", fg:"#E1E1E1", accent:"#9C89D7"},
  {name:"Light", bg:"#F4F4F4", card:"#FFFFFF", fg:"#333333", accent:"#6B56A4"},
  {name:"Purple", bg:"#6B56A4", card:"#FFFFFF", fg:"#333333", accent:"#6B56A4"},
  {name:"Fresh", bg:"#EEF7F0", card:"#FFFFFF", fg:"#333333", accent:"#1D6F34"},
];
const MAX_GALLERY = 12, MAX_SIDE = 1280;

let doc = null, rev = 0, sizes = {}, openId = null;
let saveTimer = null, saving = false, dirty = false;
const $ = s => document.querySelector(s);

function el(tag, props, ...kids){
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})){
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "html") e.innerHTML = v;
    else e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid);
  return e;
}
async function api(path, opts = {}){
  opts.headers = Object.assign({"X-Token": TOKEN}, opts.headers || {});
  const r = await fetch(path, opts);
  if (!r.ok){ let m = r.statusText; try { m = (await r.json()).error || m; } catch(e){} throw new Error(m); }
  return r;
}
function toast(msg, ms = 2600){
  const t = $("#toast"); t.textContent = msg; t.style.display = "block";
  clearTimeout(toast.h); if (ms) toast.h = setTimeout(() => t.style.display = "none", ms);
}
const uid = () => Math.random().toString(16).slice(2, 10);
const kb = n => (n / 1024).toFixed(0) + " kB";

// ------------------------------------------------------------------ load / save
async function load(){
  const j = await (await api("/api/content")).json();
  doc = j.content; rev = j.rev; sizes = j.sizes;
  $("#pname").textContent = j.portal || ""; document.title = (j.portal ? j.portal + " – " : "") + "Portal editor";
  doc.blocks.forEach(b => b.id = b.id || uid());
  $("#survey-hint").textContent = j.survey
    ? "The survey (Survey tab in Portal Admin) is shown below your content."
    : "Tip: add a survey in the Survey tab of Portal Admin.";
  renderBlocks(); renderStyle(); refreshPreview(); setState("Saved");
}
function setState(text, isDirty){ const s = $("#state"); s.textContent = text; s.classList.toggle("dirty", !!isDirty); }
function changed(structural){
  if (structural) renderBlocks();
  dirty = true; setState("Unsaved changes", true);
  clearTimeout(saveTimer); saveTimer = setTimeout(save, 450);
}
async function save(){
  clearTimeout(saveTimer); saveTimer = null;
  if (saving){ saveTimer = setTimeout(save, 200); return; }
  if (!dirty) return;
  saving = true; dirty = false; setState("Saving…");
  try {
    const j = await (await api("/api/content", {method:"POST", headers:{"Content-Type":"application/json"},
                                                 body: JSON.stringify({content: doc})})).json();
    rev = j.rev; sizes = j.sizes; updateStorage();
    setState(dirty ? "Unsaved changes" : "Saved", dirty);
    refreshPreview();
  } catch (e){ dirty = true; setState("Not saved: " + e.message, true); }
  saving = false;
}
async function refreshPreview(){
  try {
    const html = await (await api("/api/render")).text();
    const f = $("#pv");
    let y = 0; try { y = f.contentWindow.scrollY; } catch(e){}
    f.onload = () => { try { f.contentWindow.scrollTo(0, y); } catch(e){} };
    f.srcdoc = html;
  } catch(e){}
}
function updateStorage(){
  const files = Object.keys(sizes), total = files.reduce((a, n) => a + sizes[n], 0);
  $("#storage").textContent = files.length ? `${files.length} image${files.length > 1 ? "s" : ""} · ${kb(total)} of about 3 MB` : "No images";
}

// ------------------------------------------------------------------ images
function pickFiles(multiple){
  return new Promise(res => {
    const i = el("input", {type:"file", accept:"image/png,image/jpeg,image/gif,image/webp"});
    i.multiple = multiple; i.onchange = () => res([...i.files]); i.click();
  });
}
function toBlob(canvas, type, q){ return new Promise(r => canvas.toBlob(r, type, q)); }
// Gallery images: fit within GALLERY_SIDE × GALLERY_SIDE and keep the smallest encoding
// (PNG first; JPEG only when it is smaller and the image has no transparency).
const GALLERY_SIDE = 300;
async function prepare(file, small){
  if (file.type === "image/gif") return file;           // keep animation
  let bmp; try { bmp = await createImageBitmap(file); } catch(e){ throw new Error(`${file.name} is not a readable image`); }
  const scale = Math.min(1, (small ? GALLERY_SIDE : MAX_SIDE) / Math.max(bmp.width, bmp.height));
  if (!small && scale === 1 && file.size < 250 * 1024 && file.type !== "image/webp") return file;
  const w = Math.max(1, Math.round(bmp.width * scale)), h = Math.max(1, Math.round(bmp.height * scale));
  const c = el("canvas"); c.width = w; c.height = h;
  const ctx = c.getContext("2d"); ctx.imageSmoothingQuality = "high"; ctx.drawImage(bmp, 0, 0, w, h);
  let alpha = false;
  if (file.type === "image/png" || file.type === "image/webp"){
    const d = ctx.getImageData(0, 0, w, h).data;
    for (let i = 3; i < d.length; i += 16) if (d[i] < 250){ alpha = true; break; }
  }
  let out;
  if (small){
    out = await toBlob(c, "image/png");
    if (!alpha){ const j = await toBlob(c, "image/jpeg", .8); if (j.size < out.size) out = j; }
  } else {
    out = alpha ? await toBlob(c, "image/png") : await toBlob(c, "image/jpeg", .84);
  }
  return (scale === 1 && out.size >= file.size && file.type !== "image/webp") ? file : out;
}
async function uploadFiles(files, small){
  const names = [], notes = [];
  for (const [i, f] of files.entries()){
    toast(`Processing image ${i + 1} of ${files.length}…`, 0);
    try {
      const blob = await prepare(f, small);
      const j = await (await api("/api/image", {method:"POST", headers:{"Content-Type": blob.type || "application/octet-stream"}, body: blob})).json();
      if (j.error) throw new Error(j.error);
      sizes[j.name] = j.size; names.push(j.name);
      if (j.note) notes.push(j.note);
    } catch (e){ toast(e.message, 6000); return names; }
  }
  if (notes.length) toast(notes.join(" "), 6000);
  else toast(files.length ? `${names.length} image${names.length > 1 ? "s" : ""} added` : "", 1600);
  return names;
}
function dropZone(text, multiple, onFiles){
  const z = el("div", {class:"drop"}, text);
  z.onclick = async () => onFiles(await pickFiles(multiple));
  z.ondragover = e => { if ([...e.dataTransfer.types].includes("Files")){ e.preventDefault(); z.classList.add("over"); } };
  z.ondragleave = () => z.classList.remove("over");
  z.ondrop = e => { e.preventDefault(); z.classList.remove("over");
    const fs = [...e.dataTransfer.files].filter(f => f.type.startsWith("image/")); if (fs.length) onFiles(multiple ? fs : fs.slice(0, 1)); };
  return z;
}

// ------------------------------------------------------------------ block list
function summary(b){
  switch (b.type){
    case "heading": return b.text || "(empty)";
    case "text": { const d = el("div", {html: b.html}); return d.textContent.trim() || "(empty)"; }
    case "image": return b.file ? (b.caption || "Image") : "No image chosen";
    case "gallery": return `${b.files.length} image${b.files.length === 1 ? "" : "s"}`;
    case "callout": return b.text || "(empty)";
    case "divider": return "Line";
    case "spacer": return {s:"Small", m:"Medium", l:"Large"}[b.size] + " space";
  }
  return "";
}
let dragFrom = -1;
function renderBlocks(){
  const box = $("#blocks"); box.innerHTML = "";
  if (!doc.blocks.length) box.append(el("div", {class:"empty"}, "The page is empty. Add a block below."));
  doc.blocks.forEach((b, i) => {
    const card = el("div", {class: "card" + (b.id === openId ? " open" : ""), "data-i": i});
    const grip = el("span", {class:"grip", title:"Drag to move"}, "⋮⋮");
    grip.onmousedown = () => card.draggable = true;
    const sum = el("span", {}, summary(b));
    const tool = (txt, title, fn, cls) => el("button", {title, class: cls || "", onclick: e => { e.stopPropagation(); fn(); }}, txt);
    const head = el("div", {class:"head", onclick: () => { openId = openId === b.id ? null : b.id; renderBlocks(); }},
      grip, el("span", {class:"ico"}, TYPES[b.type].ico),
      el("div", {class:"what"}, el("b", {}, TYPES[b.type].label), sum),
      el("div", {class:"tools"},
        tool("↑", "Move up", () => move(i, i - 1)),
        tool("↓", "Move down", () => move(i, i + 2)),
        tool("⧉", "Duplicate", () => { const c = JSON.parse(JSON.stringify(b)); c.id = uid(); doc.blocks.splice(i + 1, 0, c); openId = c.id; changed(true); }),
        tool("✕", "Delete", () => { doc.blocks.splice(i, 1); changed(true); }, "del")));
    card.append(head, el("div", {class:"body"}, ...(b.id === openId ? editor(b, sum) : [])));
    card.ondragstart = e => { dragFrom = i; card.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(i)); };
    card.ondragend = () => { card.draggable = false; dragFrom = -1; renderBlocks(); };
    card.ondragover = e => {
      if (dragFrom < 0) return; e.preventDefault();
      const r = card.getBoundingClientRect(), after = e.clientY > r.top + r.height / 2;
      card.classList.toggle("drop-after", after); card.classList.toggle("drop-before", !after);
    };
    card.ondragleave = () => card.classList.remove("drop-after", "drop-before");
    card.ondrop = e => {
      e.preventDefault(); if (dragFrom < 0) return;
      const r = card.getBoundingClientRect(), after = e.clientY > r.top + r.height / 2;
      move(dragFrom, i + (after ? 1 : 0));
    };
    box.append(card);
  });
  updateStorage();
}
// move block from index `from` so that it lands before the block currently at `to`
function move(from, to){
  if (to < 0 || to > doc.blocks.length) return;
  const [b] = doc.blocks.splice(from, 1);
  doc.blocks.splice(to > from ? to - 1 : to, 0, b);
  changed(true);
}
function addBlock(type){
  const b = Object.assign({type, id: uid()}, TYPES[type].make());
  const at = doc.blocks.findIndex(x => x.id === openId);
  doc.blocks.splice(at < 0 ? doc.blocks.length : at + 1, 0, b);
  openId = b.id; changed(true);
  if (type === "image" || type === "gallery"){
    pickFiles(type === "gallery").then(async fs => {
      const names = await uploadFiles(fs, type === "gallery");
      if (!names.length) return;
      if (type === "image") b.file = names[0]; else b.files = names.slice(0, MAX_GALLERY);
      changed(true);
    });
  }
  setTimeout(() => { const c = document.querySelector(".card.open"); if (c) c.scrollIntoView({block:"nearest", behavior:"smooth"}); }, 30);
}

// ------------------------------------------------------------------ block editors
function field(label, ...kids){ return [el("label", {class:"f"}, label), ...kids]; }
function text(b, key, sum, placeholder){
  return el("input", {type:"text", value: b[key] || "", placeholder: placeholder || "",
    oninput: e => { b[key] = e.target.value; if (sum) sum.textContent = summary(b); changed(); }});
}
function seg(options, value, onpick){
  const s = el("div", {class:"seg"});
  for (const [v, label] of options){
    s.append(el("button", {class: v === value ? "on" : "", onclick: () => {
      [...s.children].forEach(c => c.classList.remove("on")); s.children[options.findIndex(o => o[0] === v)].classList.add("on"); onpick(v);
    }}, label));
  }
  return s;
}
const ALIGN = [["left", "Left"], ["center", "Center"]];
function editor(b, sum){
  switch (b.type){
    case "heading": return [
      ...field("Text", text(b, "text", sum)),
      el("div", {class:"row"},
        el("div", {}, ...field("Size", seg([["l","Large"],["m","Medium"],["s","Small"]], b.size, v => { b.size = v; changed(); }))),
        el("div", {}, ...field("Align", seg(ALIGN, b.align, v => { b.align = v; changed(); }))))];
    case "text": {
      const rte = el("div", {class:"rte", contenteditable:"true", html: b.html});
      const sync = () => { b.html = rte.innerHTML; sum.textContent = summary(b); changed(); };
      rte.oninput = sync;
      rte.onpaste = e => { e.preventDefault(); document.execCommand("insertText", false, e.clipboardData.getData("text/plain")); };
      const cmd = (label, title, fn, style) => el("button", {title, style: style || "",
        onmousedown: e => { e.preventDefault(); fn(); sync(); }}, label);
      const bar = el("div", {class:"bar"},
        cmd("B", "Bold", () => document.execCommand("bold"), "font-weight:800"),
        cmd("I", "Italic", () => document.execCommand("italic"), "font-style:italic"),
        cmd("U", "Underline", () => document.execCommand("underline"), "text-decoration:underline"),
        cmd("• List", "Bullet list", () => document.execCommand("insertUnorderedList")),
        cmd("1. List", "Numbered list", () => document.execCommand("insertOrderedList")),
        cmd("Link", "Add link", () => { const u = prompt("Link address (https://…)", "https://"); if (u && u !== "https://") document.execCommand("createLink", false, u); }),
        cmd("Clear", "Remove formatting", () => { document.execCommand("removeFormat"); document.execCommand("unlink"); }));
      return [bar, rte, el("div", {}, ...field("Align", seg(ALIGN, b.align, v => { b.align = v; changed(); })))];
    }
    case "image": {
      const set = async fs => { const n = await uploadFiles(fs.slice(0, 1)); if (n.length){ b.file = n[0]; changed(true); } };
      return [
        b.file ? el("img", {class:"thumb", src: "/" + b.file, alt:""}) : dropZone("Click or drop an image here (PNG, JPG, WebP or animated GIF)", false, set),
        b.file ? el("div", {class:"row", style:"margin-top:8px"}, el("button", {class:"btn sm", onclick: async () => set(await pickFiles(false))}, "Replace image…"),
                   el("span", {class:"hint", style:"margin:0;text-align:right"}, (b.file.endsWith(".gif") ? "Animated GIF · " : "") + (sizes[b.file] ? kb(sizes[b.file]) : ""))) : null,
        ...field("Caption (optional)", text(b, "caption", sum)),
        ...field("Description for screen readers", text(b, "alt", null, "e.g. Our team at the open day")),
        ...field("Width", seg([[100,"Full"],[75,"75%"],[50,"Half"]], b.width, v => { b.width = v; changed(); }))];
    }
    case "gallery": {
      const add = async fs => {
        const room = MAX_GALLERY - b.files.length;
        if (room <= 0){ toast(`A gallery holds at most ${MAX_GALLERY} images.`); return; }
        const n = await uploadFiles(fs.slice(0, room), true); if (n.length){ b.files.push(...n); changed(true); }
      };
      const grid = el("div", {class:"gal"}, b.files.map((f, i) => el("div", {},
        el("img", {src: "/" + f, alt:""}),
        i ? el("button", {class:"mv", title:"Move left", onclick: () => { b.files.splice(i - 1, 0, b.files.splice(i, 1)[0]); changed(true); }}, "‹") : null,
        el("button", {title:"Remove", onclick: () => { b.files.splice(i, 1); changed(true); }}, "✕"))));
      return [b.files.length ? grid : null,
        dropZone(b.files.length ? "Add more images…" : "Click or drop images here (several at once)", true, add),
        ...field("Columns", seg([[2,"2"],[3,"3"]], b.cols, v => { b.cols = v; changed(); }))];
    }
    case "callout": {
      const ta = el("textarea", {oninput: e => { b.text = e.target.value; sum.textContent = summary(b); changed(); }});
      ta.value = b.text;
      return [...field("Text", ta), ...field("Colour", seg([["accent","Accent"],["orange","Orange"],["green","Green"]], b.tone, v => { b.tone = v; changed(); }))];
    }
    case "divider": return [el("p", {class:"hint"}, "A thin line between sections.")];
    case "spacer": return field("Height", seg([["s","Small"],["m","Medium"],["l","Large"]], b.size, v => { b.size = v; sum.textContent = summary(b); changed(); }));
  }
  return [];
}

// ------------------------------------------------------------------ style pane
function renderStyle(){
  const p = $("#p-style"); p.innerHTML = "";
  const colorInput = (key, label) => {
    const inp = el("input", {type:"color", value: doc[key]});
    inp.oninput = () => { doc[key] = inp.value.toUpperCase(); changed(); };
    return el("label", {class:"color"}, inp, label);
  };
  p.append(
    ...field("Page title (browser tab)", text(doc, "title")),
    ...field("Footer (optional)", text(doc, "footer", null, "e.g. © 2026 Our school")),
    ...field("Theme"),
    el("div", {class:"presets"}, PRESETS.map(t => el("div", {class:"preset", style:`--c:${t.card}`, onclick: () => {
      Object.assign(doc, {bg:t.bg, card:t.card, fg:t.fg, accent:t.accent}); renderStyle(); changed(); }},
      el("i", {style:`background:${t.bg};box-shadow:inset 0 0 0 1px #444`}), t.name))),
    ...field("Colours"),
    el("div", {class:"colors"}, colorInput("bg", "Background"), colorInput("card", "Card"), colorInput("fg", "Text"), colorInput("accent", "Accent / headings")),
    ...field("Font", seg([["sans","Modern"],["serif","Classic"],["rounded","Rounded"]], doc.font, v => { doc.font = v; changed(); })),
    ...field("Layout", seg([["card","Card"],["flat","Full page"]], doc.layout, v => { doc.layout = v; changed(); })),
    ...field("Width on large screens", seg([["narrow","Narrow"],["normal","Normal"],["wide","Wide"]], doc.width, v => { doc.width = v; changed(); })),
  );
}

// ------------------------------------------------------------------ status / upload
let lastKind = "";
async function poll(){
  try {
    const s = await (await api("/api/status")).json();
    $("#status").textContent = s.status; $("#dot").className = "dot " + s.kind;
    $("#upload").disabled = s.busy;
    if (lastKind === "busy" && s.kind !== "busy") toast(s.status, 4000);
    lastKind = s.kind;
    if (s.rev !== rev && !saving && !saveTimer && !dirty){ const was = $("#pname").textContent; await load();
      toast($("#pname").textContent !== was ? `Now editing “${$("#pname").textContent}”` : "Content was changed in Portal Admin and reloaded."); }
  } catch (e){
    $("#status").textContent = "Portal Admin is closed. Restart it to keep editing."; $("#dot").className = "dot err";
    $("#upload").disabled = true;
  }
}
$("#upload").onclick = async () => {
  dirty = dirty || !!saveTimer; await save();
  try { await api("/api/upload", {method:"POST"}); toast("Uploading… keep the board connected.", 2500); poll(); }
  catch (e){ toast("Upload failed: " + e.message, 5000); }
};

// ------------------------------------------------------------------ init
for (const [type, t] of Object.entries(TYPES))
  $("#chips").append(el("button", {class:"chip", onclick: () => addBlock(type)}, el("span", {class:"ico"}, t.ico), t.label));
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("on", x === b));
  $("#p-content").hidden = b.dataset.p !== "content"; $("#p-style").hidden = b.dataset.p !== "style";
});
document.querySelectorAll("#view button").forEach(b => b.onclick = () => {
  document.querySelectorAll("#view button").forEach(x => x.classList.toggle("on", x === b));
  $("#frame").className = "frame " + b.dataset.v;
});
document.execCommand("defaultParagraphSeparator", false, "p");
// cards are only draggable while the grip is held, so text in the fields stays selectable
document.addEventListener("mouseup", () => document.querySelectorAll(".card[draggable=true]").forEach(c => { if (dragFrom < 0) c.draggable = false; }));
window.addEventListener("beforeunload", e => { if (dirty || saveTimer) { save(); } });
load().catch(e => setState("Cannot load: " + e.message, true));
poll(); setInterval(poll, 1500);
</script>
</body>
</html>
"""
