#!/usr/bin/env python3
"""
DeoVR LAN media server.

Serves a DeoVR-compatible JSON API plus byte-range video streaming, so a Quest 3
can browse and play VR videos straight off the LAN. Standard library only.

Point DeoVR at:  http://<lan-ip>:8250
"""

import hashlib
import json
import os
import re
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "/srv/deovr"
MEDIA_DIR = os.path.join(BASE, "media")
THUMB_DIR = os.path.join(BASE, "thumbs")
PLAYER_DIR = os.path.join(BASE, "player")
CACHE_PATH = os.path.join(BASE, "cache.json")
PORT = 8250

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".m4v", ".mov"}

# Filename tokens -> DeoVR screenType. SLR/CzechVR/etc. encode projection in the
# filename, so we can set the lens correctly without any manual tagging.
SCREEN_TOKENS = [
    ("MKX220", "mkx220"),
    ("MKX200", "mkx200"),
    ("VRCA220", "vrca220"),
    ("FISHEYE190", "fisheye190"),
    ("RF52", "rf52"),
    ("_360", "sphere"),
    ("360_", "sphere"),
    ("_180", "dome"),
    ("180_", "dome"),
]

_cache_lock = threading.Lock()
_cache = {}


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def load_cache():
    global _cache
    try:
        with open(CACHE_PATH) as fh:
            _cache = json.load(fh)
    except Exception:
        _cache = {}


def save_cache():
    tmp = CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(_cache, fh)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def vid_for(name):
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:12]


def probe(path):
    """Duration, height and codec via one ffprobe, cached by path+mtime."""
    fallback = {"length": 0, "height": 2160, "codec": "h264"}
    try:
        key = "probe:%s:%d" % (path, int(os.path.getmtime(path)))
    except OSError:
        return fallback
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "format=duration:stream=height,codec_name",
             "-of", "default=noprint_wrappers=1", path],
            capture_output=True, text=True, timeout=60,
        )
        fields = {}
        for line in out.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k.strip()] = v.strip()
        # DeoVR keys its decoder off this name, so hevc must not be called h264.
        raw = fields.get("codec_name", "").lower()
        info = {
            "length": int(float(fields.get("duration", 0) or 0)),
            "height": int(fields.get("height", 0) or 0) or 2160,
            "codec": "h265" if raw in ("hevc", "h265") else "h264",
        }
    except Exception:
        info = fallback
    with _cache_lock:
        _cache[key] = info
        save_cache()
    return info


def layout_for(name):
    """Guess (screenType, stereoMode, is3d) from the filename."""
    up = name.upper()

    screen = "dome"  # 180 hemisphere is the safe default for VR clips
    for token, value in SCREEN_TOKENS:
        if token in up:
            screen = value
            break

    if re.search(r"(_|\b)(MONO|2D)(_|\b|\.)", up):
        return screen, "off", False
    if re.search(r"(_|\b)(TB|OVERUNDER|OVER_UNDER)(_|\b|\.)", up):
        return screen, "tb", True
    # SBS is the overwhelming default for 180/fisheye VR material
    return screen, "sbs", True


def chapters_of(path):
    """Embedded MP4 chapters as DeoVR timeStamps, cached by path+mtime."""
    try:
        key = "chap:%s:%d" % (path, int(os.path.getmtime(path)))
    except OSError:
        return []
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    marks = []
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
        for i, ch in enumerate(json.loads(out.stdout or "{}").get("chapters", []), 1):
            name = (ch.get("tags") or {}).get("title") or ("Chapter %d" % i)
            marks.append({"ts": int(float(ch.get("start_time", 0))), "name": name})
    except Exception:
        marks = []
    with _cache_lock:
        _cache[key] = marks
        save_cache()
    return marks


