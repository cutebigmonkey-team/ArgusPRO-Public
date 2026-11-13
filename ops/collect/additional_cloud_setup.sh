#!/usr/bin/env bash
set -euo pipefail
# Jobs use separate hosted runners because grab-site requires an older Python stack.
case "$1" in
  instareelrag)
    upstream=Himesh-29/InstaReelRAG
    revision=ed60b9d355455d97ec18e1863d48138e22bdf6d4 ;;
  parth-dl)
    upstream=parthmax2/parth-dl
    revision=5cc152587688ea01c964885ba78723d1f314b5e2 ;;
  grab-site)
    upstream=ArchiveTeam/grab-site
    revision=0eaf88628f4d8ef5af4df4c13f594606a41de3cd ;;
  granary)
    upstream=snarfed/granary
    revision=$(jq -r .granary.commit ops/collect/remaining-upstreams.json) ;;
  bluesky) exit 0 ;;
  *) exit 2 ;;
esac
git init -q upstream
git -C upstream fetch -q --depth 1 "https://github.com/$upstream.git" "$revision"
git -C upstream checkout -q --detach FETCH_HEAD
test "$(git -C upstream rev-parse HEAD)" = "$revision"
case "$1" in
  instareelrag)
    python -m pip install instaloader==4.15 python-dotenv sqlalchemy faster-whisper==1.2.1
    python -m pip install 'git+https://github.com/parthmax2/parth-dl.git@5cc152587688ea01c964885ba78723d1f314b5e2'
    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends ffmpeg espeak
    echo "PYTHONPATH=$PWD/upstream:$PWD/upstream/database" >> "$GITHUB_ENV"
    ;;
  parth-dl)
    python -m pip install ./upstream
    python -m unittest discover -s upstream/tests -p test_extractors.py
    ;;
  granary)
    python -m pip install ./upstream
    (cd ops/collect && python -m unittest test_remaining.UpstreamTests.test_actual_granary_instagram_converter test_remaining.UpstreamTests.test_actual_granary_facebook_permalink_and_attachments)
    ;;
  grab-site)
    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends libxml2-dev libxslt1-dev libre2-dev liblmdb-dev pkg-config
    # The upstream CLI hardcodes TLS verification off and ignores robots.txt. Restore wpull defaults.
    sed -i '/^\s*"--no-check-certificate",$/d; /^\s*"--no-robots",$/d' upstream/libgrabsite/main.py
    python -m pip install 'setuptools<75' wheel
    GRAB_SITE_NO_CCHARDET=1 python -m pip install --no-binary lxml 'lxml==4.9.4' 'html5-parser==0.4.12' 'google-re2==1.1.20240702' 'lmdb==1.4.1' 'click<8.2' 'websockets<10' 'autobahn<23' 'warcio==1.7.5' ./upstream
    python -c 'import lmdb, html5_parser, re2'
    grab-site --help >/dev/null
    ;;
esac
