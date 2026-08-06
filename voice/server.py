#!/usr/bin/env python3
"""Theater voice bridge on spark: Whisper -> Ollama -> Chatterbox.

Replaces the studio-pc bridge (Whisper -> LM Studio -> Chatterbox) after LM
Studio hung with no remote way to restart it. Same HTTP surface, so the media
server's /voice/* proxy points here unchanged via the VOICE_BRIDGE env var.

  POST /talk?sid=S&voice=V   audio body -> {you, reply, audio, more, job}
  GET  /chunk?job=J&n=N      long-poll  -> {audio, text, more}
  GET  /health

The reply streams sentence-by-sentence: /talk returns the first sentence's
audio plus a job id, and /chunk serves the rest as they synthesize.

Persona lives in ~/theater-voice/persona.md and is re-read on every request,
so edits are live — the file is the operator-editable spot (the Open WebUI db
on this box is root-owned, so the old read-from-WebUI trick is not available).
Reference voices are ~/theater-voice/voices/<name>.wav (Chatterbox zero-shot).

Runs under the venv that has chatterbox importable (xtts-venv on spark):
  ~/xtts-venv/bin/python ~/theater-voice/server.py
"""
import base64
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import requests

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "theater-voice")
VOICES = os.path.join(BASE, "voices")
PERSONA_PATH = os.path.join(BASE, "persona.md")

WHISPER = os.environ.get("THEATER_WHISPER", "http://100.70.209.20:8178/inference")
OLLAMA = os.environ.get("THEATER_OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("THEATER_LLM", "hf.co/bartowski/L3-8B-Stheno-v3.2-GGUF:Q6_K")
DEFAULT_VOICE = os.environ.get("THEATER_VOICE", "serafina")
PORT = int(os.environ.get("THEATER_PORT", "8781"))

DEFAULT_PERSONA = (
    "You are Mariko, a warm, playful, flirtatious companion. Stay in character."
    " Keep replies short and conversational, one to three sentences, no lists,"
    " no stage directions. Never mention being an AI or a language model."
)

# ---------------------------------------------------------------- persona
def persona():
    try:
        text = open(PERSONA_PATH, encoding="utf-8").read().strip()
        if text:
            return text
    except OSError:
        pass
    return DEFAULT_PERSONA


# ---------------------------------------------------------------- voices
def voice_path(name):
    """Resolve a voice name to a reference wav, falling back sensibly."""
    for candidate in (name, DEFAULT_VOICE):
        if candidate:
            p = os.path.join(VOICES, os.path.basename(candidate) + ".wav")
            if os.path.exists(p):
                return p
    rest = sorted(f for f in os.listdir(VOICES) if f.endswith(".wav")) if os.path.isdir(VOICES) else []
    return os.path.join(VOICES, rest[0]) if rest else None


def voice_list():
    if not os.path.isdir(VOICES):
        return []
    return sorted(f[:-4] for f in os.listdir(VOICES) if f.endswith(".wav"))


# ---------------------------------------------------------------- tts
_tts = None
_tts_lock = threading.Lock()


def tts_model():
    global _tts
    if _tts is None:
        import torch
        from chatterbox.tts import ChatterboxTTS
        device = "cuda" if torch.cuda.is_available() else "cpu"
        _tts = ChatterboxTTS.from_pretrained(device=device)
        print("chatterbox loaded on", device, flush=True)
    return _tts


def synth_b64(text, ref):
    """One sentence -> base64 WAV. The GPU is shared, so synthesis is serialized."""
    with _tts_lock:
        m = tts_model()
        tensor = m.generate(text, audio_prompt_path=ref)
        sr = m.sr
    pcm = np.clip(tensor.squeeze().cpu().numpy(), -1.0, 1.0)
    buf = io.BytesIO()
    # torchaudio.save resolves to a torchcodec-less build on this box (see
    # voices/speak.py history); write the PCM ourselves.
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((pcm * 32767).astype(np.int16).tobytes())
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- stt / llm
def transcribe(body):
    """Any browser audio -> 16 kHz mono wav -> whisper-server."""
    fd, out = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        p = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", out],
            input=body, capture_output=True, timeout=60)
        if p.returncode != 0:
            return None, "ffmpeg: " + p.stderr.decode(errors="replace")[-200:]
        with open(out, "rb") as fh:
            r = requests.post(WHISPER, files={"file": ("a.wav", fh, "audio/wav")},
                              data={"response_format": "json"}, timeout=120)
        r.raise_for_status()
        return (r.json().get("text") or "").strip(), None
    except Exception as exc:
        return None, "whisper: %s" % exc
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


HIST = {}  # sid -> alternating user/assistant messages
HIST_LOCK = threading.Lock()


