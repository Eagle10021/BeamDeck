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

systemctl --user daemon-reload
echo "Installed. Turn the helper on with:  systemctl --user enable --now beamdeck"
echo "Then open BeamDeck from the app launcher; BeamNG loads its side on the next start."
