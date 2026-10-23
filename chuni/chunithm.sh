#!/usr/bin/env bash
unset LD_PRELOAD STEAM_RUNTIME STEAM_RUNTIME_LIBRARY_PATH SDL_VIDEODRIVER
exec >> chuni.log 2>&1

export LD_LIBRARY_PATH=/run/opengl-driver/lib:/run/opengl-driver-32/lib
export PATH=/run/current-system/sw/bin:/etc/profiles/per-user/$USER/bin:$PATH
export WINEPREFIX=$HOME/.local/share/wineprefixes/chunithm
export LANG=ja_JP.UTF-8

cd /your/game/dir
if grep -q '^NoNewPrivs:[[:space:]]*1' /proc/self/status; then
    GAMESCOPE=/run/current-system/sw/bin/gamescope # running thru steam
else
    GAMESCOPE=/run/wrappers/bin/gamescope # for terminal and cap sys nice compat
fi

"$GAMESCOPE" -W 2560 -H 1440 -w 1920 -h 1080 -f -S fit -r 120 -F fsr --sharpness 5 -- wine cmd /c start.bat
wineserver -k
