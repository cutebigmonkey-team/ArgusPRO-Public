#!/usr/bin/env bash
set -euo pipefail
tool=$1
[[ "$GITHUB_ACTIONS" == true && "$RUNNER_ENVIRONMENT" == github-hosted ]]
root="$PWD/.social-tools"
mkdir -p "$root"
PYTHONPATH=ops/collect python - "$tool" "$root" <<'PY'
import json, sys
from pathlib import Path
from setup_social import checkout
name = sys.argv[1]
pins = json.loads(Path('ops/collect/cloud-adapter-pins.json').read_text())
checkout(Path(sys.argv[2]), {name: pins[name]})
PY
dir="$root/$tool"
case "$tool" in
  ckelepel)
    # ARGUS needs an unknown publication date to remain unknown.
    sed -i 's@post.taken_at || Math.floor(Date.now() / 1000)@post.taken_at || null@' "$dir/src/normalizers.js"
    npm ci --prefix "$dir" --ignore-scripts --no-audit --no-fund ;;
  sipher)
    python -m pip install playwright==1.55.0
    python -m playwright install chromium ;;
  threads-actor)
    npm install --prefix "$dir" --ignore-scripts --no-audit --no-fund
    # Preserve the author observed in GraphQL when the profile contains quotes or suggestions.
    sed -i 's/formatGraphQLPost(p, username)/formatGraphQLPost(p, p.user?.username || "")/g' "$dir/src/main.js"
    ;;
  socialcrawl)
    npm ci --prefix "$dir" --ignore-scripts --no-audit --no-fund
    npm run --prefix "$dir" build ;;
  marketpulse)
    ln -s Core "$dir/core"
    python -m pip install httpx==0.28.1 ;;
  facebook-page-info) python -m pip install requests==2.32.5 ;;
  superbrain) python -m pip install instaloader==4.14.2 ;;
  vdite)
    python -m pip install playwright==1.55.0 parsel==1.10.0 nested-lookup==0.2.25
    python -m playwright install chromium ;;
esac
