#!/usr/bin/env bash
# Install deovr-lan as a systemd service. Run with sudo.
set -euo pipefail

BASE=/srv/deovr
SERVICE_USER="${SUDO_USER:-$USER}"

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo" >&2
    exit 1
fi

for bin in python3 ffmpeg ffprobe; do
    command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$BASE" "$BASE/media" "$BASE/thumbs"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 \
    "$(dirname "$0")/deovr_server.py" "$BASE/deovr_server.py"

sed "s/__USER__/$SERVICE_USER/g" "$(dirname "$0")/deovr.service" \
    > /etc/systemd/system/deovr.service

systemctl daemon-reload
systemctl enable --now deovr.service
sleep 2
systemctl is-active --quiet deovr.service \
    && echo "deovr running on http://$(hostname -I | awk '{print $1}'):8250" \
    || { echo "failed to start; see: journalctl -u deovr -n 30" >&2; exit 1; }

echo "drop videos in $BASE/media"
