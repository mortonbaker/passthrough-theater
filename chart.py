#!/usr/bin/env python3
"""
Build rhythm charts from audio.

Beat detection gives a grid; a chart needs choreography. So this does both:
locate the beats, measure how the energy moves, then lay patterns over the grid
that follow the track's own shape - denser and faster where it drives, sparse
or resting where it drops.

  chart.py <file|dir> [--force]

Writes <track>.chart.json next to each audio file.
"""

import json
import os
import subprocess
import sys
import wave

import numpy as np

# MeTube writes whatever container the source used, so accept video ones too;
# ffmpeg pulls the audio stream out either way.
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".flac", ".wav", ".ogg",
             ".webm", ".mkv", ".mp4"}
SR = 44100

# Pattern vocabulary, slowest to fastest. "div" is beats per cue: 2 = every
# other beat, 0.5 = twice a beat. Chosen by section intensity.
# "div" is beats per cue: 4 = one cue every four beats, 0.5 = twice a beat.
# The whole scale was one step too fast - four cues a beat is about 8.7 a
# second at 130bpm, which nobody can follow. Two a beat is the practical
# ceiling, so that is where the scale now tops out.
PATTERNS = [
    {"name": "rest",   "div": 0.0},
    {"name": "slow",   "div": 4.0},
    {"name": "steady", "div": 2.0},
    {"name": "double", "div": 1.0},
    {"name": "fast",   "div": 0.5},
]

# Divisions alone are tempo-relative, so a fast track would still outrun the
# ceiling. Cap the actual rate: no cue closer than this to the previous one.
MIN_GAP = 0.22          # seconds, about 4.5 cues per second


def decode(path):
    """Mono float32 at SR, via ffmpeg so every input format works."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "wav", "-ac", "1",
         "-ar", str(SR), "-acodec", "pcm_s16le", "-"],
        capture_output=True, timeout=900)
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError("decode failed: " + out.stderr.decode()[:200])
    import io
    with wave.open(io.BytesIO(out.stdout)) as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def beats_of(path):
    """Beat times and tempo from aubio."""
    import aubio
    hop = 512
    src = aubio.source(path, SR, hop)
    tempo = aubio.tempo("default", 1024, hop, SR)
    beats, frames = [], 0
    while True:
        samples, read = src()
        if tempo(samples):
            beats.append(tempo.get_last_s())
        frames += read
        if read < hop:
            break
    return beats, (tempo.get_bpm() if beats else 0.0)


def energy_curve(sig, beats):
    """RMS per beat, normalised 0..1 - this is what drives the choreography."""
    if len(beats) < 2:
        return []
    out = []
    for i, t in enumerate(beats):
        a = int(t * SR)
        b = int(beats[i + 1] * SR) if i + 1 < len(beats) else a + int(0.5 * SR)
        seg = sig[a:max(b, a + 256)]
        out.append(float(np.sqrt(np.mean(seg * seg))) if seg.size else 0.0)
    out = np.array(out)
    lo, hi = np.percentile(out, 5), np.percentile(out, 95)
    return np.clip((out - lo) / (hi - lo + 1e-9), 0, 1).tolist()


def sections(beats, energy, bars=8):
    """Group beats into bars-long sections and pick a pattern per section."""
    if not beats:
        return []
    per = bars * 4                      # 4/4
    out = []
    for start in range(0, len(beats), per):
        chunk = energy[start:start + per]
        if not chunk:
            continue
        lvl = float(np.mean(chunk))
        # map mean energy onto the pattern vocabulary
        if lvl < 0.18:
            idx = 0
        elif lvl < 0.38:
            idx = 1
        elif lvl < 0.62:
            idx = 2
        elif lvl < 0.82:
            idx = 3
        else:
            idx = 4
        out.append({
            "t": round(beats[start], 3),
            "end": round(beats[min(start + per, len(beats)) - 1], 3),
            "pattern": PATTERNS[idx]["name"],
            "div": PATTERNS[idx]["div"],
            "energy": round(lvl, 3),
        })
    # merge neighbours that landed on the same pattern
    merged = []
    for s in out:
        if merged and merged[-1]["pattern"] == s["pattern"]:
            merged[-1]["end"] = s["end"]
            merged[-1]["energy"] = round((merged[-1]["energy"] + s["energy"]) / 2, 3)
        else:
            merged.append(s)
    return merged


def cues(beats, secs):
    """Expand sections into the actual cue times the player renders."""
    out = []
    for s in secs:
        if s["div"] <= 0:
            continue
        idx = [i for i, t in enumerate(beats) if s["t"] <= t <= s["end"]]
        if len(idx) < 2:
            continue
        # coarsen on fast tracks until the real spacing is playable
        spacing = (beats[idx[-1]] - beats[idx[0]]) / max(1, len(idx) - 1)
        step = s["div"]
        while spacing * step < MIN_GAP:
            step *= 2
        s["div"] = step
        if step >= 1:
            for k, i in enumerate(idx):
                if k % int(step) == 0:
                    out.append({"t": round(beats[i], 3), "kind": s["pattern"]})
        else:
            sub = int(round(1 / step))
            for j in range(len(idx) - 1):
                a, b = beats[idx[j]], beats[idx[j + 1]]
                for k in range(sub):
                    out.append({"t": round(a + (b - a) * k / sub, 3),
                                "kind": s["pattern"]})
    out.sort(key=lambda c: c["t"])
    # Coarsening by section average is not enough: detected beats jitter, so
    # subdividing between two close beats can still land inside the floor.
    # Enforce it on the finished list, where nothing can slip through.
    kept = []
    for c in out:
        if kept and c["t"] - kept[-1]["t"] < MIN_GAP:
            continue
        kept.append(c)
    return kept


def build(path, force=False):
    dest = os.path.splitext(path)[0] + ".chart.json"
    if os.path.exists(dest) and not force:
        return None
    beats, bpm = beats_of(path)
    if len(beats) < 8:
        raise RuntimeError("only %d beats found" % len(beats))
    sig = decode(path)
    energy = energy_curve(sig, beats)
    secs = sections(beats, energy)
    chart = {
        "title": os.path.splitext(os.path.basename(path))[0],
        "duration": round(len(sig) / SR, 2),
        "bpm": round(bpm, 2),
        "beats": [round(b, 3) for b in beats],
        "energy": [round(e, 3) for e in energy],
        "sections": secs,
        "cues": cues(beats, secs),
    }
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(chart, fh, indent=1)
    return chart


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    target = args[0] if args else "/srv/deovr/music"
    files = ([target] if os.path.isfile(target) else
             [os.path.join(target, f) for f in sorted(os.listdir(target))
              if os.path.splitext(f)[1].lower() in AUDIO_EXT])
    if not files:
        print("no audio found in", target)
        return
    for f in files:
        try:
            c = build(f, force)
            if c is None:
                print("  skip (charted): %s" % os.path.basename(f))
            else:
                print("  %-44s %6.1f bpm  %4d beats  %3d cues  %2d sections"
                      % (os.path.basename(f)[:44], c["bpm"], len(c["beats"]),
                         len(c["cues"]), len(c["sections"])))
        except Exception as exc:
            print("  FAIL %s: %s" % (os.path.basename(f), exc))


if __name__ == "__main__":
    main()
