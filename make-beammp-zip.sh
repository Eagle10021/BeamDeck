#!/bin/sh
# Builds beamdeck.zip (the BeamNG mod) for a BeamMP server's Resources/Client folder.
#   ./make-beammp-zip.sh                 -> ./beamdeck.zip
#   ./make-beammp-zip.sh server:path/    -> also copies it there with scp (e.g. myserver:beammp-server/Resources/Client/)
set -e
here=$(cd "$(dirname "$0")" && pwd)
rm -f "$here/beamdeck.zip"
(cd "$here/mod" && zip -q -r -X "$here/beamdeck.zip" lua scripts ui vehicles)
echo "Built $here/beamdeck.zip"
[ -n "$1" ] && scp "$here/beamdeck.zip" "$1" && echo "Copied to $1"
