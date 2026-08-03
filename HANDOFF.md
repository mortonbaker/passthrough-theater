# Handoff — Passthrough Theater

Written 2026-08-03. Everything below is verified unless marked otherwise.

Repo: `mortonbaker/passthrough-theater` (public), local `C:\Code\deovr-lan`.
Also cloned on atlas01 at `~/Code/passthrough-theater` (2026-08-03 session
deployed from there — atlas01 is the deploy target, so the deploy.sh steps were
run locally: parse checks, copy, restart, verify). Deployed build stamp
`1785761038` (this commit).

2026-08-03 changes, all **unverified in the headset**:
- `micOn` was never declared — every mic-button click threw a ReferenceError
  (seen in the client log) and widgetClick is now wrapped so one broken widget
  can't eat clicks silently.
- Exit is the 🚪 button (operator preference); ✕ hides the menu. A quick
  trigger tap anywhere that isn't a widget toggles the menu, DeoVR-style;
  holds still carry/drag. B still exits; exit paths beacon
  `EXIT via bar` / `EXIT via B` so the log finally shows exit attempts.
- The adv panel's pitch is computed from the bar's top edge (scale-aware),
  because the bar is nearer and wins the raycast wherever they overlap —
  which made the adv panel's lower buttons unclickable.
- Grip-grab yaw/pitch signs flipped: rotation now follows the hand like the
  height mapping always did — the old opposing signs are what read as
  "the axes are messed up". Axis assignments themselves were already canonical
  (yaw=Y, pitch=X, roll=Z, YXZ order — per MDN WebXR docs).
- DeoVR-style bar: title + clock row, scrubber with times at the ends,
  prev/−10/play/+10/next, playback-speed cycle, volume sliders.
- ADV panel: lens (180/360/F190/MKX200/MKX220) and stereo (SBS/TB/2D)
  overrides; `save` now persists screen/stereo/is3d to the sidecar too.
  The UI-size slider was drawn off the bottom edge of the canvas (876–940 on a
  900px canvas) — panel is taller now and rows are tighter.
- TB (over/under) stereo actually renders now; eyeUV previously only knew SBS.
- cleanup() no longer disposes the renderer (the comment always said keep it);
  disposing forced the next entry to create a GL context between session grant
  and attach — the sequence that used to crash the tab. It also resets
  `centered`, so re-entry recenters instead of reusing a stale baseYaw
  (the second session in the client log never logged CENTERED).

---

## Deploying

**Always use `./deploy.sh`.** It parse-checks the player with `node --check` and
the server with `ast.parse`, and refuses to upload if either fails.

This exists because a syntax error shipped once and took the whole page down —
an inline module that fails to parse never runs, which looks like an empty
library, not an error. Brace-counting had passed on that file: the braces were
balanced, the JavaScript was not valid. Don't hand-edit and scp.

Every response carries `X-Build`, and the flat page shows the same number in its
header. Confirm the build before debugging anything.

---

## Architecture

**atlas01** (`100.127.143.78` / `192.168.0.188`, system systemd, passwordless sudo)

| Port | What |
| --- | --- |
| 8250 | media server, HTTP — DeoVR-compatible clients |
| 8253 | same server over HTTPS — **the WebXR player lives here** |
| 8251 | AriaNg UI (aria2 RPC on 6800) |
| 8252 | Chromium container, `morton` / `ifCH2wofxg4Pjl5` |

Media in `/srv/deovr/media` (18 files). Service: `sudo systemctl restart deovr`,
logs `sudo journalctl -u deovr -f`.

**studio-pc** (`100.80.197.38`) — the voice bridge, `:8781`.
Started at logon via `%APPDATA%\...\Startup\voice-bridge.vbs` (hidden, no
console). Registering a scheduled task needs admin; the Startup folder doesn't.

The player is served over HTTPS and cannot call a plain-HTTP backend, so the
server proxies `/voice/*` to `VOICE_BRIDGE`. GET **and** POST are both
forwarded — `sendBeacon` always POSTs.

---

## URLs

Player (Quest, home LAN only — **the headset has no Tailscale**):
```
https://192.168.0.188:8253/player/
```
Accept the self-signed cert once. WebXR does not exist on plain HTTP.

Everything else, tailnet-first (the operator is usually away from home):
`https://atlas01.tail00ae77.ts.net` (remote browser),
`http://100.127.143.78:8251` (downloader), `http://100.70.209.20:8251` (spark).

Older builds are still deployed for bisecting: `/player/legacy.html` (`8d4924a`,
known good), `t1`–`t5`.

---

## Debugging in the headset

There is no console. The player POSTs to `/clientlog`; read it with:

```bash
ssh morton@192.168.0.188 \
  'sudo journalctl -u deovr --since "10 minutes ago" -o cat | grep -a CLIENT:'
```

It emits `STEP A`…`STEP J` through session entry, `CENTERED yaw=N`,
`HEARTBEAT frames=N` every 5s once running, plus window errors and unhandled
rejections. Each render-loop stage is individually guarded, so a fault in input
or drawing cannot stop the scene from rendering.

**Read this log before forming any theory.** Four hypotheses were spent on
guesses that the log later disproved.

---

## Bugs found, and what they teach

1. **`await replyAudio.play()` on an element with no src never settles.**
   Execution stopped mid-entry with no error; the session was never attached and
   the loop never ran. `HEARTBEAT` count of zero is what proved it. A hang looks
   exactly like a crash from the outside.

