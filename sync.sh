#!/usr/bin/env bash
# Sync the add-on into Anki and rebuild anki_ambush.ankiaddon.
#   ./sync.sh            sync + rebuild package
#   ./sync.sh --restart  also quit and reopen Anki so the new code loads
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/anki_ambush"
DEST="$HOME/Library/Application Support/Anki2/addons21/anki_ambush"
PKG="$ROOT/anki_ambush.ankiaddon"
FILES=(__init__.py config.json config.md manifest.json)

# 1. Syntax check before touching anything
python3 -m py_compile "$SRC/__init__.py"
rm -rf "$SRC/__pycache__"
echo "✓ syntax ok"

# 2. Copy into Anki's add-ons folder (meta.json keeps your saved config)
mkdir -p "$DEST"
rsync -a --exclude __pycache__ --exclude .DS_Store --exclude meta.json \
  "$SRC/" "$DEST/"
echo "✓ synced to $DEST"

# 3. Rebuild the .ankiaddon package
rm -f "$PKG"
(cd "$SRC" && zip -qX "$PKG" "${FILES[@]}")
echo "✓ built $PKG"

# 4. Optionally restart Anki
if [[ "${1:-}" == "--restart" ]]; then
  if pgrep -xq anki || pgrep -fq "Anki.app/Contents/MacOS"; then
    osascript -e 'quit app "Anki"'
    while pgrep -fq "Anki.app/Contents/MacOS"; do sleep 0.5; done
  fi
  open -a Anki
  echo "✓ Anki restarted"
else
  echo "→ restart Anki (Cmd+Q, reopen) to load changes, or rerun with --restart"
fi
