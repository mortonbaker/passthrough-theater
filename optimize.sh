#!/usr/bin/env bash
# Move the moov atom to the front of every MP4 in the media directory.
#
# Files written by ffmpeg/ComfyUI without -movflags +faststart put their index
# after the video data. A streaming player then can't resolve duration or seek
# points until it has pulled the whole file, which shows up as broken seeking
# and odd behaviour at the end of playback.
#
# Remux only, no re-encode: lossless and fast.
set -euo pipefail

MEDIA="${1:-/srv/deovr/media}"
[[ -d $MEDIA ]] || { echo "no such directory: $MEDIA" >&2; exit 1; }

needs_fix() {
    python3 - "$1" <<'EOF'
import os, sys, struct
p = sys.argv[1]
try:
    size = os.path.getsize(p)
    with open(p, "rb") as f:
        off = 0
        while off < size:
            f.seek(off)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            box = struct.unpack(">I", hdr[:4])[0]
            typ = hdr[4:8].decode("latin1")
            if box == 1:
                box = struct.unpack(">Q", f.read(8))[0]
            if box < 8:
                break
            if typ == "moov":
                sys.exit(1)   # already streamable
            if typ == "mdat":
                sys.exit(0)   # index is after the data
            off += box
except Exception:
    pass
sys.exit(1)
EOF
}

fixed=0
shopt -s nullglob nocaseglob
for f in "$MEDIA"/*.mp4 "$MEDIA"/*.m4v "$MEDIA"/*.mov; do
    if needs_fix "$f"; then
        echo "remuxing: $(basename "$f")"
        tmp="$(mktemp --tmpdir="$MEDIA" .fs.XXXXXX.mp4)"
        if ffmpeg -nostdin -y -v error -i "$f" -c copy -movflags +faststart "$tmp"; then
            touch -r "$f" "$tmp"
            mv "$tmp" "$f"
            fixed=$((fixed + 1))
        else
            echo "  FAILED, left original untouched" >&2
            rm -f "$tmp"
        fi
    fi
done

echo "done; $fixed file(s) remuxed"
