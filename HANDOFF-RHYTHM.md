# Handoff — Passthrough Theater / rhythm mode

Written 2026-08-06. Continues the earlier HANDOFF.md in the repo; this covers
the rhythm work added since. Everything below is verified unless marked.

Repo `mortonbaker/passthrough-theater` (public). Deploy host **atlas01**, which
has its own clone at `~/Code/passthrough-theater` — deploy from there, not from
Windows, or that clone goes stale.

---

## Deploying

```bash
ssh morton@192.168.0.188 'cd ~/Code/passthrough-theater && git pull --ff-only && bash deploy.sh'
```

`deploy.sh` parse-checks **both** pages (`player/index.html`,
`player/highway.html`) with `node --check` plus the server with `ast.parse`, and
**refuses to restart over a live headset session** (heartbeats in the last 20s).
Use `FORCE=1` to override. Never scp by hand.

Confirm the running build via `X-Build` header / the number in the page header.

---

## What exists

**Server** (`deovr_server.py`, port 8250 http / 8253 https):

| Route | Returns |
| --- | --- |
| `/music` | charted tracks with bpm, duration, cue count |
| `/chart/<name>` | that track's chart JSON |
| `/track/<name>` | audio, byte-range enabled |
| `/session?mins=N` | an escalating multi-track plan with per-track cue lists |
| `/clientlog?m=…` | client beacons, **GET and POST** (sendBeacon always POSTs) |

**`chart.py`** — analysis and chart generation. Run
`/srv/deovr/chart.py /srv/deovr/music [--force]`. A `chartwatch` systemd timer
runs it every 3 minutes, so new downloads chart themselves.

**MeTube** on atlas01: `https://atlas01.tail00ae77.ts.net:8451`, downloads into
`/srv/deovr/music`. **Gotcha:** playlists land in a *subfolder*, and the scanner
only reads the root — flatten after a playlist download.

**Bench** — `https://192.168.0.188:8253/player/highway.html`. Flat 2D page, same
cue window and audio clock as VR. **Iterate here, not in the headset**; that was
the single biggest speedup in this whole build.

**VR highway** — in `player/index.html`, `♪` on the bar. Lane off to the left at
yaw −42°, notes + spectrum. Plays one track, or a full `/session` plan (see
addendum).

---

## How charting works

1. `aubio.tempo` → beat grid; `aubio.onset("specflux")` → transients.
2. Per-beat energy in kick (40–140 Hz) and snare/hat (1.5–9 kHz) bands.
3. A beat becomes a **hit** only with an onset within 70 ms *and* real
   percussive energy — the tempo grid runs through intros, drums don't.
4. `first_groove` = first sustained run of 6 hits → the session starts there,
   and playback seeks to ~2.5 s before it. Retention varies hugely by track
   (17% for a piano/vocal track, 76% for four-on-the-floor); that's correct.
5. `downbeat_phase` — the phase of every 4 beats with the strongest kicks.
6. `session_arc` lays down rounds: climb a ladder, hold, stop dead, recover.
7. `cues()` places cues on **metrical positions**, not by stopwatch.

### The intensity ladder

`RUNGS = [4.0, 3.0, 2.0, 1.5, 1.0, 0.7, 0.5, 0.35, 0.25]` (seconds between
cues). `MIN_GAP = 0.22` is a hard floor enforced on the finished list.

**0.25 s is the ceiling, set by operator testing** — four cues per beat was
"not humanly possible". Don't raise it without re-testing.

`MULTIPLES = [8, 4, 2, 1, 0.5]` beats per cue. Each section picks the multiple
closest to its intended pace and steps whole beats from a downbeat; section
edges snap to bar lines. Eighths only subdivide where something actually plays
on the offbeat.

### Session escalation

`plan()` orders tracks by percussive density, assigns each a tier from its
position (0 → 1), and re-cues at that tier. Early tracks work the bottom of the
ladder, only the last reaches 0.25 s. Recovery shortens as it escalates.
Always ≥3 tracks so warm up / building / relentless all appear, even if that
overshoots the requested minutes.

Design basis: a peak only reads as a peak against the valley before it, so each
round starts higher while still dropping to nothing between. Full stops are
deliberate.

---

## Debugging

No console in the headset, and the bench reports too. Read both:

```bash
ssh morton@192.168.0.188 \
  'sudo journalctl -u deovr --since "10 minutes ago" -o cat | grep -a CLIENT:'
```