def is_faststart(path):
    """True when moov precedes mdat, i.e. the file can stream without a full pull."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            off = 0
            while off < size:
                fh.seek(off)
                hdr = fh.read(8)
                if len(hdr) < 8:
                    break
                box = int.from_bytes(hdr[:4], "big")
                typ = hdr[4:8].decode("latin1", "replace")
                if box == 1:
                    box = int.from_bytes(fh.read(8), "big")
                if box < 8:
                    break
                if typ == "moov":
                    return True
                if typ == "mdat":
                    return False
                off += box
    except Exception:
        pass
    return True  # unknown container: assume fine rather than nag


def sidecar(path):
    """Optional <video>.json next to the file overrides any guess."""
    try:
        with open(os.path.splitext(path)[0] + ".json") as fh:
            return json.load(fh)
    except Exception:
        return {}


def scan():
    """Return [{id,name,path,title,length,screen,stereo,is3d}] sorted by title."""
    items = []
    if not os.path.isdir(MEDIA_DIR):
        return items
    for name in sorted(os.listdir(MEDIA_DIR)):
        path = os.path.join(MEDIA_DIR, name)
        if not os.path.isfile(path) or os.path.splitext(name)[1].lower() not in VIDEO_EXT:
            continue
        screen, stereo, is3d = layout_for(name)
        title = os.path.splitext(name)[0].replace("_", " ").strip()
        info = probe(path)
        entry = {
            "id": vid_for(name),
            "name": name,
            "path": path,
            "title": title[:120],
            "length": info["length"],
            "height": info["height"],
            "codec": info["codec"],
            "screen": screen,
            "stereo": stereo,
            "is3d": is3d,
            "chapters": chapters_of(path),
        }
        entry.update(sidecar(path))
        if not is_faststart(path):
            print("WARNING: %s has moov after mdat; seeking and end-of-video will "
                  "misbehave. Fix with: ./optimize.sh" % name, flush=True)
        items.append(entry)
    return items


def thumb_for(entry):
    """Make a poster frame once, then reuse it. Left eye only, so it looks right."""
    out = os.path.join(THUMB_DIR, entry["id"] + ".jpg")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    os.makedirs(THUMB_DIR, exist_ok=True)
    # Grab a frame ~20% in; a frame at t=0 is usually black or a title card.
    at = max(5, int(entry["length"] * 0.2)) if entry["length"] else 5
    if entry["stereo"] == "sbs":
        crop = "crop=iw/2:ih:0:0,"
    elif entry["stereo"] == "tb":
        crop = "crop=iw:ih/2:0:0,"
    else:
        crop = ""
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-ss", str(at), "-i", entry["path"],
             "-vf", crop + "scale=640:-2", "-frames:v", "1", "-q:v", "4", out],
            capture_output=True, timeout=120,
        )
    except Exception:
        pass
    return out if os.path.exists(out) else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "deovr-lan/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def base_url(self):
        # Derive from the Host header so LAN IP / hostname / Tailscale all work.
        host = self.headers.get("Host") or ("192.168.0.188:%d" % PORT)
        return "http://" + host

    def send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            # DeoVR asks for /deovr on a bare host and expects a normal page at
            # "/". Serving JSON at the root makes its browser render it as text.
            if path == "/":
                return self.homepage()
            if path in ("/deovr", "/index.json"):
                return self.index()
            if path == "/player" or path.startswith("/player/"):
                return self.player_static(path)
            if path.startswith("/video/"):
                return self.detail(path[len("/video/"):])
            if path.startswith("/thumb/"):
                return self.thumb(path[len("/thumb/"):].removesuffix(".jpg"))
            if path.startswith("/media/"):
                return self.media(urllib.parse.unquote(path[len("/media/"):]))
            self.send_error(404, "not found")
        except BrokenPipeError:
            pass  # headset closed the stream mid-seek; normal
        except Exception as exc:
            try:
                self.send_error(500, str(exc))
            except Exception:
                pass

    def player_static(self, path):
        """Serve the WebXR player. Modules need a JS MIME type or Chromium refuses them."""
        name = os.path.basename(path[len("/player"):].lstrip("/")) or "index.html"
        fp = os.path.join(PLAYER_DIR, name)
        if not os.path.isfile(fp):
            return self.send_error(404, "not found")
        ext = name.rsplit(".", 1)[-1].lower()
        ctype = {"html": "text/html; charset=utf-8",
                 "js": "application/javascript",
                 "css": "text/css"}.get(ext, "application/octet-stream")
        with open(fp, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        """POST /sidecar/<id> with a JSON body: merge into the video's sidecar file.

        Lets the in-headset player tune the chroma key once and persist it next
        to the video, so every future play reads the same settings.
        """
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/sidecar/"):
            return self.send_error(404, "not found")
        vid = path[len("/sidecar/"):]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 65536:
                return self.send_error(413, "bad size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
        except Exception:
            return self.send_error(400, "bad json")
        for it in scan():
            if it["id"] != vid:
                continue
            sc = os.path.splitext(it["path"])[0] + ".json"
            current = sidecar(it["path"])
            current.update(payload)
            with open(sc, "w") as fh:
                json.dump(current, fh, indent=2)
            return self.send_json({"ok": True})
        self.send_error(404, "no such video")

    def do_HEAD(self):
        # DeoVR probes with HEAD before streaming.
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/media/"):
            name = urllib.parse.unquote(path[len("/media/"):])
            fp = os.path.join(MEDIA_DIR, os.path.basename(name))
            if os.path.isfile(fp):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(os.path.getsize(fp)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
        self.send_error(404, "not found")

    def homepage(self):
        """Human-viewable gallery. DeoVR loads this, then fetches /deovr itself."""
        items = scan()
        base = self.base_url()
        cards = "".join(
            '<a class="c" href="/media/{q}"><img loading="lazy" src="/thumb/{i}.jpg">'
            '<span>{t}</span><em>{m}:{s:02d} &middot; {sc} &middot; {st}</em></a>'.format(
                q=urllib.parse.quote(it["name"]), i=it["id"],
                t=html_escape(it["title"]), m=it["length"] // 60, s=it["length"] % 60,
                sc=it["screen"], st=it["stereo"],
            )
            for it in items
        )
        page = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>deovr-lan</title>
<link rel="alternate" type="application/json" href="/deovr">
<style>
body{background:#111;color:#eee;font:15px/1.4 system-ui,sans-serif;margin:0;padding:24px}
h1{font-size:18px;font-weight:600;margin:0 0 4px}
p{color:#888;margin:0 0 24px}code{color:#6cf}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.c{display:block;background:#1c1c1c;border-radius:8px;overflow:hidden;text-decoration:none;color:#eee}
.c:hover{background:#262626}
.c img{width:100%%;aspect-ratio:1;object-fit:cover;display:block;background:#000}
.c span{display:block;padding:10px 12px 2px;font-weight:500}
.c em{display:block;padding:0 12px 12px;color:#888;font-style:normal;font-size:13px}
</style></head><body>
<h1>deovr-lan &middot; %d video%s</h1>
<p>In DeoVR enter <code>%s</code> &nbsp;|&nbsp; manifest at <code>/deovr</code></p>
<div class="g">%s</div></body></html>""" % (
            len(items), "" if len(items) == 1 else "s", base, cards)
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def index(self):
        items = scan()
        base = self.base_url()
        lst = [{
            "title": it["title"],
            "videoLength": it["length"],
            "video_url": "%s/video/%s" % (base, it["id"]),
            "thumbnailUrl": "%s/thumb/%s.jpg" % (base, it["id"]),
        } for it in items]
        self.send_json({
            "authorized": "1",
            "scenes": [{"name": "LAN Library (%d)" % len(lst), "list": lst}],
        })

    def detail(self, vid):
        for it in scan():
            if it["id"] != vid:
                continue
            base = self.base_url()
            url = "%s/media/%s" % (base, urllib.parse.quote(it["name"]))
            detail = {
                "id": int(vid[:8], 16),
                "title": it["title"],
                "videoLength": it["length"],
                "is3d": it["is3d"],
                "screenType": it["screen"],
                "stereoMode": it["stereo"],
                "thumbnailUrl": "%s/thumb/%s.jpg" % (base, it["id"]),
                "encodings": [{
                    "name": it["codec"],
                    "videoSources": [{"resolution": it["height"], "url": url}],
                }],
            }
            if it.get("chapters"):
                detail["timeStamps"] = it["chapters"]
            # player-only metadata from sidecars, ignored by DeoVR
            for extra in ("chroma", "alphaPack"):
                if it.get(extra) is not None:
                    detail[extra] = it[extra]
            return self.send_json(detail)
        self.send_error(404, "no such video")

    def thumb(self, vid):
        for it in scan():
            if it["id"] == vid:
                jpg = thumb_for(it)
                if jpg:
                    data = open(jpg, "rb").read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(data)
                    return
        self.send_error(404, "no thumbnail")

    def media(self, name):
        """Range-aware streaming. Without 206 support, seeking in VR breaks."""
        fp = os.path.join(MEDIA_DIR, os.path.basename(name))
        if not os.path.isfile(fp):
            return self.send_error(404, "no such file")
        size = os.path.getsize(fp)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False

        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                g1, g2 = m.group(1), m.group(2)
                if g1:
                    start = int(g1)
                    if g2:
                        end = min(int(g2), size - 1)
                else:
                    # suffix form: last N bytes
                    if g2:
                        start = max(0, size - int(g2))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()

        with open(fp, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)
    load_cache()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print("deovr-lan serving %s on :%d" % (MEDIA_DIR, PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
