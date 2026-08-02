# Passthrough Theater

A self-hosted VR media server and mixed-reality video player for your LAN. Drop
immersive videos on a Linux box; watch them on a Quest either in any
DeoVR-compatible player or in the built-in WebXR **passthrough player**, which
composites the subject into your real room with chroma keying or a true alpha
matte.

Python standard library plus `ffmpeg` on the server, vendored three.js in the
player. No cloud, no accounts, no app installs, no build step.

Good for VR renders and AI-generated immersive video, 180°/360° camera footage,
and any fisheye or equirect stereo content you want served locally.

## What you get

- **Media server** (`:8250`) — scans a folder, reads projection/stereo layout
  from filenames, serves a browsable thumbnail gallery and a
  DeoVR-compatible JSON API with correct per-file metadata
- **WebXR passthrough player** (`:8253/player/`, HTTPS) — runs in the Quest's
  own browser as an immersive-AR session: your video keyed over the real room,
  laser-pointer menus, per-file settings that persist on the server
- **Sidecar metadata** — chroma key values, projection overrides, chapters, and
  matte mode live in a JSON file next to each video, editable from inside the
  headset

## Quick start

On the server (Debian/Ubuntu-ish, needs `python3`, `ffmpeg`, `openssl`):

```bash
git clone https://github.com/mortonbaker/passthrough-theater.git
cd passthrough-theater
sudo ./install.sh
```

Drop videos in `/srv/deovr/media`, then:

| Client | URL |
| --- | --- |
| Passthrough player (headset browser) | `https://<lan-ip>:8253/player/` |
| DeoVR-compatible players | `http://<lan-ip>:8250` |
| Desktop browser gallery | `http://<lan-ip>:8250` |

The player URL must be the **https** one — WebXR only exists in a secure
context. The install script generates a self-signed certificate; accept the
browser warning once.

No re-indexing, ever: the library rescans per request, durations are cached by
mtime, thumbnails generate on first view.

## Filename conventions

Projection and stereo mode are parsed from the filename, matching the naming
most VR tools already produce:

| Filename contains | screenType |
| --- | --- |
| `MKX200`, `MKX220` | `mkx200`, `mkx220` |
| `FISHEYE190` | `fisheye190` |
| `RF52`, `VRCA220` | `rf52`, `vrca220` |
| `_360` | `sphere` (360°) |
| *(default)* | `dome` (180°) |

Stereo defaults to side-by-side. `_TB` = top/bottom, `_MONO` or `_2D` = flat.

Example: `sunset_flight_2160p_FISHEYE190_alpha.mp4` → fisheye 190°, SBS stereo.

## The passthrough player

Enter on the flat page, then everything is laser + trigger:

| Input | Action |
| --- | --- |
| Trigger on the menu | click buttons, grab and drag sliders |
| Trigger away from the menu | show / hide the menu |
| **Hold** trigger away from the menu | move the menu to where you point |
| **A** / **B** | play-pause / exit (always active) |
| Left stick click | show / hide menu (no-laser fallback) |

The **⚙ popup** holds everything: key colour presets, key strength and
softness, and sliders for yaw, pitch, height, zoom, and distance — so the whole
player is usable with no gestures at all.

**ADV mode** (toggle on the menu bar, remembered across sessions) adds a
gesture layer:

| Gesture | Action |
| --- | --- |
| Hold right grip | **grab the scene** — twist for yaw, tilt for pitch, lift for height; right stick zooms while held |
| Left stick ↑↓ | zoom (hold grip: distance) |
| Left stick ←→ flick | seek ±10 s |
| **X** | step key strength (hold grip: softness) |
| **Y** | reset position |
| Right stick click | save settings to the file |

## Sidecar metadata

`My Video.mp4` reads overrides from `My Video.json` beside it:

```json
{
  "title": "Custom title",
  "screen": "fisheye190",
  "stereo": "sbs",
  "chroma": { "color": "#00ff00", "similarity": 0.12, "smoothness": 0.08 },
  "alphaPack": "tb",
  "chapters": [ { "ts": 0, "name": "Intro" }, { "ts": 95, "name": "Main" } ]
}
```

- `chroma` — the player's key settings for this file. Saving from the headset
  writes this block via `POST /sidecar/<id>`, so you tune once and it sticks.
- `alphaPack: "tb"` — switch from keying to a **true packed matte** (below).
- `chapters` — served to DeoVR-compatible players as `timeStamps`; embedded MP4
  chapters are also read automatically.

## Packed alpha mattes

Chroma keying is a guess; a matte is the answer. If your pipeline produces a
matte pass (renderers and segmentation models can emit one directly), pack it
under the colour pass:

```bash
ffmpeg -i color.mp4 -i matte.mp4 -filter_complex "[0][1]vstack" \
  -c:v libx265 -crf 18 -movflags +faststart out.mp4
```

Frame layout: colour on top, greyscale matte (white = opaque) directly below,
same width, double height. Set `"alphaPack": "tb"` in the sidecar and the
player samples the matte for alpha instead of keying. Hair, shadows, and dark
edges survive; nothing is guessed.

## Faststart

MP4s written without `-movflags +faststart` put their index after the video
data, which breaks streaming seeks and end-of-file behaviour. Many encoders do
this by default. The server logs a warning naming affected files; fix them all
in place (lossless remux):

```bash
./optimize.sh /srv/deovr/media
```

## API

| Route | Purpose |
| --- | --- |
| `GET /` | thumbnail gallery (HTML) |
| `GET /deovr` | DeoVR-compatible index JSON |
| `GET /video/<id>` | per-video JSON: projection, stereo, codec, chroma, chapters |
| `GET /thumb/<id>.jpg` | poster frame (generated once, left eye) |
| `GET /media/<name>` | the file, with HTTP Range / 206 support |
| `GET /player/` | the WebXR player |
| `POST /sidecar/<id>` | merge a JSON body into the video's sidecar |

URLs are built from the request's Host header and scheme, so LAN IPs,
hostnames, and reverse proxies all work unconfigured.

## Service

```bash
sudo systemctl status deovr
journalctl -u deovr -f
```

Config is a handful of constants at the top of `deovr_server.py` (media dir,
ports, cert paths). HTTPS activates automatically when `certs/cert.pem` and
`certs/key.pem` exist.

## Caveats

- **No auth.** Anything on your LAN can browse and stream the library, and
  `POST /sidecar/<id>` accepts writes without credentials. Sidecar input is
  key-whitelisted so it cannot redirect file paths, but treat the whole service
  as trusted-network-only: run it on a LAN you control or behind a VPN such as
  Tailscale. Do not port-forward it.
- Everything binds `0.0.0.0`, because headsets generally have no VPN client and
  must reach the player over the LAN. If your headset *can* join your VPN, bind
  to that interface instead.
- **No transcoding.** Files must already be decodable by the headset — for a
  Quest that means H.264/H.265 MP4. 4K+ HEVC in the browser player depends on
  hardware decode.
- Thumbnails are a single frame from 20% in.
- The player needs the self-signed cert accepted once per browser.

## License

MIT