VR emits `STEP A…J`, `CENTERED yaw=N`, `HEARTBEAT frames=N` every 5 s,
`RHY loaded/start`, `RHY diag …`. Bench emits `BENCH …` for every step.

**Read the log before forming a theory.** Multiple hypotheses this session were
killed by it after being asserted first.

---

## Bugs found here, and what they teach

1. **`onended` race.** `play()` calls `stop()`; stopping a buffer source fires
   its `onended` *asynchronously*, landing after the next source set
   `playing=true` and flipping it false. Audio continued, every draw gated on
   the flag went blank. **Detach the handler before stopping.** Only appeared
   once session mode made tracks overlap.

2. **Black notes.** `vertexColors:true` is what makes the fragment shader apply
   `vColor`, but it also makes the vertex shader multiply by a `color`
   attribute. The geometry had none → reads `(0,0,0)` → notes rendered solid
   black on a dark lane. Give instanced geometry a white `color` attribute.

3. **Empty lane ≠ broken.** A track whose groove starts at 25 s produced no
   cues for 25 s. Skip to the groove.

4. **Scripted edits matched the wrong line — twice.** An 8-space-indented search
   string is a substring of the 12-space version, so `replace` silently hit the
   planner and deleted its `cues` field. **Match on unique text, not
   whitespace-prefixed code.**

5. **`deploy.sh` checked only one page.** The guard built to stop syntax errors
   shipped a broken `highway.html` because it wasn't in the check loop.

6. **Deployed over a live session**, dropping every in-flight connection. Hence
   the heartbeat guard.

7. **CRLF from a Windows checkout** broke `chart.py`'s shebang
   (`env: 'python3\r'`). `.gitattributes` forces LF now.

---

## Current state (as written 2026-08-06, morning)

**Library:** 14 tracks from the operator's playlist, all charted. The previous
set was deleted at their request.

**Verified working:** charting, percussion filtering, groove detection,
metrical placement, `/session` planning, bench session playback with
auto-advance and inter-track gap.

**Untested by the operator:** metrical cue placement. It charts correctly and
the beat-gap maths verifies (gaps on whole/half beats), but they hit an
unrelated crash before hearing it.

## Still open from the earlier handoff

Pitch/roll axis separation, the `✕` hide fix, and the three-row bar are all
deployed but **never confirmed in the headset**.

---

# Addendum — 2026-08-06, later session

Shipped after the handoff above was written. Deployed build `1786007349`
(commit `888ca25`).

**The "unrelated crash" is identified and was already fixed.** The client log
shows it plainly: `chart.cues is not iterable` looping on the bench — the
planner had stopped emitting `cues` (bug 4 above), and `d4dac2f` restored
them. Post-fix beacons show two single tracks and a 3-track session playing
through cleanly. Nothing left to fix there; the metrical charts still await
the operator's ear.

**The VR player now consumes `/session`** — the top item of the open work.
The bench's runner, ported into the rhythm module of `player/index.html`:

- A `session` button on the rhythm panel fetches `/session?mins=N`, plays
  each plan track from ~2.5 s before its groove with the plan's own tier-cued
  list, rests 10 s between tracks, and advances to the end. A `mins` button
  cycles 7/10/15/20 (persisted). The status line carries the stage
  (`warm up · 1/3 · …`); the bar's `♪` stays lit through the gaps.
- `rhyStart(from)` gained the seek offset and `rhyTime()` adds it back — the
  VR clock previously always started at 0, so groove-skipping was impossible.
- Hardening the bench lacked: a generation token (`rhyRun`) invalidates
  in-flight decodes and pending gap timers when a run is stopped or replaced;
  leaving VR stops the session (in `cleanup`); a track ending naturally now
  restores the video volume it ducked.
- The bench got the matching fixes: `stopSession` clears its pending gap
  timer, and picking a track by hand exits session mode.

Smoke-tested headlessly after deploy: the bench planned and played a session
(`session: 11.6 min · 3 tracks`, warm-up cues on the lane, no errors in the
journal), and the VR flat page renders the full grid on the new build. **The
in-headset path — rhythm panel → session — is untested**, as is everything in
the "still open" list above.

Windows clones: `C:\Code\deovr-lan` is the documented local clone and made
this commit. A duplicate accidentally cloned to `C:\Code\passthrough-theater`
the same day was removed.

## Addendum 2 — same day: impact bursts at the strike line

Research pass on how Guitar Hero decides note placement (RBN/C3 authoring
docs, customs-community guides, GH highway references), distilled:

1. **Tempo map first** — a hand-authored beat map aligns the MIDI grid to the
   song's real beats; everything else snaps to that grid (subdivisions: 1/1
   down through 1/16 and triplets). Our aubio grid + `downbeat_phase` +
   `MULTIPLES` is the same architecture, automated.
2. **Charts are transcriptions** — gems follow what the instrument actually
   plays, hand-charted per difficulty; lower tiers keep the strong beats and
   drop subdivisions. Our percussive-hit filter and tier ladder mirror this;
   the gap is that GH follows one musical line where we follow band energy.
3. **Highway grammar** — beat lines (brighter at measures) ride the highway;
   notes that reach the strike line and are hit **terminate there with a
   flame**; only a **miss** slides past the line. A note running under the
   line is the miss signal.

Point 3 was the operator's complaint: every cue ran past the line into the
spectrum bars — permanent-miss language. Build `1786007997`:

- A note now dies exactly at its cue time. An impact burst takes over at the
  line: kind-colored bar expanding outward with a detaching white ring, mostly
  transparent so incoming notes stay readable, ~0.28 s life; the strike line
  thickens and glows with each hit. Bench canvas and VR (12-instance pool,
  same white-`color`-attribute trap as the notes), both timed off the audio
  clock via a monotone cue pointer that resets on chart change or backward
  seek.
- Verified on the bench headlessly: burst frame captured mid-flare, notes
  never render below the line, no client errors.

Follow-ups the research suggests, not built: beat/measure lines riding the
highway (session payload would need `beats`), real hit detection with
differentiated hit-vs-miss feedback, an optional on-beat audio tick.

## Addendum 3 — same day: phrased cues

Operator heard it and called the flaw: every section was a uniform stream —
"the yellow and the red are on the same beat" (true mechanically: at high bpm
the double and fast rungs both map to every-beat), no peaks and valleys
inside a section. `cues()` now lays bar-level phrases over the grid:

- every density has holes and answers (skip bars, pickup bars, breath bars);
- **yellow** settles into its pace, then every once in a while throws one
  offbeat or drops one beat — acceleration without overwhelm;
- **red** is a scripted arc, per the operator's spec: on-off pairs with air
  between them → pairs closing up → a crescendo of straight eighths → a dead
  bar → a syncopated "on on on-off" resume. Eighth-told where the offbeat
  clears MIN_GAP (≲136 bpm), the same shape told in whole beats above that.

Also found while verifying: **the offbeat gate had never worked.** It checked
midpoints against the percussive BEAT grid, where a midpoint can never sit —
a null gate; no offbeat ever survived charting, in any prior build. Charts
now store the raw `onsets` list and gate offbeats against that (old charts
without the field let offs through instead of silently killing them).

All 14 tracks recharted with `--force`. Verified bar-by-bar from the chart
JSON: Fatso (130 bpm) fast = `[0,.5] · [2,2.5] · [0,.5,2,2.5] · eighths ·
eighths · [] · [0,1,2,2.5]`, min gap 0.231 s; S3RL (178) runs the beat-told
version; yellow shows the 2.5 spice. Build `1786008959`. Everything from
addenda 1–3 still awaits the headset.

## Addendum 4 — same day: menu fixes after first headset feedback

Operator confirmed the rhythm feel ("spot on") and reported the `✕` bug was
still real: it hid the lasers, never the menu. Root cause: input and canvas
drawing were gated on `menuOn`, but nothing ever set the panel MESHES
invisible. Meshes now follow `menuOn` every frame; `holder.visible` remains
the logical open/closed state per panel. Build `1786010098`, plus:

- **Grab pill** (SLR/Horizon-OS pattern): a visible grip pill at the top edge
  of the bar and browse panels; point-hold carries the menu (the old
  empty-space carry still works — the pill is the discoverable spot).
- **Rhythm list pages**: prev/next + "page x/y · N tracks" under the rows;
  all 14 tracks reachable; a running session flips to its track's page.
- Playlists: deliberately NOT built in VR. Recommendation on record: curate
  order on the flat page or via MeTube playlist folders; VR gets play, page,
  session. Planner-order question (density vs playlist order) still open.

## Addendum 5 — same day: the invisible notes were frustum culling

