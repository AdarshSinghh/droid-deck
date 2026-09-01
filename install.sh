#!/bin/zsh
# droid-deck installer — checks prerequisites and installs the launcher.
set -e

REPO_DIR="${0:A:h}"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/droid-deck"

echo "🤖 droid-deck installer"
echo "───────────────────────"

# --- python3 ---
if command -v python3 >/dev/null; then
  echo "✓ python3: $(python3 --version 2>&1)"
else
  echo "✗ python3 not found."
  echo "  Install it with:  brew install python"
  exit 1
fi

# --- adb ---
if command -v adb >/dev/null; then
  echo "✓ adb: $(command -v adb)"
else
  echo "✗ adb not found."
  if command -v brew >/dev/null; then
    echo -n "  Install android-platform-tools with Homebrew now? [y/N] "
    read -r ans
    if [[ "$ans" == [yY]* ]]; then
      brew install --cask android-platform-tools
    else
      echo "  Skipped. Install later with:  brew install --cask android-platform-tools"
      exit 1
    fi
  else
    echo "  Install Homebrew (https://brew.sh), then:"
    echo "    brew install --cask android-platform-tools"
    exit 1
  fi
fi

# --- launcher ---
mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/bin/zsh
exec python3 "$REPO_DIR/droid-deck.py" "\$@"
EOF
chmod +x "$LAUNCHER" "$REPO_DIR/droid-deck.py"
echo "✓ launcher installed: $LAUNCHER"

# --- PATH check ---
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "⚠ $BIN_DIR is not on your PATH. Add this to ~/.zshrc:"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "Done. Enable USB debugging on the phone (see README), then run:  droid-deck"