def chat(sid, text, scene=""):
    with HIST_LOCK:
        hist = list(HIST.get(sid, []))
    sysmsg = persona()
    # Scene grounding: the media server resolves the viewer's playback time to
    # a scene-transcript entry and forwards it, so "this" means what is on
    # screen. Rebuilt every turn - it tracks the video as it plays.
    if scene:
        sysmsg += ("\n\nOn the screen in front of them right now: " + scene
                   + "\nWhen they talk about what they are watching, that is "
                   "what they mean. React to it naturally and specifically, "
                   "as if you are watching it together.")
    msgs = [{"role": "system", "content": sysmsg}] + hist + [{"role": "user", "content": text}]
    r = requests.post(OLLAMA + "/api/chat", json={
        "model": MODEL, "messages": msgs, "stream": False,
        "options": {"temperature": 0.8, "num_ctx": 8192}}, timeout=180)
    r.raise_for_status()
    reply = (r.json().get("message") or {}).get("content", "").strip()
    # a reasoning model's think-block would be read aloud; strip defensively
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S).strip()
    # stheno narrates actions in asterisks despite the persona's instructions,
    # and TTS reads them out loud ("asterisk blushes asterisk") — drop them
    stripped = re.sub(r"\*[^*\n]{0,200}\*", " ", reply)
    stripped = re.sub(r"\s{2,}", " ", stripped).strip()
    reply = stripped or reply.replace("*", "")
    with HIST_LOCK:
        hist = HIST.setdefault(sid, [])
        hist += [{"role": "user", "content": text},
                 {"role": "assistant", "content": reply}]
        del hist[:-24]
    return reply


def sentences(text):
    parts = re.split(r"(?<=[.!?…])\s+", text.replace("\n", " "))
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------- chunk jobs
JOBS = {}
JOBS_LOCK = threading.Lock()


def start_job(sents, ref):
    """Synthesize sentences 1..n in the background; /chunk long-polls them."""
    job = uuid.uuid4().hex[:12]
    state = {"chunks": [], "texts": sents, "done": not sents, "born": time.time()}
    with JOBS_LOCK:
        JOBS[job] = state
        for k in [k for k, v in JOBS.items() if time.time() - v["born"] > 600]:
            del JOBS[k]

    def work():
        try:
            for s in sents:
                state["chunks"].append(synth_b64(s, ref))
        except Exception as exc:
            print("job %s died: %s" % (job, exc), flush=True)
        finally:
            state["done"] = True

    if sents:
        threading.Thread(target=work, daemon=True).start()
    return job


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/health":
            return self.send_json({
                "ok": True, "voice": DEFAULT_VOICE, "model": MODEL,
                "persona_id": "persona.md", "persona_chars": len(persona()),
                "voices": voice_list(), "whisper_loaded": True})
        if u.path == "/chunk":
            job = (q.get("job") or [""])[0]
            n = int((q.get("n") or ["0"])[0])
            state = JOBS.get(job)
            if not state or n < 1:
                return self.send_json({"audio": None, "more": False})
            idx = n - 1
            deadline = time.time() + 25
            while time.time() < deadline:
                if len(state["chunks"]) > idx or state["done"]:
                    break
                time.sleep(0.25)
            if idx < len(state["chunks"]):
                return self.send_json({
                    "audio": state["chunks"][idx], "text": state["texts"][idx],
                    "more": idx + 1 < len(state["texts"])})
            return self.send_json({"audio": None, "more": False})
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path != "/talk":
            return self.send_json({"error": "not found"}, 404)
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= (25 << 20):
            return self.send_json({"error": "bad length"}, 400)
        body = self.rfile.read(length)
        sid = (q.get("sid") or ["anon"])[0]
        scene = (q.get("scene") or [""])[0][:900]
        ref = voice_path((q.get("voice") or [""])[0])
        if not ref:
            return self.send_json({"error": "no reference voices installed"})
        heard, err = transcribe(body)
        if err:
            return self.send_json({"error": err})
        if not heard or len(heard) < 2:
            return self.send_json({"you": heard or "", "reply": "", "audio": None,
                                   "more": False, "note": "nothing heard"})
        try:
            reply = chat(sid, heard, scene)
        except Exception as exc:
            return self.send_json({"you": heard, "error": "llm: %s" % exc})
        if not reply:
            return self.send_json({"you": heard, "reply": "", "audio": None,
                                   "more": False, "note": "empty reply"})
        sents = sentences(reply)
        try:
            first = synth_b64(sents[0], ref)
        except Exception as exc:
            return self.send_json({"you": heard, "reply": reply, "error": "tts: %s" % exc})
        job = start_job(sents[1:], ref)
        self.send_json({"you": heard, "reply": sents[0], "audio": first,
                        "more": len(sents) > 1, "job": job, "error": None})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    os.makedirs(VOICES, exist_ok=True)
    # warm the TTS weights before the first request needs them
    threading.Thread(target=tts_model, daemon=True).start()
    print("theater voice bridge on :%d  model=%s voice=%s" % (PORT, MODEL, DEFAULT_VOICE), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
