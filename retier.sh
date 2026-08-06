#!/usr/bin/env bash
# Re-tier an over-budget video to the proven-playable envelope and install it.
# Runs on spark (NVENC). The headset decodes HEVC 4320x2160@60 (every working
# library file) but not 5800x2900@60 (~1000 MP/s); same codec, same profile —
# the delta is pure decoder throughput. So: NVDEC in, CUDA scale, NVENC out at
# a bitrate that beats SLR's own starved 2160p tier, faststart for the
# browser, then install on atlas01 and retire the original to masters/.
#
#   retier.sh "<exact basename.mp4>"            full transcode + swap
#   retier.sh "<exact basename.mp4>" --sample   3-minute ZZSAMPLE for a quick
#                                               headset check, no swap
set -euo pipefail

ATLAS="morton@100.127.143.78"
HTTP="http://100.127.143.78:8250"
MEDIA="/srv/deovr/media"
SRC="${1:?usage: retier.sh <basename.mp4> [--sample]}"
MODE="${2:-full}"

OUT="${SRC//2900p/2160p}"
[ "$OUT" = "$SRC" ] && OUT="${SRC%.mp4}_2160p.mp4"
[ "$MODE" = "--sample" ] && OUT="ZZSAMPLE_${OUT}"

ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$SRC")
TMPDIR="$HOME/theater-scenes/transcode"
mkdir -p "$TMPDIR"
TMP="$TMPDIR/$OUT"

ARGS=()
[ "$MODE" = "--sample" ] && ARGS=(-t 180)

echo "== transcoding: $SRC -> $OUT"
ffmpeg -y -v warning -stats \
  -hwaccel cuda -hwaccel_output_format cuda \
  -i "$HTTP/media/$ENC" "${ARGS[@]}" \
  -vf scale_cuda=4320:2160 \
  -c:v hevc_nvenc -preset p5 -rc vbr -b:v 20M -maxrate 30M -bufsize 60M \
  -tag:v hvc1 -c:a copy -movflags +faststart \
  "$TMP"

echo "== installing on atlas01"
scp -q "$TMP" "$ATLAS:$MEDIA/$OUT"
rm -f "$TMP"

if [ "$MODE" != "--sample" ]; then
  echo "== retiring original to masters/, carrying scenes sidecar if present"
  ssh "$ATLAS" "SRC=\"$SRC\" OUT=\"$OUT\" python3 - <<'PY'
import json, os, shutil, urllib.parse, urllib.request
src, out = os.environ['SRC'], os.environ['OUT']
media, base = '/srv/deovr/media', '/srv/deovr'
os.makedirs(os.path.join(base, 'masters'), exist_ok=True)
# ids keyed by REAL filename via each entry's media URL - titles are
# prettified (underscores become spaces) and must never be used as filenames
ids = {}
lst = json.load(urllib.request.urlopen('http://127.0.0.1:8250/deovr'))['scenes'][0]['list']
for it in lst:
    vid = it['video_url'].rsplit('/', 1)[-1]
    d = json.load(urllib.request.urlopen('http://127.0.0.1:8250/video/' + vid))
    fn = urllib.parse.unquote(d['encodings'][0]['videoSources'][0]['url'].rsplit('/', 1)[-1])
    ids[fn] = vid
old, new = ids.get(src), ids.get(out)
sc = os.path.join(base, 'scenes')
if old and new and os.path.exists(os.path.join(sc, old + '.json')):
    shutil.copyfile(os.path.join(sc, old + '.json'), os.path.join(sc, new + '.json'))
    print('scenes sidecar carried', old, '->', new)
shutil.move(os.path.join(media, src), os.path.join(base, 'masters', src))
print('original retired to masters/')
PY"
fi
echo "== done: $OUT"
