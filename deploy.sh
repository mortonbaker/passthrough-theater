#!/usr/bin/env bash
# Deploy the player and server to the media host.
#
# The player is one file with an inline module, so a parse error takes the whole
# page down and shows up as an empty library rather than as an error. Balanced
# braces are not enough to catch that; parse it properly before shipping.
set -euo pipefail

HOST="${DEOVR_HOST:-morton@192.168.0.188}"
BASE="${DEOVR_BASE:-/srv/deovr}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# python3 on Linux, python on Git Bash: this runs from either machine
PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "no python found" >&2; exit 1; }

echo "== checking player syntax =="
# write the extracted module with an explicit encoding: piping through stdout
# fails on Windows, where the default codec cannot represent the UI glyphs
CHECK="$HERE/.syntax-check.mjs"
"$PY" - "$HERE/player/index.html" "$CHECK" <<'EOF'
import sys
s = open(sys.argv[1], encoding='utf-8').read()
js = s.split('<script type="module">', 1)[1].rsplit('</script>', 1)[0]
js = js.replace("import * as THREE from './three.module.js';", "const THREE={MathUtils:{}};")
open(sys.argv[2], 'w', encoding='utf-8', newline='').write(js)
EOF
node --check "$CHECK" || { rm -f "$CHECK"; echo "player has a syntax error; not deploying" >&2; exit 1; }
rm -f "$CHECK"
echo "   player OK"

echo "== checking server syntax =="
"$PY" -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$HERE/deovr_server.py" \
  || { echo "server has a syntax error; not deploying" >&2; exit 1; }
echo "   server OK"

echo "== uploading =="
if [ -d "$BASE" ]; then                      # running on the target itself
  install -m 0644 "$HERE/player/index.html" "$BASE/player/index.html"
  install -m 0755 "$HERE/deovr_server.py"   "$BASE/deovr_server.py"
  [ -f "$HERE/chart.py" ] && install -m 0755 "$HERE/chart.py" "$BASE/chart.py"
  sudo systemctl restart deovr && sleep 2 && systemctl is-active deovr
else
  scp -q -o BatchMode=yes "$HERE/player/index.html" "$HOST:$BASE/player/index.html"
  scp -q -o BatchMode=yes "$HERE/deovr_server.py"   "$HOST:$BASE/deovr_server.py"
  [ -f "$HERE/chart.py" ] && scp -q -o BatchMode=yes "$HERE/chart.py" "$HOST:$BASE/chart.py"
  ssh -o BatchMode=yes "$HOST" "sudo systemctl restart deovr && sleep 2 && systemctl is-active deovr"
fi

echo "== verifying =="
verify() {
  curl -sk -o /dev/null -D /tmp/_h.txt https://127.0.0.1:8253/player/
  grep -i x-build /tmp/_h.txt
  "$1" -c "import json,urllib.request,ssl
ctx = ssl._create_unverified_context()
def get(p):
    return json.load(urllib.request.urlopen('https://127.0.0.1:8253' + p, context=ctx))
print('   videos:', len(get('/deovr')['scenes'][0]['list']))
t = get('/music')['tracks']
print('   tracks:', len(t), '(' + str(sum(1 for x in t if x.get('charted'))) + ' charted)')"
}
if [ -d "$BASE" ]; then verify "$PY"
else ssh -o BatchMode=yes "$HOST" "$(declare -f verify); verify python3"
fi
