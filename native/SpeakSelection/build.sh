#!/bin/bash
# Build the menu-bar app bundle: `build/SpeakSelection.app`.
# `swift run SpeakSelection` also works for a quick headless dev loop; the
# bundle is what carries LSUIElement, the bundle id, and a stable identity for
# the Accessibility grant.
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release

APP=build/SpeakSelection.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp .build/release/SpeakSelection "$APP/Contents/MacOS/SpeakSelection"
cp Resources/Info.plist "$APP/Contents/Info.plist"

# Sign with a stable identity so the Accessibility grant survives rebuilds.
# Ad-hoc signing re-derives the code requirement from the binary hash on every
# build, which makes macOS treat each rebuild as a brand-new app and drop the
# grant. An Apple Development / Developer ID identity is stable across builds.
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
  | awk -F'"' '/Apple Development|Developer ID Application/ {print $2; exit}')

if [ -n "${IDENTITY:-}" ]; then
  codesign --force --deep --sign "$IDENTITY" "$APP" >/dev/null 2>&1 \
    && echo "signed with $IDENTITY" \
    || { echo "sign failed, falling back to ad-hoc"; codesign --force --sign - "$APP" >/dev/null 2>&1; }
else
  echo "no stable identity; ad-hoc signing (Accessibility grant will not survive rebuilds)"
  codesign --force --sign - "$APP" >/dev/null 2>&1
fi

echo "built $APP"
