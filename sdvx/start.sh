#!/usr/bin/env bash
exec >>sdvx-launch.log 2>&1
set -x

# Change paths to your setup!!

unset LD_PRELOAD
export LD_LIBRARY_PATH=/run/opengl-driver/lib:$LD_LIBRARY_PATH
export WINEPREFIX="$HOME/.local/share/wineprefixes/sdvx"
export LANG=ja_JP.UTF-8

GAME_DIR=/your/game/dir
cd "$GAME_DIR" || exit 1

pkill swayidle &

pgrep -f spice_lights_usb.py >tmplights || /etc/profiles/per-user/user/bin/python3 "$GAME_DIR/spice_lights_usb.py" &
/etc/profiles/per-user/user/bin/python3 sdvx_rpc_nabla_linux.py
