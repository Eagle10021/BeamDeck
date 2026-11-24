#!/bin/sh
# BeamDeck installer for Linux: helper + window + launcher + icon + service + the BeamNG mod.
#   Needs: python3, ffmpeg, yt-dlp and a Chromium-based browser (Edge, Chrome, Chromium, Brave, Vivaldi).
#   App sound / Android Auto also need PipeWire or PulseAudio (pactl); Android Auto needs adb (android-tools),
#   Xvfb (xorg-server-xvfb), xdotool and libc++. Now playing from apps needs playerctl.
#   Usage:  ./install.sh            (installs everything; Android Auto's head unit is downloaded too)
#           BEAMNG_USERFOLDER=/path ./install.sh   if BeamNG's user folder isn't found
set -e
here=$(cd "$(dirname "$0")" && pwd)
bin="$HOME/.local/bin"
data="${XDG_DATA_HOME:-$HOME/.local/share}"

# BeamNG's user folder: native, then Proton
user="$BEAMNG_USERFOLDER"
if [ -z "$user" ]; then
    for base in "$data/BeamNG/BeamNG.drive" "$HOME/.local/share/Steam/steamapps/compatdata/284160/pfx/drive_c/users/steamuser/AppData/Local/BeamNG/BeamNG.drive"; do
        if [ -d "$base/current" ]; then user="$base/current"; break; fi
        if [ -d "$base" ]; then user="$base"; break; fi
    done
fi
[ -n "$user" ] || { echo "BeamNG's user folder wasn't found; run with BEAMNG_USERFOLDER=/path/to/it ./install.sh"; exit 1; }
mod="$user/mods/unpacked/beamdeck"

for f in beamdeck-server.py beamdeck-launch.py beamdeck; do install -Dm755 "$here/$f" "$bin/$f"; done
for f in beamdeck-ui.html beamdeck-icon.svg; do install -Dm644 "$here/$f" "$bin/$f"; done
rm -rf "$bin/realroutes" && cp -r "$here/realroutes" "$bin/realroutes"   # Real Routes (bus routes on BeamNG.Earth)
install -Dm644 "$here/beamdeck-icon.svg" "$data/icons/hicolor/scalable/apps/beamdeck.svg"
mkdir -p "$data/applications"   # full path: desktop sessions often don't have ~/.local/bin on PATH
sed "s|@BIN@|$bin|" "$here/beamdeck.desktop" > "$data/applications/beamdeck.desktop"
install -Dm644 "$here/beamdeck.service"  "$HOME/.config/systemd/user/beamdeck.service"

mkdir -p "$mod/beamdeck_media"
cp -r "$here/mod/." "$mod/"

# Google's Android Auto Desktop Head Unit (Android SDK "extras;google;auto"), checked against its SHA-1
dhu="$data/beamdeck/dhu"
if [ ! -x "$dhu/desktop-head-unit" ]; then
    tmp=$(mktemp -d)
    if curl -sfL -o "$tmp/dhu.zip" https://dl.google.com/android/repository/desktop-head-unit-linux-x64_r02.1.zip &&
       echo "5d87eb87a1421e59d528eac1f5cdd406c0b39f51  $tmp/dhu.zip" | sha1sum -c - >/dev/null; then
        mkdir -p "$dhu" && bsdtar -xf "$tmp/dhu.zip" -C "$dhu" && chmod +x "$dhu/desktop-head-unit"
    else
        echo "Android Auto's head unit couldn't be downloaded (everything else still works)."
    fi
    rm -rf "$tmp"
fi

# Real Routes speech: Piper (an offline neural voice) in its own Python environment, and a voice model with
# 904 speakers (one for the announcer, the others for riders). Piper needs Python 3.9-3.13; uv fetches one if needed.
piper="$data/beamdeck/piper"
if ! "$piper/bin/python" -c "import piper" 2>/dev/null; then
    rm -rf "$piper"
    if command -v uv >/dev/null 2>&1; then
        uv venv -q --python 3.12 "$piper" && VIRTUAL_ENV="$piper" uv pip install -q piper-tts
    else
        py=""
        for c in python3.12 python3.13 python3.11 python3.10 python3; do
            if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys; sys.exit(not (3, 9) <= sys.version_info[:2] <= (3, 13))"; then py="$c"; break; fi
        done
        [ -n "$py" ] && "$py" -m venv "$piper" && "$piper/bin/pip" install -q piper-tts
    fi || true
    "$piper/bin/python" -c "import piper" 2>/dev/null ||
        echo "Piper couldn't be installed (needs Python 3.9-3.13 or uv): Real Routes speaks on the phone or tablet instead."
fi
# head tracking with this computer's camera from any screen: a native tracker (realroutes/headtrack.py) in its own
# Python environment with MediaPipe and OpenCV (same Python versions as Piper)
ht="$data/beamdeck/headtrack"
if ! "$ht/bin/python" -c "import mediapipe, cv2" 2>/dev/null; then
    rm -rf "$ht"
    if command -v uv >/dev/null 2>&1; then
        uv venv -q --python 3.12 "$ht" && VIRTUAL_ENV="$ht" uv pip install -q mediapipe opencv-python-headless
    else
        py=""
        for c in python3.12 python3.13 python3.11 python3.10 python3; do
            if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys; sys.exit(not (3, 9) <= sys.version_info[:2] <= (3, 12))"; then py="$c"; break; fi
        done
        [ -n "$py" ] && "$py" -m venv "$ht" && "$ht/bin/pip" install -q mediapipe opencv-python-headless
    fi || true
    "$ht/bin/python" -c "import mediapipe, cv2" 2>/dev/null ||
        echo "The head tracker couldn't be installed: picking the computer's camera from the tablet uses a browser window instead."
fi
voices="$data/beamdeck/voices"; mkdir -p "$voices"
hf=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium
for f in "en_US-libritts_r-medium.onnx 10bb85e071d616fcf4071f369f1799d0491492ab3c5d552ec19fb548fac13195" \
         "en_US-libritts_r-medium.onnx.json b471dc60d2d8335e819c393d196d6fbf792817f40051257b269878505bc9afb3"; do
    set -- $f
    if ! echo "$2  $voices/$1" | sha256sum -c - >/dev/null 2>&1; then
        curl -sfL -o "$voices/$1.part" "$hf/$1" && echo "$2  $voices/$1.part" | sha256sum -c - >/dev/null &&
            mv "$voices/$1.part" "$voices/$1" || { rm -f "$voices/$1.part"; echo "The speech voice $1 couldn't be downloaded."; }
    fi
done

systemctl --user daemon-reload
echo "Installed. Turn the helper on with:  systemctl --user enable --now beamdeck"
echo "Then open BeamDeck from the app launcher; BeamNG loads its side on the next start."
