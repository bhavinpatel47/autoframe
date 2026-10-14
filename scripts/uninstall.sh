#!/usr/bin/env bash
# Remove autoframe.
#
#   scripts/uninstall.sh [--dry-run] [--purge]
#
# Stops and removes the user service, the virtualenv and the command link, and
# (sudo) the module configuration autoframe installed. --purge also deletes
# ~/.config/autoframe. The v4l2loopback package itself is left installed.
set -euo pipefail

DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/autoframe"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autoframe"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/autoframe.service"
SUDO="${SUDO:-sudo}"
DRY_RUN=0 PURGE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done
run() { if ((DRY_RUN)); then printf '    [dry-run] %s\n' "$*"; else "$@"; fi; }

if [[ -f "$UNIT" ]]; then
    run systemctl --user disable --now autoframe.service || true
    run rm -f "$UNIT"
    run systemctl --user daemon-reload
fi
[[ -L "$HOME/.local/bin/autoframe" ]] && run rm -f "$HOME/.local/bin/autoframe"
[[ -d "$DATA_DIR" ]] && run rm -rf "$DATA_DIR"
for f in /etc/modprobe.d/autoframe.conf /etc/modules-load.d/autoframe.conf; do
    # only remove files autoframe wrote
    if [[ -f "$f" ]] && grep -q 'Installed by autoframe' "$f"; then run $SUDO rm -f "$f"; fi
done
if [[ -d /sys/module/v4l2loopback && "$(cat /sys/module/v4l2loopback/refcnt)" == 0 ]]; then
    run $SUDO modprobe -r v4l2loopback || true
fi
((PURGE)) && [[ -d "$CONF_DIR" ]] && run rm -rf "$CONF_DIR"
if ((DRY_RUN)); then echo "(dry run: nothing was changed)"; else echo "autoframe removed."; fi
