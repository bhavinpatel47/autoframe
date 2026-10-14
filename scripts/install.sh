#!/usr/bin/env bash
# Install autoframe for the current user.
#
#   scripts/install.sh [--dry-run] [--editable] [--no-module] [--no-service]
#
# What it does:
#   1. creates a virtualenv in ~/.local/share/autoframe/venv and installs autoframe there
#   2. links ~/.local/bin/autoframe
#   3. (sudo) configures the v4l2loopback kernel module to load at boot with an
#      "Autoframe Camera" device, and loads it now
#   4. installs and starts a systemd user service
#
# --dry-run prints every command instead of running it.
# --editable installs the repo in development mode (code edits apply on restart).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/autoframe"
VENV="$DATA_DIR/venv"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
MODPROBE_CONF=/etc/modprobe.d/autoframe.conf
MODULES_LOAD_CONF=/etc/modules-load.d/autoframe.conf
LABEL="Autoframe Camera"
SUDO="${SUDO:-sudo}"

DRY_RUN=0 EDITABLE=0 DO_MODULE=1 DO_SERVICE=1
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --editable) EDITABLE=1 ;;
        --no-module) DO_MODULE=0 ;;
        --no-service) DO_SERVICE=0 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if ((DRY_RUN)); then printf '    [dry-run] %s\n' "$*"; else "$@"; fi; }
# write_root FILE SRC: copy SRC to a root-owned FILE
write_root() { run $SUDO install -m 0644 "$2" "$1"; }

# --- preflight -------------------------------------------------------------------
[[ "$(uname -s)" == Linux ]] || die "autoframe only runs on Linux"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || die "python3 not found"
"$PY" - <<'PYEOF' || die "Python 3.10 or newer with the venv module is required (Debian/Ubuntu: sudo apt install python3-venv)"
import sys, venv, ensurepip
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PYEOF

# --- 1+2. application ------------------------------------------------------------
say "Installing autoframe into $VENV"
run mkdir -p "$DATA_DIR" "$BIN_DIR"
[[ -x "$VENV/bin/python" ]] || run "$PY" -m venv "$VENV"
run "$VENV/bin/python" -m pip install --quiet --upgrade pip
if ((EDITABLE)); then
    run "$VENV/bin/python" -m pip install --quiet -e "$REPO_DIR[dev]"
else
    run "$VENV/bin/python" -m pip install --quiet --upgrade "$REPO_DIR"
fi
run ln -sf "$VENV/bin/autoframe" "$BIN_DIR/autoframe"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not on your PATH; add it to use the 'autoframe' command" ;; esac

# --- 3. kernel module --------------------------------------------------------------
loopback_has_label() {
    local d
    for d in /sys/class/video4linux/video*; do
        [[ -e "$d/name" && "$(cat "$d/name")" == "$LABEL" ]] && return 0
    done
    return 1
}

if ((DO_MODULE)); then
    say "Configuring the v4l2loopback kernel module"
    if ! modinfo v4l2loopback >/dev/null 2>&1; then
        cat >&2 <<'MSG'
The v4l2loopback kernel module is not installed. Install it, then re-run this script:
  Debian/Ubuntu:        sudo apt install v4l2loopback-dkms
  Fedora (RPM Fusion):  sudo dnf install akmod-v4l2loopback
  Arch:                 sudo pacman -S v4l2loopback-dkms
  openSUSE:             sudo zypper install v4l2loopback-kmp-default
With Secure Boot enabled, DKMS modules must be signed (your distro's DKMS usually prompts to enrol a key).
MSG
        exit 1
    fi

    # Respect v4l2loopback options someone else (e.g. OBS setup) already configured.
    other="$(grep -lsE '^\s*options\s+v4l2loopback' /etc/modprobe.d/*.conf 2>/dev/null | grep -v "^$MODPROBE_CONF$" || true)"
    if [[ -n "$other" ]]; then
        warn "v4l2loopback is already configured in: $other"
        warn "not adding a second options line. Either add a device labelled \"$LABEL\" there,"
        warn "or point autoframe at an existing device: autoframe set output.device /dev/videoN"
    else
        write_root "$MODPROBE_CONF" "$REPO_DIR/packaging/modprobe-autoframe.conf"
        write_root "$MODULES_LOAD_CONF" "$REPO_DIR/packaging/modules-load-autoframe.conf"
    fi

    if loopback_has_label; then
        echo "    \"$LABEL\" device already present"
    elif [[ ! -d /sys/module/v4l2loopback ]]; then
        run $SUDO modprobe v4l2loopback
    elif [[ "$(cat /sys/module/v4l2loopback/refcnt 2>/dev/null || echo 1)" == 0 && -z "$other" ]]; then
        say "Reloading v4l2loopback (loaded with other options, not in use)"
        run $SUDO modprobe -r v4l2loopback
        run $SUDO modprobe v4l2loopback
    else
        warn "v4l2loopback is loaded with other options and is in use; the \"$LABEL\" device appears after a reboot"
    fi
fi

# --- 4. systemd user service -------------------------------------------------------
if ((DO_SERVICE)); then
    if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
        say "Installing systemd user service"
        run mkdir -p "$UNIT_DIR"
        tmp="$(mktemp)"
        sed "s|@BIN@|$VENV/bin/autoframe|" "$REPO_DIR/packaging/autoframe.service" > "$tmp"
        run install -m 0644 "$tmp" "$UNIT_DIR/autoframe.service"
        rm -f "$tmp"
        run systemctl --user daemon-reload
        run systemctl --user enable autoframe.service
        run systemctl --user restart autoframe.service
    else
        warn "systemd user session not available; start autoframe yourself with: autoframe run"
    fi
fi

if ((! DRY_RUN)); then
    say "Checking the setup"
    sleep 1
    "$VENV/bin/autoframe" doctor || true
fi
cat <<MSG

Done. Pick "$LABEL" as the camera in your video app.
  autoframe cameras        list cameras        autoframe use <n>     switch camera
  autoframe status         what it is doing    autoframe tracking off|on
MSG
