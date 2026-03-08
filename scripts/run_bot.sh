#!/bin/zsh
set -euo pipefail

ROOT_DIR="${0:A:h:h}"

cd "$ROOT_DIR"
exec "$ROOT_DIR/polymarketbot"
