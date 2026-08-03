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

echo "== checking player syntax =="
# write the extracted module with an explicit encoding: piping through stdout
# fails on Windows, where the default codec cannot represent the UI glyphs
CHECK="$HERE/.syntax-check.mjs"
python - "$HERE/player/index.html" "$CHECK" <<'EOF'
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
python -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$HERE/deovr_server.py" \
  || { echo "server has a syntax error; not deploying" >&2; exit 1; }
echo "   server OK"

echo "== uploading =="
scp -q -o BatchMode=yes "$HERE/player/index.html" "$HOST:$BASE/player/index.html"
scp -q -o BatchMode=yes "$HERE/deovr_server.py"   "$HOST:$BASE/deovr_server.py"

echo "== restarting =="
ssh -o BatchMode=yes "$HOST" "sudo systemctl restart deovr && sleep 2 && systemctl is-active deovr"

echo "== verifying =="
ssh -o BatchMode=yes "$HOST" "curl -sk -o /tmp/_p.html -D /tmp/_h.txt https://127.0.0.1:8253/player/ \
  && grep -i x-build /tmp/_h.txt \
  && python3 -c \"import json,urllib.request,ssl; ctx=ssl._create_unverified_context(); \
     d=json.load(urllib.request.urlopen('https://127.0.0.1:8253/deovr', context=ctx)); \
     print('   videos in manifest:', len(d['scenes'][0]['list']))\""