2. **Panel face culling.** `curvedGeo` wound triangles clockwise as seen from the
   viewer, and the panel material defaulted to `FrontSide`, so the entire UI was
   culled while the video (which uses `DoubleSide`) rendered fine. Raycasting
   honours `material.side` too, so pointers were failing for the same reason.

3. **Recentring contaminated the rotation axes.** `baseYaw` was composed into the
   same `YXZ` Euler as the user's yaw/pitch/roll. Yaw applies first, so at the
   observed `baseYaw` of 64° a pitch of +20° tilts the scene's up vector to
   `(0.31, 0.94, 0.15)` — mostly X, which reads as roll. The controls were never
   mislabelled. `baseYaw` now lives on a parent group (`screenRoot`).

4. **`display:flex` beats `[hidden]`.** The confirm overlay covered the page from
   load, so no video could be tapped. There is now an explicit
   `[hidden]{display:none !important}`.

5. **The hide button undid itself.** `widgetClick` hid the menu on select-start,
   and select-end's "any trigger restores it" rule brought it back on the same
   pull. Presses now record `menuWas`.

6. **Cache headers were absent**, so Chromium cached heuristically. Now
   `no-store` on the player and JSON. (This was *not* the cause of the render
   failures — hashes proved the served bytes matched. I claimed it without
   checking; don't repeat that.)

---

## In-headset controls

**Trigger** — on a button, clicks it. On empty panel space, hold to carry the
menu (follows the ray in yaw *and* pitch). Short click off-panel hides it; with
it hidden, any trigger pull restores it. `✕` on the bar hides it explicitly.

**Left stick** rotate (yaw / pitch), grip + vertical = distance.
**Right stick** zoom / seek ±10s. **Right grip** grab the scene with your hand.
**A** play/pause, **B** exit, **Y** reset + recenter.

Bar rows: transport + clock / scrubber / audio then modes.
`⌖` recenters after a **3-second countdown**, so you can look where you want first.
`ADV` gates the `⚙` panel (progressive disclosure; vanilla stays minimal).

---

## Voice

Whisper → LM Studio → Chatterbox, all on studio-pc.

- Persona is read **live from Open WebUI's SQLite** (`model` table, id
  `stheno-uncensored`, name Mariko). Edit it in Open WebUI; the bridge re-reads
  every 30s. Do not hardcode a prompt — doing that once neutered the character.
- Default voice `serafina` (note the spelling). 40 reference clips in
  `C:\Users\morto\Code\chatterbox-server\voices`.
- A `"voice"` key in a video's sidecar overrides it for that video. A leftover
  `aoi` binding once caused a "wrong voice" report — check sidecars first.
- Replies stream sentence-by-sentence: `POST /talk` returns the first sentence
  plus a job id, `GET /chunk?job&n` long-polls for the rest. First audio ~5s,
  down from ~18s.
- **Push-to-talk**, not hands-free. The energy-based VAD opened the mic for one
  exchange and did not re-arm; it is still in the file behind an early `return`
  if anyone wants to revisit it.

---

## Media pipeline

Filenames carry the projection (`MKX200`, `FISHEYE190`, `RF52`, `_360`, `_TB`,
`_MONO`) and the server parses it. Sidecar `<video>.json` overrides, whitelisted
to `title, screen, stereo, is3d, chapters, chroma, alphaPack, voice` — it used to
merge wholesale, which let `path` be redirected into ffmpeg (arbitrary read +
SSRF) via an unauthenticated endpoint.

**ffmpeg and ComfyUI write MP4s without `+faststart`** (moov after mdat), which
breaks seeking and end-of-video. Run `./optimize.sh /srv/deovr/media`. SLR files
already ship correctly. A systemd path+timer unit on spark
(`~/bin/deovr-sync.sh`) rsyncs `~/video/quest_export` → atlas01 and runs the
remux automatically, filtering to projection-tagged finals so intermediates
never publish.

---

## Open items

**Unverified in the headset** (built, deployed, no verdict yet):
the pitch/roll axis separation, the `✕` hide fix, the three-row bar layout.

**Known limitations**
- Every library video is 4320×2160 HEVC except one at 7680×3840. A small H.264
  test clip (`ZZTEST_small_h264_1920x960_...`) exists for isolating decode
  issues — delete it when done.
- `distance` translates a dome the viewer sits inside, so it reads as
  field-of-view. Arguably it should be renamed rather than fixed.
- No auth anywhere; everything binds `0.0.0.0` because the headset has no VPN
  client. Trusted LAN only. Do not port-forward.
- atlas01's MagicDNS resolves `*.ts.net` to public IPs, not tailnet ones — use
  raw `100.x` addresses from there. Fix would be `tailscale set --accept-dns`.
- Three commits (`36c859c`, `c0742d0`, `6e1196b`) rearranged session entry while
  chasing causes that turned out to be wrong. They are harmless and the
  persistent renderer from the last one is worth keeping, but the entry path
  carries complexity it does not need.

**Not done, deliberately**
- Alpha-matte packing was abandoned: the format is undocumented, the tool is a
  GUI-only .NET binary that resisted automation, and none of the SLR files
  actually contain packed alpha (corner luma maxima 55–78 against ~255 for a real
  matte). Our own `alphaPack: "tb"` format exists instead — colour on top,
  greyscale matte below, one `vstack` in ffmpeg.
- Cloning voices from performers in commercial video: declined.
