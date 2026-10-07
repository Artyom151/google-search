#!/bin/sh
# Installs Circle to Search for the current user (KDE Plasma / other Linux desktops).
#   ./install.sh            shortcut Meta+S
#   ./install.sh Meta+Alt+S custom shortcut
set -e

SHORTCUT="${1:-Meta+S}"
DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart"
mkdir -p "$APPS" "$AUTOSTART"

cat > "$APPS/circle-search.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Circle to Search
Comment=Circle anything on screen to search it with Google Lens
Exec=python3 $DIR/circle_search.py
Icon=$DIR/icons/google_lens.svg
Categories=Utility;
X-KDE-Shortcuts=$SHORTCUT
EOF

# Resident instance so the hotkey opens instantly (no Qt/WebEngine startup).
cat > "$AUTOSTART/circle-search-daemon.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Circle to Search (background)
Exec=python3 $DIR/circle_search.py --daemon
Icon=$DIR/icons/google_lens.svg
NoDisplay=true
EOF

if command -v kwriteconfig6 >/dev/null; then
    kwriteconfig6 --file kglobalshortcutsrc --group services --group circle-search.desktop \
        --key _launch "$SHORTCUT"
    echo "Shortcut $SHORTCUT registered (takes effect after re-login, or set it in"
    echo "System Settings -> Keyboard -> Shortcuts -> Circle to Search)."
else
    echo "Bind the command 'python3 $DIR/circle_search.py' to a shortcut in your desktop settings."
fi

nohup python3 "$DIR/circle_search.py" --daemon >/dev/null 2>&1 &
echo "Done. Background instance started."
