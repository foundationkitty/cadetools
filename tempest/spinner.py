#!/bin/sh
# /usr/bin/gamescope -w 2560 -h 1440 -W 1440 -H 2560 -S stretch -f -r 200 -- sh -c 'exec sh "$STEAM_COMPAT_INSTALL_PATH/spinner.py" -- "$@"' sh %command%
''':'
dir=$(cd "$(dirname "$0")" && pwd)
env="$dir/.faucet-spinner-python"
pkgs='python3.withPackages (p: [ p.evdev p.xlib ])'
if [ ! -x "$env/bin/python3" ]; then
    nix-build -o "$env" -E "with import <nixpkgs> {}; $pkgs" >&2 ||
    nix --extra-experimental-features 'nix-command flakes' build --impure -o "$env" \
        --expr "with import (builtins.getFlake \"nixpkgs\") {}; $pkgs" >&2
fi
[ -x "$env/bin/python3" ] && exec "$env/bin/python3" "$0" "$@"
echo "couldn't set up python with evdev" | tee -a "$dir/faucet-spinner.log" >&2
while [ $# -gt 0 ]; do
    if [ "$1" = "--" ]; then shift; exec "$@"; fi
    shift
done
exit 1
'''

import argparse
import math
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time

import evdev
from evdev import AbsInfo, InputDevice, UInput, ecodes as e
from evdev.uinput import UInputError

FULL_SPEED = 2.0     # turns/sec for a full push
MIN_PUSH = 0.3       # enough to get past the game's dead zone
RELEASE = 0.05       # sec without movement before the stick recentres
SMOOTHING = 0.012    # sec
START_STEPS = 2      # so jitter doesn't nudge the claw
REVERSE = False
SKIP_LAUNCHER = True
NAME = r"faucet|gamo2"

# mode 2 button order: START, BT-A, BT-B, BT-C, BT-D, FX-L, FX-R
PAD_BUTTONS = [e.BTN_START, e.BTN_A, e.BTN_B, e.BTN_X, e.BTN_Y, e.BTN_TL, e.BTN_TR]

STEPS_PER_TURN = 200
TICK = 0.004
PAD_NAME = "Microsoft X-Box 360 pad"

warned = set()


def warn(msg):
    if msg not in warned:
        warned.add(msg)
        print(msg, file=sys.stderr, flush=True)


def is_button(code):
    return (e.BTN_MISC <= code < e.BTN_MOUSE or e.BTN_JOYSTICK <= code < e.BTN_DIGI
            or code >= e.BTN_TRIGGER_HAPPY)


def looks_like_faucet(dev):
    caps = dev.capabilities()
    axes = {code for code, _ in caps.get(e.EV_ABS, [])}
    buttons = [c for c in caps.get(e.EV_KEY, []) if is_button(c)]
    others = {e.ABS_Z, e.ABS_RX, e.ABS_RY, e.ABS_RZ, e.ABS_HAT0X, e.ABS_HAT0Y}
    return {e.ABS_X, e.ABS_Y} <= axes and not axes & others and 7 <= len(buttons) <= 16


def has_knob(dev):
    return any(code == e.ABS_X for code, _ in dev.capabilities().get(e.EV_ABS, []))


def find_controller(path=None):
    if path:
        dev = InputDevice(path)
        if has_knob(dev):
            return dev
        warn(f"{path} has no knob axis, is the controller in mode 2?")
        dev.close()
        return None
    named, lookalikes = [], []
    for p in evdev.list_devices():
        try:
            dev = InputDevice(p)
        except OSError:
            continue
        if dev.name != PAD_NAME and has_knob(dev):
            if re.search(NAME, dev.name, re.I):
                named.append(dev)
                continue
            if looks_like_faucet(dev):
                lookalikes.append(dev)
                continue
        dev.close()
    pick = named[:1] or (lookalikes if len(lookalikes) == 1 else [])
    for dev in named + lookalikes:
        if dev not in pick:
            dev.close()
    return pick[0] if pick else None


