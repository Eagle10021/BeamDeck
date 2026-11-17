#!/usr/bin/env bash
# Reads the tablet's Material You / One UI palette over adb and gives it to the Real Routes helper, so the console
# on the tablet uses the tablet's own colours. Run again after changing the wallpaper or colour palette.
#   tools/tablet_colours.sh [adb-serial] [helper-url]
set -euo pipefail
serial=${1:-}
helper=${2:-http://127.0.0.1:47152/routes}
adbs=(adb); [ -n "$serial" ] && adbs=(adb -s "$serial")
json=$("${adbs[@]}" shell settings get secure theme_customization_overlay_packages)
argb=$(grep -o '"android.theme.customization.system_palette":"[0-9a-fA-F]*"' <<<"$json" | grep -o '[0-9a-fA-F]\{8\}"$' | tr -d '"')
[ -z "$argb" ] && argb=$(grep -o '"android.theme.customization.accent_color":"[0-9a-fA-F]*"' <<<"$json" | grep -o '[0-9a-fA-F]\{8\}"$' | tr -d '"')
if [ -z "$argb" ]; then echo "No palette found on the tablet." >&2; exit 1; fi
seed="#${argb:2}"
curl -fsS -X POST -H 'Content-Type: application/json' -d "{\"seed\":\"$seed\",\"device\":\"tablet\"}" "$helper/api/theme" >/dev/null
echo "Tablet colour $seed sent to the helper."
