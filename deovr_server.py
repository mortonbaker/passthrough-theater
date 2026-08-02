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


def duration_of(path):
    """Seconds via ffprobe, cached by path+mtime so re-scans are instant."""
    try:
        key = "dur:%s:%d" % (path, int(os.path.getmtime(path)))
    except OSError:
        return 0
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60,
        )
        secs = int(float(out.stdout.strip()))
    except Exception:
        secs = 0
    with _cache_lock:
        _cache[key] = secs
        save_cache()
    return secs


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
        entry = {
            "id": vid_for(name),
            "name": name,
            "path": path,
            "title": title[:120],
            "length": duration_of(path),
            "screen": screen,
            "stereo": stereo,
            "is3d": is3d,
        }
        entry.update(sidecar(path))
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
            if path in ("/", "/deovr", "/index.json"):
                return self.index()
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
            return self.send_json({
                "id": int(vid[:8], 16),
                "title": it["title"],
                "videoLength": it["length"],
                "is3d": it["is3d"],
                "screenType": it["screen"],
                "stereoMode": it["stereo"],
                "thumbnailUrl": "%s/thumb/%s.jpg" % (base, it["id"]),
                "encodings": [{
                    "name": "h264",
                    "videoSources": [{"resolution": 2160, "url": url}],
                }],
            })
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
