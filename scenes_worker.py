#!/usr/bin/env python3
"""Scene transcripts for the theater library. Runs on spark (DGX, ollama).

Walks every video the server knows, samples one frame every STEP seconds
(left eye, centre crop - a sane rectilinear-ish view of a fisheye SBS frame),
captions it with a local vision model, and posts the timed list back to the
server as a scenes sidecar. The voice bridge then grounds its replies in the
entry covering the current playback time, so "what do you think of this"
means the scene actually on screen.

  scenes_worker.py [--force] [--one VIDEO_ID] [--step 30]

Env: THEATER (default http://100.127.143.78:8250)   OLLAMA (127.0.0.1:11434)
     VLM_MODEL (default huihui_ai/qwen2.5-vl-abliterated:7b-instruct)
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

THEATER = os.environ.get("THEATER", "http://100.127.143.78:8250").rstrip("/")
OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("VLM_MODEL",
                       "huihui_ai/qwen2.5-vl-abliterated:7b-instruct")

# The captions feed a spoken companion, not an art critic: concrete, present
# tense, no throat-clearing, no euphemism, no guessing at inner states.
PROMPT = (
    "You are captioning one frame of a first-person VR video for a companion "
    "who will chat about it with the viewer. In 1-3 short, blunt, concrete "
    "sentences describe: who is visible and what they look like, their "
    "position and what they are doing, the viewer's role in the scene, and "
    "the setting. Use plain explicit language for whatever is happening. "
    "Never write 'the image shows', never hedge, never speculate about "
    "thoughts or feelings.")


def get_json(path):
    with urllib.request.urlopen(THEATER + path, timeout=60) as r:
        return json.load(r)


def has_scenes(vid):
    try:
        with urllib.request.urlopen(THEATER + "/scenes/" + vid, timeout=30):
            return True
    except urllib.error.HTTPError:
        return False


def eye_filter(detail):
    """One eye, centre 55%, shrunk for the model."""
    stereo = (detail.get("stereoMode") or "sbs").lower()
    if detail.get("is3d") is False:
        stereo = "off"
    pre = {"sbs": "crop=iw/2:ih:0:0,", "tb": "crop=iw:ih/2:0:0,"}.get(stereo, "")
    return (pre + "crop=iw*0.55:ih*0.55:(iw-iw*0.55)/2:(ih-ih*0.55)/2,"
                  "scale=896:-2")


def frame(url, t, vf):
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", url, "-frames:v", "1",
         "-vf", vf, "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "-"],
        capture_output=True, timeout=240)
    if r.returncode or not r.stdout:
        raise RuntimeError("ffmpeg: " + r.stderr.decode()[:160])
    return r.stdout


def duration_of(detail, url):
    dur = int(detail.get("videoLength") or 0)
    if dur:
        return dur
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", url], capture_output=True, text=True, timeout=120)
    return int(float(r.stdout.strip() or 0))


def caption(jpg):
    payload = {"model": MODEL, "prompt": PROMPT, "stream": False,
               "images": [base64.b64encode(jpg).decode()],
               "keep_alive": "20m",
               "options": {"temperature": 0.2, "num_predict": 160}}
    req = urllib.request.Request(
        OLLAMA + "/api/generate", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return (json.load(r).get("response") or "").strip()


def post_scenes(vid, doc):
    req = urllib.request.Request(
        THEATER + "/scenes/" + vid, data=json.dumps(doc).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def transcribe(vid, title, step):
    detail = get_json("/video/" + vid)
    url = detail["encodings"][0]["videoSources"][0]["url"]
    dur = duration_of(detail, url)
    if dur < 5:
        print("  no duration; skipping", flush=True)
        return
    vf = eye_filter(detail)
    # offset by half a step so the first frame is not the black lead-in
    ts = list(range(step // 2, max(step, dur), step))
    scenes, t0 = [], time.time()
    for i, t in enumerate(ts):
        try:
            scenes.append({"t": t, "d": caption(frame(url, t, vf))})
        except Exception as exc:
            print("  t=%ds failed: %s" % (t, exc), flush=True)
            continue
        if i % 10 == 0:
            per = (time.time() - t0) / max(1, i + 1)
            print("  %d/%d frames  %.1fs/frame  eta %dm"
                  % (i + 1, len(ts), per, per * (len(ts) - i - 1) / 60),
                  flush=True)
    doc = {"v": 1, "step": step, "model": MODEL, "made": int(time.time()),
           "title": title, "scenes": scenes}
    post_scenes(vid, doc)
    print("  saved %d scenes in %.0fs" % (len(scenes), time.time() - t0),
          flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="redo videos that already have scenes")
    ap.add_argument("--one", help="only this video id")
    ap.add_argument("--step", type=int, default=30,
                    help="seconds between sampled frames")
    args = ap.parse_args()

    lst = get_json("/deovr")["scenes"][0]["list"]
    todo = []
    for it in lst:
        vid = it["video_url"].rsplit("/", 1)[-1]
        if args.one and vid != args.one:
            continue
        if not args.force and not args.one and has_scenes(vid):
            print("skip (done): %s" % it.get("title", vid), flush=True)
            continue
        todo.append((vid, it.get("title", vid)))

    print("%d videos to transcribe with %s" % (len(todo), MODEL), flush=True)
    for vid, title in todo:
        print("== %s (%s)" % (title[:60], vid), flush=True)
        try:
            transcribe(vid, title, args.step)
        except Exception as exc:
            print("  FAILED: %s" % exc, flush=True)
    print("all done", flush=True)


if __name__ == "__main__":
    main()
