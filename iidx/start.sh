#!/usr/bin/env bash
exec >>iidx-launch.log 2>&1
set -x

unset LD_PRELOAD
export LD_LIBRARY_PATH=/run/opengl-driver/lib:$LD_LIBRARY_PATH
export LANG=ja_JP.UTF-8

GAME_DIR=/your/game/dir
cd "$GAME_DIR" || exit 1

pkill swayidle &

export WINEPREFIX=~/.local/share/wineprefixes/iidx

pgrep -f iidx_rpc_linux.py >tmplights || /etc/profiles/per-user/user/bin/python3 "$GAME_DIR/iidx_rpc_linux.py" "--no-launch" &
wine spice64.exe