Operator, in the headset: no colored bars on the VR lane, only a white
splash at the line and the EQ. The log discriminated it in one line —
`RHY notes visible=1 at t=21.6` — notes were *placed and counted*, so they
rendered invisibly rather than never existing. Cause: an `InstancedMesh` is
frustum-culled by its **base geometry's** bounding sphere, which for a note
is a ~18 cm disc at the group origin — the wearer's head — while the
instances sit 1.45 m away on the lane. Lean off the anchor and three.js
culls the whole mesh. The bursts shared the trap (hence line-pulse-only
"splash"); the spectrum bars survived only because their base geometry is a
metre tall. `frustumCulled = false` on all three. Build `1786010756`.

Lesson for the file: **the bench cannot catch three.js bugs** — it draws the
same cue window on a flat canvas. Anything about meshes, materials,
culling, or stereo needs the headset (or a WebXR-emulated browser run).

## Addendum 6 — same day: scene transcripts ground the voice

The operator asked for the companion to know what he is watching. Built as a
batch pipeline, not live vision:

- `scenes_worker.py` runs on **spark** (DGX, clone at
  `~/Code/passthrough-theater`): one eye-cropped frame every 30 s per video,
  fetched over the tailnet (**spark cannot reach atlas01's LAN address** —
  use 100.127.143.78), captioned by
  `huihui_ai/qwen2.5-vl-abliterated:7b-instruct` on spark's ollama
  (~6–9 s/frame), POSTed to `/scenes/<id>`.
- Server: GET/POST `/scenes/<id>` (JSON files in `/srv/deovr/scenes/`),
  `GET /scenectx?vid=&vt=` for debugging, and the voice proxy resolves
  vid+vt → scene text → `&scene=` param forwarded to the bridge.
- **The live voice bridge is spark's `theater-voice.service`**
  (whisper→ollama Stheno→chatterbox) — NOT studio-pc's voice_bridge.py; the
  deovr unit sets `VOICE_BRIDGE=http://100.70.209.20:8781`. The bridge file
  is tracked in-repo at `voice/server.py`; deploying it = `install` to
  `~/theater-voice/server.py` on spark + `systemctl --user restart
  theater-voice`. Never scp it from a Windows checkout (CRLF, lesson 7).
  (Studio-pc's dormant bridge got the same scene support, harmlessly.)
- Player sends `vid` + `vt` (floored currentTime) with every voice turn.
- Verified end-to-end on the 5-min yuki film: 11 scenes, `/scenectx?vt=150`
  returns the scene on screen at 2:30, bridge healthy on the grounded build.
- Full-library batch on spark: log `~/theater-scenes/logs/batch.log`, ntfy
  pings the operator on completion. A stray `.fs.*` temp file in the media
  dir 404s and is skipped — harmless.

Build `1786012205`. Headset test: play a transcribed video, ask Mariko about
what's on screen.

## Addendum 7 — same day: the 2900p files exceed the headset's decode budget

Two new SLR downloads (5800×2900@60) load metadata but never play. Not a
codec-family problem — the working library files are **also HEVC Main
yuv420p@60**; the only delta is pixel rate (~1009 MP/s vs ~560). The
headset's HEVC decoder ceiling sits between those. Fix: `retier.sh` (repo)
re-tiers on spark's NVENC to the proven envelope — HEVC 4320×2160@60 at
20 Mbps VBR (better than SLR's own 6 Mbps 2160p tier), `hvc1` tag,
faststart — installs on atlas01, retires the original to
`/srv/deovr/masters/`, and carries the scenes sidecar to the new id.
Transcode speed ~1.25× realtime while the scenes batch owns the compute
side. `ZZSAMPLE_*` = 3-minute envelope check in the library.

**Trap found on the way (lesson): `/deovr` titles are prettified —
underscores become spaces. Never match or reconstruct filenames from
titles; resolve the real name via `/video/<id>` →
`encodings[0].videoSources[0].url`.** Both retier and its runner do this
now.

Ingest guidance: SLR tiers at or under 4320×2160@60 play natively; anything
bigger needs a retier pass (`~/theater-scenes/run_retier.sh` on spark does
every `_2900p_` file and pings ntfy per file).

Remaining open work, unchanged in substance:

- **Planner ordering** — density over the *charted region* only, or the
  operator's suggested plain playlist order. Operator's call; not built.
- **No hit detection or scoring.**
- **Phrase length** for pattern changes is still 1 bar; 4 or 8 is likely more
  musical.
- **Peaks aren't aligned to the track's own drops.**
- **VR rhythm panel lists only the first 7 tracks** (`RHY_ROWS = 7`, library
  is 14, no paging).
