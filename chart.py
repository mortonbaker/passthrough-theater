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


# Rungs of intensity, in seconds between cues. The ladder is absolute time,
# not beat divisions, because what a body can follow does not scale with tempo.
RUNGS = [4.0, 3.0, 2.0, 1.5, 1.0, 0.7, 0.5, 0.35, 0.25]


def label_for(interval):
    if interval <= 0:
        return "rest"
    if interval >= 2.5:
        return "slow"
    if interval >= 1.2:
        return "steady"
    if interval >= 0.6:
        return "double"
    return "fast"


def session_arc(duration, energy, beats, rounds=3):
    """Build rounds of climb, peak and rest rather than tracking the music.

    A peak only reads as a peak against the valley before it, so each round
    starts and finishes higher than the last while still dropping to nothing in
    between: the baseline resets, and the next climb lands harder than it would
    have from a standing start.
    """
    # Budget by what time there is, so every round completes rather than the
    # last one being cut off mid-climb.
    rounds = max(1, min(4, int(duration // 110)))

    secs, t = [], 2.0
    budget = (duration - 4.0) / rounds
    for r in range(rounds):
        frac = r / max(1, rounds - 1) if rounds > 1 else 1.0
        last = (r == rounds - 1)

        # ratchet: start further up the ladder each round, and finish higher
        lo = int(round(frac * 3))
        hi = min(len(RUNGS) - 1, int(round(4 + frac * (len(RUNGS) - 5))))
        if last:
            hi = len(RUNGS) - 1          # the final round earns the top rung
        rungs = RUNGS[lo:hi + 1] or [RUNGS[-1]]

        # climb / hold / recover, as fractions of this round's slice
        rest  = 0.0 if last else budget * 0.28
        peak  = budget * 0.16
        climb = budget - peak - rest

        per = climb / max(1, len(rungs))
        for iv in rungs:
            secs.append({"t": round(t, 2), "end": round(t + per, 2),
                         "interval": iv, "pattern": label_for(iv)})
            t += per

        secs.append({"t": round(t, 2), "end": round(t + peak, 2),
                     "interval": rungs[-1], "pattern": label_for(rungs[-1]),
                     "peak": True})
        t += peak

        if rest > 0:
            secs.append({"t": round(t, 2), "end": round(t + rest, 2),
                         "interval": 0.0, "pattern": "rest"})
            t += rest
    return secs


def cues(beats, secs):
    """Walk each section at its interval, snapping to the nearest real beat."""
    out = []
    for s in secs:
        iv = s.get("interval", 0)
        if iv <= 0:
            continue
        t = s["t"]
        while t <= s["end"]:
            # snap so cues sit on the music rather than beside it
            b = min(beats, key=lambda x: abs(x - t)) if beats else t
            if abs(b - t) > iv * 0.5:
                b = t
            out.append({"t": round(b, 3), "kind": s["pattern"]})
            t += iv
    out.sort(key=lambda c: c["t"])
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
    secs = session_arc(len(sig) / SR, energy, beats)
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