class Controller:
    def __init__(self, dev):
        self.dev = dev
        caps = dev.capabilities()
        info = dict(caps[e.EV_ABS])[e.ABS_X]
        if info.fuzz:
            # fuzz eats slow turns
            try:
                dev.set_absinfo(e.ABS_X, fuzz=0)
            except OSError:
                pass
        self.last = self.lo = self.hi = info.value
        self.span = info.max - info.min + 1
        self.fresh = True
        buttons = sorted(c for c in caps.get(e.EV_KEY, []) if is_button(c))
        self.buttons = dict(zip(buttons, PAD_BUTTONS))

    def steps(self, value):
        last, self.last = self.last, value
        fresh, self.fresh = self.fresh, False
        d = value - last
        half = self.span // 2
        wrapped = (d + half) % self.span - half
        if abs(wrapped) <= 24:
            self.lo, self.hi = min(self.lo, value), max(self.hi, value)
            return wrapped
        if fresh:
            self.lo = self.hi = value
            return 0
        seen = self.hi - self.lo + 1
        self.lo, self.hi = min(self.lo, value), max(self.hi, value)
        if abs(d) <= seen // 2:
            return d
        # wrapped short of the advertised max
        m = self.hi - self.lo + 1
        if STEPS_PER_TURN - 4 <= m <= STEPS_PER_TURN:
            m = STEPS_PER_TURN
        return (d + m // 2) % m - m // 2

    def close(self):
        for f in (self.dev.ungrab, self.dev.close):
            try:
                f()
            except OSError:
                pass


class Stick:
    def __init__(self):
        self.pending = 0
        self.speed = 0.0
        self.dir = 1
        self.last_move = -1e9
        self.last_tick = None
        self.arm = 0
        self.arm_time = 0.0

    def turn(self, steps, now):
        if now - self.last_move > RELEASE:
            if self.arm and ((self.arm > 0) != (steps > 0) or now - self.arm_time > 0.15):
                self.arm = 0
            if not self.arm:
                self.arm_time = now
            self.arm += steps
            if abs(self.arm) < START_STEPS:
                return
            steps, self.arm = self.arm, 0
        self.pending += steps
        self.last_move = now
        self.dir = 1 if steps > 0 else -1

    def idle(self):
        return self.pending == 0 and self.speed == 0

    def value(self, now):
        dt = TICK if self.last_tick is None else max(now - self.last_tick, 0.001)
        self.last_tick = now
        rate, self.pending = self.pending / dt, 0
        if rate and self.speed and (rate > 0) != (self.speed > 0):
            self.speed = 0.0
        self.speed += (1 - math.exp(-dt / SMOOTHING)) * (rate - self.speed)
        if now - self.last_move > RELEASE:
            self.speed, self.last_tick = 0.0, None
            return 0
        push = min(abs(self.speed) / (FULL_SPEED * STEPS_PER_TURN), 1.0)
        return round(self.dir * (MIN_PUSH + (1 - MIN_PUSH) * push) * 32767)


def make_pad():
    stick = AbsInfo(0, -32768, 32767, 0, 0, 0)
    trigger = AbsInfo(0, 0, 255, 0, 0, 0)
    hat = AbsInfo(0, -1, 1, 0, 0, 0)
    caps = {
        e.EV_KEY: [e.BTN_A, e.BTN_B, e.BTN_X, e.BTN_Y, e.BTN_TL, e.BTN_TR, e.BTN_SELECT,
                   e.BTN_START, e.BTN_MODE, e.BTN_THUMBL, e.BTN_THUMBR],
        e.EV_ABS: [(e.ABS_X, stick), (e.ABS_Y, stick), (e.ABS_Z, trigger), (e.ABS_RX, stick),
                   (e.ABS_RY, stick), (e.ABS_RZ, trigger), (e.ABS_HAT0X, hat), (e.ABS_HAT0Y, hat)],
    }
    # wired 360 pad ids, so proton treats it as xinput
    return UInput(caps, name=PAD_NAME, vendor=0x045e, product=0x028e, version=0x110)


def release_pad(pad):
    try:
        pad.write(e.EV_ABS, e.ABS_X, 0)
        for button in PAD_BUTTONS:
            pad.write(e.EV_KEY, button, 0)
        pad.syn()
    except OSError:
        pass


def connect(path):
    try:
        dev = find_controller(path)
        if dev:
            dev.grab()
            return Controller(dev)
    except OSError as err:
        warn(f"can't use the controller: {err}")
    return None


def window_title(d, win):
    from Xlib import X, Xatom
    for atom in (d.intern_atom("_NET_WM_NAME"), Xatom.WM_NAME):
        prop = win.get_full_property(atom, X.AnyPropertyType)
        if prop and prop.value:
            value = prop.value
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return ""


def proton_window(win):
    classes = win.get_wm_class() or ()
    return any(c.startswith("steam_app_") or c == "steam_proton" for c in classes)


def is_launcher(d, win, titles):
    from Xlib import X
    if win.get_attributes().map_state != X.IsViewable or not proton_window(win):
        return False
    title = window_title(d, win)
    titles.add(title)
    if "welcome to tempest" in title.lower():
        return True
    # the game itself is always full screen
    geo = win.get_geometry()
    return "tempest" in title.lower() and geo.width < 1000 and geo.height < 1000


def find_launcher(d, win, titles):
    try:
        if is_launcher(d, win, titles):
            return win
        for child in win.query_tree().children:
            found = find_launcher(d, child, titles)
            if found:
                return found
    except Exception:
        pass
    return None


def is_open(d, win, title):
    # window ids get reused, so check it's still the same window
    from Xlib import X
    try:
        return win.get_attributes().map_state == X.IsViewable and window_title(d, win) == title
    except Exception:
        return False


def has_focus(d, win):
    root = d.screen().root
    focus = d.get_input_focus().focus
    while hasattr(focus, "id") and focus.id != root.id:
        if focus.id == win.id:
            return True
        focus = focus.query_tree().parent
    return False


def server_time(d):
    # changing a property on our own window is the only way to get the server's clock
    from Xlib import X, Xatom
    win = d.screen().root.create_window(0, 0, 1, 1, 0, 0, X.InputOnly, event_mask=X.PropertyChangeMask)
    win.change_property(Xatom.WM_NAME, Xatom.STRING, 8, b"")
    d.flush()
    end = time.monotonic() + 1
    try:
        while time.monotonic() < end:
            while d.pending_events():
                ev = d.next_event()
                if ev.type == X.PropertyNotify and ev.window.id == win.id:
                    return ev.time
            select.select([d.fileno()], [], [], max(0, end - time.monotonic()))
        return X.CurrentTime
    finally:
        win.destroy()


def skip_launcher(timeout=120):
    # Return on the resolution dialog is Start Game. Send it straight to the window, and if
    # that gets ignored, type it - only inside gamescope or while the dialog has focus, and
    # not on KDE/GNOME, where typed keys need a permission prompt.
    try:
        from Xlib import X, XK, display
        from Xlib.ext import xtest
        from Xlib.protocol import event
    except ImportError:
        warn("launcher: python-xlib is missing, delete .faucet-spinner-python to have it rebuilt")
        return
    where = os.environ.get("DISPLAY", "")
    try:
        d = display.Display()
    except Exception as err:
        warn(f"launcher: can't open X display {where!r}: {err}")
        return
    root = d.screen().root
    enter = d.keysym_to_keycode(XK.string_to_keysym("Return"))
    gamescope = "GAMESCOPE_WAYLAND_DISPLAY" in os.environ
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    may_type = gamescope or not ("KDE" in desktop or "GNOME" in desktop)
    titles = set()
    dialog = title = None
    tries = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dialog is None:
            dialog = find_launcher(d, root, titles)
            if dialog is None:
                time.sleep(0.25)
                continue
            try:
                title = window_title(d, dialog)
            except Exception:
                dialog = None
                continue
            time.sleep(0.5)
        try:
            when = server_time(d)
            for kind in (event.KeyPress, event.KeyRelease):
                dialog.send_event(kind(time=when, root=root, window=dialog, child=X.NONE,
                                       root_x=0, root_y=0, event_x=0, event_y=0,
                                       state=0, same_screen=1, detail=enter))
            if tries >= 2 and may_type:
                if gamescope:
                    dialog.set_input_focus(X.RevertToParent, when)
                if gamescope or has_focus(d, dialog):
                    xtest.fake_input(d, X.KeyPress, enter)
                    xtest.fake_input(d, X.KeyRelease, enter)
            d.sync()
        except Exception:
            pass
        tries += 1
        time.sleep(1)
        if not is_open(d, dialog, title):
            break
    else:
        if dialog is None:
            seen = ", ".join(repr(t) for t in sorted(titles)) or "none"
            warn(f"launcher: never showed up on display {where}, game windows seen: {seen}")
        else:
            warn(f"launcher: sent Return to {title!r} {tries} times but it stayed open")
    d.close()


def game_env():
    # nix's python wrapper sets this, the game shouldn't get it
    env = dict(os.environ)
    if sys.executable.startswith("/nix/store/"):
        env.pop("PYTHONNOUSERSITE", None)
    return env


def run(pad, device, cmd):
    ctl = game = game_fd = None
    stick = Stick()
    x = 0
    now = next_tick = next_scan = time.monotonic()
    try:
        while True:
            if ctl is None and now >= next_scan:
                ctl = connect(device)
                stick, x = Stick(), 0
                next_scan = now + 2
                if ctl is None and not cmd:
                    sys.exit("FAUCETWO+ not found, is it in mode 2?")

            if cmd and game is None:
                game = subprocess.Popen(cmd, close_fds=False, env=game_env())
                game_fd = os.pidfd_open(game.pid)
                if SKIP_LAUNCHER:
                    threading.Thread(target=skip_launcher, daemon=True).start()
                for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    signal.signal(sig, lambda s, _: game.poll() is None and game.send_signal(s))

            idle = ctl is None or (x == 0 and stick.idle())
            timeout = None if idle else max(0, next_tick - now)
            if ctl is None:
                timeout = max(0, next_scan - now)
            fds = ([ctl.dev.fd] if ctl else []) + ([game_fd] if game else [])
            ready = select.select(fds, [], [], timeout)[0]
            now = time.monotonic()

            if game and game_fd in ready:
                code = game.wait()
                return code if code >= 0 else 128 - code
            if idle:
                next_tick = now

            if ctl and ctl.dev.fd in ready:
                try:
                    events = list(ctl.dev.read())
                except BlockingIOError:
                    events = []
                except OSError:
                    # unplugged, keep the game running and look for it again
                    ctl.close()
                    ctl = None
                    release_pad(pad)
                    if not cmd:
                        sys.exit("lost the controller")
                    next_scan = now + 2
                    continue
                for ev in events:
                    if ev.type == e.EV_ABS and ev.code == e.ABS_X:
                        steps = ctl.steps(ev.value)
                        if steps:
                            stick.turn(-steps if REVERSE else steps, now)
                    elif ev.type == e.EV_KEY and ev.code in ctl.buttons and ev.value in (0, 1):
                        pad.write(e.EV_KEY, ctl.buttons[ev.code], ev.value)
                        pad.syn()

            if ctl and now >= next_tick:
                value = stick.value(now)
                if value != x:
                    x = value
                    pad.write(e.EV_ABS, e.ABS_X, x)
                    pad.syn()
                next_tick = max(next_tick + TICK, now)
    finally:
        if ctl:
            ctl.close()
        release_pad(pad)


def list_devices():
    paths = sorted(evdev.list_devices(), key=lambda p: int(p.rsplit("event", 1)[1]))
    if not paths:
        print("no readable input devices")
    for path in paths:
        dev = InputDevice(path)
        mark = ""
        if has_knob(dev) and (re.search(NAME, dev.name, re.I) or looks_like_faucet(dev)):
            mark = "  <- this one"
        print(f"{path}  {dev.info.vendor:04x}:{dev.info.product:04x}  {dev.name}{mark}")
        dev.close()


def main():
    args, cmd = sys.argv[1:], []
    if "--" in args:
        i = args.index("--")
        args, cmd = args[:i], args[i + 1:]
    parser = argparse.ArgumentParser(usage="%(prog)s [--list] [--device PATH] [-- game command]")
    parser.add_argument("--list", action="store_true", help="list input devices")
    parser.add_argument("--device", help="use this /dev/input/eventN instead of searching")
    opts = parser.parse_args(args)

    if opts.list:
        list_devices()
        return

    if cmd:
        # errors go to a log next to the script, steam's output stays the game's
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            sys.stderr = open(os.path.join(here, "faucet-spinner.log"), "w", buffering=1)
        except OSError:
            pass
    else:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        pad = make_pad()
    except (OSError, UInputError) as err:
        if not cmd:
            sys.exit(f"can't create the virtual pad: {err}")
        print(f"can't create the virtual pad: {err}", file=sys.stderr)
        os.execvpe(cmd[0], cmd, game_env())

    code = 0
    try:
        code = run(pad, opts.device, cmd)
    except KeyboardInterrupt:
        pass
    finally:
        pad.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
