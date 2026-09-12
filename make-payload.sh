#!/bin/bash
# Rebuilds Sandbox-Install-Payload-3.0.tar.gz from the Sandbox-Install-Payload-3.0 folder.
# The folder keeps the wrapper under src/SARndbox-3.0, but the installed tree is
# ~/src/SARndbox-2.8 (that is where sandbox-install-3.0.sh builds SARndbox), so the
# tarball is written with src/SARndbox-2.8 paths. Scripts and desktop files are
# made executable inside the tarball.
set -e
cd "$(dirname "$0")"
SRC=Sandbox-Install-Payload-3.0
OUT=Sandbox-Install-Payload-3.0.tar.gz
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

cp -a "$SRC/." "$STAGE/"
if [ -d "$STAGE/src/SARndbox-3.0" ]; then
    mv "$STAGE/src/SARndbox-3.0" "$STAGE/src/SARndbox-2.8"
fi
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
# Never ship build output: the installer must compile SandboxHelper against the Vrui on that
# machine (a stale .so would look newer than the source and make would skip the build).
find "$STAGE" \( -name '*.so' -o -name '*.o' \) -delete
find "$STAGE" \( -name '*.sh' -o -name '*.py' -o -name '*.desktop' \) -not -path '*/autostart/*' -exec chmod 755 {} +

tar czf "$OUT" -C "$STAGE" --owner=sandbox --group=sandbox .config Desktop Pictures src
echo "Wrote $OUT"
tar tzf "$OUT" | grep -v '/$'
