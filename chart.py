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


def onsets_of(path):
    """Percussive attacks. 'specflux' fires on transients, which is what a drum
    is; the tempo tracker alone happily emits beats through a silent intro."""
    import aubio
    hop = 256
    src = aubio.source(path, SR, hop)
    det = aubio.onset("specflux", 1024, hop, SR)
    det.set_threshold(0.35)
    out = []
    while True:
        samples, read = src()
        if det(samples):
            out.append(det.get_last_s())
        if read < hop:
            break
    return out


def percussive_strength(sig, beats):
    """Energy in the kick and snare/hat bands at each beat.

    A beat with no drum under it has little of either, which is how an intro or
    a breakdown gets told apart from the same grid position in a full section.
    """
    if not beats:
        return []
    win = int(0.08 * SR)
    freqs = np.fft.rfftfreq(win, 1.0 / SR)
    kick = (freqs >= 40) & (freqs <= 140)
    snare = (freqs >= 1500) & (freqs <= 9000)
    w = np.hanning(win)
    out = []
    for t in beats:
        a = int(t * SR)
        seg = sig[a:a + win]
        if seg.size < win:
            out.append(0.0)
            continue
        mag = np.abs(np.fft.rfft(seg * w))
        out.append(float(mag[kick].sum() + mag[snare].sum()))
    arr = np.array(out)
    hi = np.percentile(arr, 90) or 1.0
    return np.clip(arr / hi, 0, 1).tolist()


def percussive_beats(beats, onsets, strength, thresh=0.22, window=0.07):
    """Keep only beats with a real attack on them."""
    keep = []
    oi = 0
    for i, t in enumerate(beats):
        while oi < len(onsets) - 1 and onsets[oi] < t - window:
            oi += 1
        near = any(abs(onsets[j] - t) <= window
                   for j in range(max(0, oi - 2), min(len(onsets), oi + 3)))
        if near and strength[i] >= thresh:
            keep.append(t)
    return keep


def first_groove(beats, keep, run=6):
    """Where the drums actually start: the first sustained run of hit beats."""
    if not keep:
        return beats[0] if beats else 0.0
    ks = set(keep)
    streak, start = 0, None
    for t in beats:
        if t in ks:
            if streak == 0:
                start = t
            streak += 1
            if streak >= run:
                return start
        else:
            streak = 0
    return keep[0]


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


def session_arc(duration, energy, beats, rounds=3, start=2.0, tier=1.0):
    """Build rounds of climb, peak and rest rather than tracking the music.

    A peak only reads as a peak against the valley before it, so each round
    starts and finishes higher than the last while still dropping to nothing in
    between: the baseline resets, and the next climb lands harder than it would
    have from a standing start.
    """
    # Budget by what time there is, so every round completes rather than the
    # last one being cut off mid-climb.
    # ~90s buys a climb, a hold and a recovery, so a 3 minute track gets a
    # genuine valley rather than one unbroken ramp.
    duration = max(0.0, duration - start)
    rounds = max(1, min(4, int(duration // 90)))

    secs, t = [], start
    budget = (duration - 2.0) / rounds
    for r in range(rounds):
        frac = r / max(1, rounds - 1) if rounds > 1 else 1.0
        last = (r == rounds - 1)

        # 'tier' places this track within the session: an early track works the
        # bottom of the ladder, a late one the top. Within the track, the same
        # ratchet applies round to round.
        span = len(RUNGS) - 5
        base = int(round(tier * span))
        lo = min(len(RUNGS) - 2, base + int(round(frac * 2)))
        hi = min(len(RUNGS) - 1, base + 4 + int(round(frac * 1)))
        if last and tier >= 0.99:
            hi = len(RUNGS) - 1          # only the final track earns the top rung
        rungs = RUNGS[lo:hi + 1] or [RUNGS[-1]]

        # climb / hold / recover. Recovery shortens as the session escalates.
        rest  = 0.0 if last else budget * (0.34 - 0.14 * tier)
        peak  = budget * (0.12 + 0.08 * tier)
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
    # 'beats' here is already filtered to beats with a drum on them, so a
    # cue can never land in a passage with nothing to hit.
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
    onsets = onsets_of(path)
    strength = percussive_strength(sig, beats)
    hits = percussive_beats(beats, onsets, strength)
    if len(hits) < 8:                     # nothing percussive: fall back
        hits = beats
    start = first_groove(beats, hits)
    secs = session_arc(len(sig) / SR, energy, hits, start=start)
    chart = {
        "title": os.path.splitext(os.path.basename(path))[0],
        "duration": round(len(sig) / SR, 2),
        "bpm": round(bpm, 2),
        "beats": [round(b, 3) for b in beats],
        "hits": [round(b, 3) for b in hits],
        "grooveStart": round(start, 2),
        "energy": [round(e, 3) for e in energy],
        "sections": secs,
        "cues": cues(hits, secs),
    }
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(chart, fh, indent=1)
    return chart


def plan(charts, minutes=7.0):
    """Order tracks into an escalating session and re-cue each at its tier.

    Charts hold the expensive analysis already, so laying out a session is just
    arithmetic over the stored hit times.
    """
    usable = [c for c in charts if c.get("hits") and c.get("duration")]
    if not usable:
        return []
    # gentler tracks first: fewer percussive hits per second reads as calmer
    def density(c):
        return len(c["hits"]) / max(1.0, c["duration"])
    usable.sort(key=density)

    # Three stages is the point - warm up, build, relentless - so aim for at
    # least three tracks even if that overshoots the requested length a little.
    target = minutes * 60.0
    want = max(3, min(len(usable), int(round(target / 190.0))))
    step = max(1, len(usable) // want)
    chosen = usable[::step][:want] if len(usable) > want else usable[:want]
    if len(chosen) < min(3, len(usable)):
        chosen = usable[:min(3, len(usable))]

    # re-order so the calmest opens and the busiest closes
    chosen.sort(key=density)
    out = []
    for i, c in enumerate(chosen):
        tier = i / max(1, len(chosen) - 1)
        secs = session_arc(c["duration"], c.get("energy") or [], c["hits"],
                           start=c.get("grooveStart", 2.0), tier=tier)
        out.append({
            "name": c["name"], "title": c["title"], "bpm": c["bpm"],
            "duration": c["duration"], "tier": round(tier, 2),
            "label": ["warm up", "building", "relentless"][
                min(2, int(tier * 2.99))],
            "sections": secs,
            "cues": cues(c["hits"], secs),
        })
    return out


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
