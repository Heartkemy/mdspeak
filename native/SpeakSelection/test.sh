#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
CHECK_DIR=$(mktemp -d)
trap 'rm -rf "$CHECK_DIR"' EXIT
swiftc Sources/SpeakSelection/{Player,PlayerWindow,Worker,AppSettings}.swift \
    Tests/PlayerWindowCheck.swift -o "$CHECK_DIR/player-window-check"
"$CHECK_DIR/player-window-check"
