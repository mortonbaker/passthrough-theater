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

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$BASE" "$BASE/media" "$BASE/thumbs" "$BASE/player"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 \
    "$(dirname "$0")/deovr_server.py" "$BASE/deovr_server.py"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0644 \
    "$(dirname "$0")/player/index.html" "$BASE/player/index.html"
if [[ -f "$(dirname "$0")/player/three.module.js" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0644 \
        "$(dirname "$0")/player/three.module.js" "$BASE/player/three.module.js"
else
    echo "NOTE: player/three.module.js not present; fetch it once with:"
    echo "  curl -Lo $BASE/player/three.module.js https://unpkg.com/three@0.160.0/build/three.module.js"
fi

# Self-signed cert: the WebXR player needs a secure context, so the server also
# listens on HTTPS when these files exist. Accept the browser warning once.
if command -v openssl >/dev/null && [[ ! -f $BASE/certs/cert.pem ]]; then
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$BASE/certs"
    LAN_IP=$(hostname -I | awk '{print $1}')
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
        -keyout "$BASE/certs/key.pem" -out "$BASE/certs/cert.pem" \
        -days 3650 -nodes -subj "/CN=passthrough-theater" \
        -addext "subjectAltName=IP:$LAN_IP" 2>/dev/null
    chown "$SERVICE_USER:$SERVICE_USER" "$BASE/certs/"*.pem
    chmod 600 "$BASE/certs/key.pem"
    echo "self-signed cert generated for $LAN_IP"
fi

sed "s/__USER__/$SERVICE_USER/g" "$(dirname "$0")/deovr.service" \
    > /etc/systemd/system/deovr.service

systemctl daemon-reload
systemctl enable --now deovr.service
sleep 2
systemctl is-active --quiet deovr.service \
    && echo "deovr running on http://$(hostname -I | awk '{print $1}'):8250" \
    || { echo "failed to start; see: journalctl -u deovr -n 30" >&2; exit 1; }

echo "drop videos in $BASE/media"
