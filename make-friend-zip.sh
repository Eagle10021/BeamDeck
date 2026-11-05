#!/bin/sh
# Builds BeamDeck-Windows.zip for a player joining a BeamMP server: the Windows files, the mod, and
# FRIEND-INSTALL.txt with the music relay address filled in (from the BeamDeck settings, or the 2nd argument).
#   ./make-friend-zip.sh [output-folder] [ws://relay:47140]
# By default it goes to dist/ in this folder.
set -e
here=$(cd "$(dirname "$0")" && pwd)
out="${1:-$here/dist}"
relay="$2"
if [ -z "$relay" ]; then
    relay=$(python3 -c "import json,os,sys; print(json.load(open(os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'),'beamdeck','config.json'))).get('mp_relay',''))" 2>/dev/null || true)
fi
[ -n "$relay" ] || { echo "No relay address: set it in BeamDeck's Settings, or pass it as the 2nd argument"; exit 1; }
tmp=$(mktemp -d); pkg="$tmp/BeamDeck"
mkdir -p "$pkg"
cp "$here/beamdeck-server.py" "$here/beamdeck-ui.html" "$here/beamdeck-icon.svg" "$here/beamdeck-launch.py" "$here/install.ps1" "$pkg/"
cp -r "$here/mod" "$pkg/"
sed "s|{{RELAY}}|$relay|" "$here/FRIEND-INSTALL.txt" | sed 's/$/\r/' > "$pkg/FRIEND-INSTALL.txt"   # Windows line endings
mkdir -p "$out"
rm -f "$out/BeamDeck-Windows.zip"
(cd "$tmp" && zip -q -r -X "$(cd "$out" && pwd)/BeamDeck-Windows.zip" BeamDeck)
rm -rf "$tmp"
echo "Built $(cd "$out" && pwd)/BeamDeck-Windows.zip (relay: $relay)"
