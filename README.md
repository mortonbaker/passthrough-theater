# deovr-lan

A small DeoVR-compatible media server for streaming VR video off your own LAN to a
headset. Point DeoVR at the box, get a thumbnail grid, play with the right lens and
stereo mode. No cloud, no account, no scraping.

Python standard library only — no pip install, no Docker, no Node.

## Why this exists

[XBVR](https://github.com/xbapps/xbvr) and [deovr-local-server](https://github.com/clear-gh/deovr-local-server)
already cover this ground and cover it well. Use XBVR if you want a real library
manager with scraped metadata, cast, and tags.

This is the other end of the spectrum: one file, no dependencies, reads projection
straight off the filename, and serves byte ranges correctly so seeking in a 4K
fisheye file doesn't stall. It exists because a systemd unit and 300 lines of stdlib
were less work than maintaining a Docker stack for a job this small.

## Install

On the server (Debian/Ubuntu, needs `ffmpeg` and `python3`):

```bash
git clone https://github.com/mortonbaker/deovr-lan.git
cd deovr-lan
sudo ./install.sh
```

Drop videos in `/srv/deovr/media`, then open `http://<lan-ip>:8250` in DeoVR's
address bar.

Nothing to re-index — the library is rescanned per request. Durations are cached by
mtime and thumbnails are generated once on first view.

## Projection detection

Projection and stereo mode are read from the filename, so the naming most VR sites
already use works untouched.

| Filename contains | screenType |
| --- | --- |
| `MKX200`, `MKX220` | `mkx200`, `mkx220` |
| `FISHEYE190` | `fisheye190` |
| `RF52`, `VRCA220` | `rf52`, `vrca220` |
| `_360` | `sphere` |
| *(default)* | `dome` (180°) |

Stereo defaults to SBS. `_TB` gives top/bottom, `_MONO` or `_2D` gives flat 2D.

To override a file that guesses wrong, put a JSON sidecar next to it —
`My Video.mp4` gets `My Video.json`:

```json
{ "screen": "fisheye190", "stereo": "tb", "title": "Custom title" }
```

## API

| Route | Returns |
| --- | --- |
| `GET /` or `/deovr` | DeoVR index JSON (scene list) |
| `GET /video/<id>` | Per-video JSON with projection metadata |
| `GET /thumb/<id>.jpg` | Poster frame, left eye, generated on demand |
| `GET /media/<name>` | The file, with `Range` / `206` support |

URLs are built from the request's `Host` header, so the same server works over LAN
IP, hostname, or a Tailscale address without reconfiguration.

## Service

```bash
sudo systemctl status deovr
sudo systemctl restart deovr
journalctl -u deovr -f
```

Config is four constants at the top of `deovr_server.py` (`MEDIA_DIR`, `THUMB_DIR`,
`CACHE_PATH`, `PORT`).

## Caveats

- No auth. It binds `0.0.0.0` and serves anything in the media directory. Keep it on
  a trusted LAN, or put it behind Tailscale.
- No transcoding. Files must already be in a format the headset decodes — for a
  Quest 3 that means H.264 or H.265 MP4.
- Thumbnails come from a single frame 20% into the file.

## License

MIT
