#!/bin/sh
# Per-user install: app files, launcher, autostart, Super+V on GNOME.
set -e
cd "$(dirname "$0")"
APP=io.github.winclip.WinClip
DEST=$HOME/.local/share/winclip

install -Dm755 winclip.py "$DEST/winclip.py"
install -Dm755 watcher.py "$DEST/watcher.py"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.config/autostart"
ln -sf "$DEST/winclip.py" "$HOME/.local/bin/winclip"

desktop() {
    cat <<EOF
[Desktop Entry]
Type=Application
Name=Clipboard History
Comment=Windows-style clipboard history (Super+V)
Exec=$HOME/.local/bin/winclip $1
Icon=edit-paste
Categories=Utility;
StartupNotify=false
EOF
}
desktop "" > "$HOME/.local/share/applications/$APP.desktop"
desktop --background > "$HOME/.config/autostart/$APP.desktop"

# The shortcut talks to the running app over D-Bus (instant), and starts it if it isn't running.
TOGGLE="gdbus call --session --dest $APP --object-path /io/github/winclip/WinClip --method org.gtk.Actions.Activate toggle [] {}"
if gsettings list-schemas | grep -qx org.gnome.settings-daemon.plugins.media-keys; then
    KEYS=org.gnome.settings-daemon.plugins.media-keys
    KEY=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/winclip/
    list=$(gsettings get $KEYS custom-keybindings)
    case "$list" in
        *"$KEY"*) ;;
        "@as []" | "[]") gsettings set $KEYS custom-keybindings "['$KEY']" ;;
        *) gsettings set $KEYS custom-keybindings "${list%]}, '$KEY']" ;;
    esac
    gsettings set $KEYS.custom-keybinding:$KEY name 'Clipboard History'
    gsettings set $KEYS.custom-keybinding:$KEY command "\"sh -c '$TOGGLE || $HOME/.local/bin/winclip'\""
    gsettings set $KEYS.custom-keybinding:$KEY binding '<Super>v'
    # stock GNOME opens the notification list on Super+V; keep it on Super+M only
    case "$(gsettings get org.gnome.shell.keybindings toggle-message-tray)" in
        *"<Super>v"*) gsettings set org.gnome.shell.keybindings toggle-message-tray "['<Super>m']" ;;
    esac
    echo "Super+V bound."
else
    # ponytail: no GlobalShortcuts portal yet; one manual binding covers every other desktop
    echo "Bind Super+V to this command in your desktop's keyboard settings:"
    echo "  $HOME/.local/bin/winclip"
fi

# (re)start in the background
pkill -f "python3 $HOME/.local/bin/winclip" || true
setsid "$HOME/.local/bin/winclip" --background >/dev/null 2>&1 < /dev/null &

if [ ! -w /dev/uinput ]; then
    echo
    echo "Auto-paste (pressing Ctrl+V for you) needs access to /dev/uinput. Run once:"
    echo "  sudo install -m644 $(pwd)/70-winclip-uinput.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger --name-match=uinput"
    echo "then re-run ./install.sh. Until then, picking an item copies it and you press Ctrl+V."
fi
