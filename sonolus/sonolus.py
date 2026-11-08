#!/usr/bin/env python3
import argparse
import json
import math
import os
import select
import signal
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

try:
    from evdev import AbsInfo, InputDevice, UInput
    from evdev import ecodes as e
except ImportError:
    sys.exit("python-evdev is missing. Install it: apt install python3-evdev | "
             "dnf install python3-evdev | pacman -S python-evdev | pip install evdev")

COLS = 16
ABS_MAX = 32767
VIRT_NAME = "TASOLLER Touch"
VIRT_VENDOR, VIRT_PRODUCT = 0x1209, 0x7A5C
TASOLLER_VENDORS = {0x1CCF, 0x0E8F}
CFW_VID, CFW_PID = 0x1CCF, 0x2333
IO4_VID, IO4_PID = 0x0CA3, 0x0021

INPUT_BY_ID = Path("/dev/input/by-id")
SERIAL_BY_ID = Path("/dev/serial/by-id")

SL_REPORT, SL_LED, SL_SCAN_START, SL_SCAN_STOP, SL_RESET = 0x01, 0x02, 0x03, 0x04, 0x10

DISCORD_CLIENT_ID = "1568758081112772758"
DISCORD_SOCKET_DIRS = ["", "app/com.discordapp.Discord", "snap.discord",
                       ".flatpak/dev.vencord.Vesktop/xdg-run"]
SONOLUS_TYPES = {"levels", "playlists", "skins", "backgrounds", "effects",
                 "particles", "engines", "replays", "posts"}


def sonolus_api_url(link):
    rest = link.split("://", 1)[-1].split("?")[0].split("#")[0]
    parts = [p for p in rest.split("/") if p]
    for i in range(len(parts) - 2, 0, -1):
        if parts[i] in SONOLUS_TYPES and parts[i + 1] not in ("list", "info"):
            return f"https://{'/'.join(parts[:i])}/sonolus/{parts[i]}/{parts[i + 1]}"
    return None


def fetch_sonolus_item(link):
    url = sonolus_api_url(link)
    if not url:
        return None, None
    req = urllib.request.Request(url, headers={"User-Agent": "sonolus-tasoller/1.0",
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            item = json.load(r).get("item")
    except (OSError, ValueError, AttributeError) as err:
        print(f"Discord presence: couldn't look up {url} ({err})", file=sys.stderr)
        return None, None
    return (item, url) if isinstance(item, dict) else (None, None)


def runtime_dir():
    if os.environ.get("SUDO_UID"):
        return Path("/run/user") / os.environ["SUDO_UID"]
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")


def config_path():
    home = Path.home()
    if os.environ.get("SUDO_USER"):
        home = Path(os.path.expanduser("~" + os.environ["SUDO_USER"]))
    return home / ".config" / "tasoller-touch.json"


class TouchOut:
    def __init__(self, args, ui=None):
        self.a = args
        self.ui = ui or self._make_uinput(args.slots)
        self.touches = []
        self.next_tid = 1
        self.last_col = COLS / 2 - 0.5
        self.prev_air = False

    @staticmethod
    def _make_uinput(slots):
        def axis(mx):
            return AbsInfo(value=0, min=0, max=mx, fuzz=0, flat=0, resolution=100)

        caps = {
            e.EV_KEY: [e.BTN_TOUCH],
            e.EV_ABS: [
                (e.ABS_X, axis(ABS_MAX)),
                (e.ABS_Y, axis(ABS_MAX)),
                (e.ABS_MT_SLOT, AbsInfo(0, 0, slots - 1, 0, 0, 0)),
                (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                (e.ABS_MT_POSITION_X, axis(ABS_MAX)),
                (e.ABS_MT_POSITION_Y, axis(ABS_MAX)),
            ],
        }
        return UInput(caps, name=VIRT_NAME, vendor=VIRT_VENDOR, product=VIRT_PRODUCT,
                      version=1, input_props=[e.INPUT_PROP_DIRECT])

    def _px(self, col, y):
        xf = self.a.x0 + (col + 0.5) / COLS * (self.a.x1 - self.a.x0)
        return int(max(0, min(1, xf)) * ABS_MAX), int(max(0, min(1, y)) * ABS_MAX)

    def _free_slot(self):
        used = {t["slot"] for t in self.touches}
        for s in range(self.a.slots):
            if s not in used:
                return s
        return None

    def _w(self, typ, code, val):
        self.ui.write(typ, code, val)

    def _down(self, col, y):
        slot = self._free_slot()
        if slot is None:
            return None
        t = {"slot": slot, "tid": self.next_tid, "col": col, "y": y}
        self.next_tid = self.next_tid % 65000 + 1
        x, yy = self._px(col, y)
        self._w(e.EV_ABS, e.ABS_MT_SLOT, slot)
        self._w(e.EV_ABS, e.ABS_MT_TRACKING_ID, t["tid"])
        self._w(e.EV_ABS, e.ABS_MT_POSITION_X, x)
        self._w(e.EV_ABS, e.ABS_MT_POSITION_Y, yy)
        self.touches.append(t)
        return t

    def _move(self, t, col=None, y=None):
        if col is not None:
            t["col"] = col
        if y is not None:
            t["y"] = y
        x, yy = self._px(t["col"], t["y"])
        self._w(e.EV_ABS, e.ABS_MT_SLOT, t["slot"])
        self._w(e.EV_ABS, e.ABS_MT_POSITION_X, x)
        self._w(e.EV_ABS, e.ABS_MT_POSITION_Y, yy)

    def _up(self, t):
        self._w(e.EV_ABS, e.ABS_MT_SLOT, t["slot"])
        self._w(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        self.touches.remove(t)
        self.last_col = t["col"]

    def _sync(self):
        if self.touches:
            x, y = self._px(self.touches[0]["col"], self.touches[0]["y"])
            self._w(e.EV_ABS, e.ABS_X, x)
            self._w(e.EV_ABS, e.ABS_Y, y)
        self._w(e.EV_KEY, e.BTN_TOUCH, 1 if self.touches else 0)
        self.ui.syn()

    def _blobs(self, cols):
        out, i = [], 0
        while i < COLS:
            if not cols[i]:
                i += 1
                continue
            j = i
            while j + 1 < COLS and cols[j + 1]:
                j += 1
            width = j - i + 1
            if self.a.mode == "lanes":
                out.extend(float(c) for c in range(i, j + 1))
            else:
                n = max(1, math.ceil(width / self.a.max_blob))
                for k in range(n):
                    a = i + k * width / n
                    b = i + (k + 1) * width / n - 1
                    out.append((a + b) / 2)
            i = j + 1
        return out

    def _matches(self, t, b):
        if self.a.mode == "lanes":
            return t["col"] == b
        return abs(t["col"] - b) <= self.a.max_jump

    def update(self, cols, air):
        blobs = self._blobs(cols)
        pairs = sorted(
            (abs(t["col"] - b), ti, bi)
            for ti, t in enumerate(self.touches)
            for bi, b in enumerate(blobs)
            if self._matches(t, b)
        )

        used_t, used_b, moves = set(), set(), []
        for _, ti, bi in pairs:
            if ti in used_t or bi in used_b:
                continue
            used_t.add(ti)
            used_b.add(bi)
            moves.append((self.touches[ti], blobs[bi]))
        gone = [t for ti, t in enumerate(self.touches) if ti not in used_t]

        changed = False
        for t, b in moves:
            if t["col"] != b:
                self._move(t, col=b)
                changed = True
        for t in gone:
            self._up(t)
            changed = True
        for bi, b in enumerate(blobs):
            if bi not in used_b and self._down(b, self.a.y):
                changed = True
        if changed:
            self._sync()

        rising = air and not self.prev_air
        self.prev_air = air
        if rising and not self.a.no_air:
            self.flick()

    def flick(self):
        steps = max(2, self.a.flick_steps)
        dt = self.a.flick_ms / 1000.0 / steps
        targets = list(self.touches)
        temp = None
        if not targets:
            temp = self._down(self.last_col, self.a.y)
            if temp is None:
                return
            self._sync()
            targets = [temp]

        start = {id(t): t["y"] for t in targets}
        for s in range(1, steps + 1):
            time.sleep(dt)
            for t in targets:
                if t in self.touches:
                    self._move(t, y=start[id(t)] - self.a.flick * s / steps)
            self._sync()

        if temp is not None and temp in self.touches:
            self._up(temp)
            self._sync()

    def release_all(self):
        for t in list(self.touches):
            self._up(t)
        self._sync()


class DiscordPresence(threading.Thread):
    def __init__(self, client_id, mode):
        super().__init__(daemon=True)
        self.client_id = client_id
        self.mode = mode
        self.started = int(time.time())
        self.now_file = runtime_dir() / "sonolus-now"
        self.sock = None
        self.items = {}

    def run(self):
        shown, connected = object(), None
        while True:
            try:
                if self.sock is None:
                    self._connect()
                    shown = object()
                link = self._now_playing()
                if link != shown:
                    self._send(1, {"cmd": "SET_ACTIVITY", "nonce": str(uuid.uuid4()),
                                   "args": {"pid": os.getpid(), "activity": self._activity(link)}})
                    self._recv()
                    shown = link
                if connected is not True:
                    print("Discord presence: connected")
                    connected = True
                time.sleep(2)
            except (OSError, ValueError) as err:
                if connected is not False:
                    print(f"Discord presence: not connected ({err}), retrying in the background",
                          file=sys.stderr)
                    connected = False
                self._close()
                time.sleep(15)

    def _connect(self):
        base = runtime_dir()
        for sub in DISCORD_SOCKET_DIRS:
            for n in range(10):
                path = base / sub / f"discord-ipc-{n}"
                if not path.exists():
                    continue
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(5)
                try:
                    s.connect(str(path))
                except OSError:
                    s.close()
                    continue
                self.sock = s
                self._send(0, {"v": 1, "client_id": self.client_id})
                op, reply = self._recv()
                if op != 1 or reply.get("evt") != "READY":
                    raise ValueError(reply.get("message", "handshake refused"))
                return
        raise OSError("Discord isn't running")

    def _close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _send(self, op, payload):
        data = json.dumps(payload).encode()
        self.sock.sendall(struct.pack("<II", op, len(data)) + data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OSError("Discord closed the connection")
            buf += chunk
        return buf

    def _recv(self):
        op, length = struct.unpack("<II", self._recv_exact(8))
        reply = json.loads(self._recv_exact(length))
        if op == 2:
            raise ValueError(reply.get("message", "closed by Discord"))
        return op, reply

    def _now_playing(self):
        try:
            return self.now_file.read_text().strip() or None
        except OSError:
            return None

    def _activity(self, link):
        def text(s):
            s = s[:128]
            return s if len(s) >= 2 else s + " "

        activity = {
            "timestamps": {"start": self.started},
            "assets": {"large_image": "sonolus", "large_text": text(f"TASOLLER ({self.mode})")},
        }
        if not link:
            activity["details"] = "Playing on a TASOLLER"
            return activity

        host, _, path = link.split("://", 1)[-1].partition("/")
        if link not in self.items:
            self.items[link] = fetch_sonolus_item(link)
        item, api = self.items[link]

        if not item:
            parts = [p for p in path.split("?")[0].split("/") if p]
            activity["details"] = text(urllib.parse.unquote(parts[-1]) if parts else host)
            activity["state"] = text(host)
            return activity

        activity["details"] = text(item.get("title") or "Sonolus")
        by = item.get("artists") or item.get("subtitle") or item.get("author")
        if by:
            activity["state"] = text(by)

        info = f"Lv. {item['rating']} · {host}" if "rating" in item else host
        cover = (item.get("cover") or item.get("thumbnail") or {}).get("url")
        if cover:
            cover = urllib.parse.urljoin(api, cover)
        if cover and cover.startswith("https://") and len(cover) <= 256:
            activity["assets"] = {
                "large_image": cover, "large_text": text(info),
                "small_image": "sonolus", "small_text": text(f"TASOLLER ({self.mode})"),
            }
        else:
            activity["assets"]["large_text"] = text(f"{info} · TASOLLER ({self.mode})")
        return activity


def by_id_links(directory, pattern="*"):
    links = {}
    if directory.is_dir():
        for link in sorted(directory.glob(pattern)):
            links.setdefault(os.path.realpath(link), str(link))
    return links


def find_keyboards(args):
    if args.device:
        return [InputDevice(p) for p in args.device]

    devs = []
    for link in by_id_links(INPUT_BY_ID, "*event*").values():
        try:
            d = InputDevice(link)
        except OSError:
            continue
        name_hit = any(s in d.name.lower() for s in ("tasoller", "skystar", "nya"))
        if (d.info.vendor in TASOLLER_VENDORS or name_hit) and e.EV_KEY in d.capabilities():
            devs.append(d)
        else:
            d.close()

    if not devs:
        sys.exit("No TASOLLER keyboard device found in /dev/input/by-id. Check it is in a "
                 "keyboard mode, run --list, then pass --device /dev/input/by-id/...")
    return devs


def usb_ids_of_tty(dev):
    base = Path("/sys/class/tty") / os.path.basename(dev) / "device"
    try:
        vid = int((base / ".." / "idVendor").read_text(), 16)
        pid = int((base / ".." / "idProduct").read_text(), 16)
        return vid, pid
    except (OSError, ValueError):
        return None


def list_devices():
    print("/dev/input/by-id:")
    for real, link in by_id_links(INPUT_BY_ID).items():
        try:
            d = InputDevice(link)
            info = f"{d.info.vendor:04x}:{d.info.product:04x}  {d.name}"
            tag = "  <-- TASOLLER?" if d.info.vendor in TASOLLER_VENDORS else ""
        except OSError:
            info, tag = "(no access)", ""
        print(f"  {link}\n      -> {real}  {info}{tag}")

    print("/dev/serial/by-id:")
    for real, link in by_id_links(SERIAL_BY_ID).items():
        vp = usb_ids_of_tty(real)
        tag = "  <-- IO4 slider" if vp == (IO4_VID, IO4_PID) and link.endswith("-if02") else ""
        ids = f"{vp[0]:04x}:{vp[1]:04x}" if vp else "?"
        print(f"  {link}\n      -> {real}  {ids}{tag}")

    hid, _ = find_io4(argparse.Namespace(io4_hid=None, io4_tty="-"))
    if hid:
        print(f"IO4 hidraw (hidraw has no by-id; matched by 0ca3:0021 interface 0): {hid}")


def learn(args):
    devs = find_keyboards(args)
    for d in devs:
        d.grab()
    print("Learning. Using:", ", ".join(f"{d.path} ({d.name})" for d in devs))
    print("Put the TASOLLER in 32-key or 16-key keyboard mode first.\n")

    def collect(prompt, timeout_first=20.0, quiet=0.7):
        print(prompt, flush=True)
        codes, held, t_last, started = set(), set(), None, time.time()
        while True:
            r, _, _ = select.select(devs, [], [], 0.05)
            for d in r:
                for ev in d.read():
                    if ev.type != e.EV_KEY:
                        continue
                    if ev.value == 1:
                        codes.add(ev.code)
                        held.add(ev.code)
                    elif ev.value == 0:
                        held.discard(ev.code)
                    t_last = time.time()
            now = time.time()
            if not codes and now - started > timeout_first:
                return set()
            if codes and not held and t_last and now - t_last > quiet:
                return codes

    def key_name(code):
        return str(e.KEY.get(code, code))

    mapping = {"cols": {}, "air": []}
    try:
        for c in range(COLS):
            got = collect(f"  Column {c + 1:2}/16 (count from the LEFT): rub top AND bottom pad, then lift.")
            if not got:
                print("    nothing pressed, skipping")
            for code in got:
                prev = mapping["cols"].get(str(code))
                if prev is not None:
                    print(f"    note: {key_name(code)} was already column {prev + 1}; "
                          f"now column {c + 1} (use 32/16-key mode for full resolution)")
                mapping["cols"][str(code)] = c
            print("    ->", ", ".join(key_name(k) for k in sorted(got)))

        got = collect("\n  AIR: raise your hand up through all the air sensors, then pull it out "
                      "(waits 20 s, skip if your mode has no air keys).")
        mapping["air"] = sorted(k for k in got if str(k) not in mapping["cols"])
        print("    ->", ", ".join(key_name(k) for k in mapping["air"]) or "none")
    finally:
        for d in devs:
            d.ungrab()

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=2))
    if os.environ.get("SUDO_UID"):
        os.chown(path, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
    print(f"\nSaved {path}")


def show(cols, air):
    print("".join("#" if c else "." for c in cols), "AIR" if air else "")


def run_keyboard(args, out):
    path = config_path()
    if not path.exists():
        sys.exit(f"No key map at {path}. Run with --learn first.")
    m = json.loads(path.read_text())
    colmap = {int(k): v for k, v in m["cols"].items()}
    air_keys = set(m.get("air", []))

    devs = find_keyboards(args)
    for d in devs:
        d.grab()
    print("Reading:", ", ".join(f"{d.path} ({d.name})" for d in devs))

    held = set()
    try:
        while True:
            r, _, _ = select.select(devs, [], [])
            dirty = False
            for d in r:
                for ev in d.read():
                    if ev.type != e.EV_KEY or ev.value == 2:
                        continue
                    if ev.value == 1:
                        held.add(ev.code)
                    else:
                        held.discard(ev.code)
                    dirty = True
            if not dirty:
                continue
            cols = [False] * COLS
            for k in held:
                if k in colmap:
                    cols[colmap[k]] = True
            air = bool(held & air_keys)
            out.update(cols, air)
            if args.verbose:
                show(cols, air)
    finally:
        for d in devs:
            try:
                d.ungrab()
            except OSError:
                pass


def run_cfw(args, out):
    try:
        import usb.core
    except ImportError:
        sys.exit("pyusb missing: apt install python3-usb | pacman -S python-pyusb | pip install pyusb")

    dev = usb.core.find(idVendor=CFW_VID, idProduct=CFW_PID)
    if dev is None:
        sys.exit("TASOLLER custom firmware (1ccf:2333) not found.")
    for cfg in dev:
        for intf in cfg:
            try:
                if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                    dev.detach_kernel_driver(intf.bInterfaceNumber)
            except (usb.core.USBError, NotImplementedError):
                pass

    print("Reading TASOLLER CFW 2.0 over USB (ep 0x84)")
    while True:
        try:
            data = dev.read(0x84, 36, timeout=100)
        except usb.core.USBTimeoutError:
            continue
        except usb.core.USBError as err:
            if getattr(err, "errno", None) == 110:
                continue
            raise
        if len(data) < 36:
            continue
        cols = pads_to_cols(data[4:36], args.threshold, not args.cfw_no_flip)
        air = bool(data[3] & 0x3F)
        out.update(cols, air)
        if args.verbose:
            show(cols, air)


def slider_frame(cmd, payload=b""):
    body = bytes([cmd, len(payload)]) + bytes(payload)
    csum = -(0xFF + sum(body)) & 0xFF
    out = bytearray([0xFF])
    for b in body + bytes([csum]):
        out += bytes([0xFD, b - 1]) if b in (0xFF, 0xFD) else bytes([b])
    return bytes(out)


class SliderParser:
    def __init__(self):
        self.buf = bytearray()
        self.esc = False

    def feed(self, data):
        frames = []
        for b in data:
            if b == 0xFF:
                self.buf = bytearray([0xFF])
                self.esc = False
                continue
            if not self.buf:
                continue
            if b == 0xFD:
                self.esc = True
                continue
            if self.esc:
                b = (b + 1) & 0xFF
                self.esc = False
            self.buf.append(b)
            if len(self.buf) >= 3 and len(self.buf) == 4 + self.buf[2]:
                if sum(self.buf) & 0xFF == 0:
                    frames.append((self.buf[1], bytes(self.buf[3:-1])))
                self.buf = bytearray()
        return frames


def pads_to_cols(pads, threshold, flip=True):
    cols = [False] * COLS
    for p in range(COLS):
        hit = max(pads[2 * p], pads[2 * p + 1]) > threshold
        cols[COLS - 1 - p if flip else p] = hit
    return cols


def io4_air_blocked(rep, invert=False):
    off = 1 if rep and rep[0] == 0x01 else 0
    if len(rep) < off + 32:
        return None
    w0 = rep[off + 28] | rep[off + 29] << 8
    w1 = rep[off + 30] | rep[off + 31] << 8
    free = [(w >> b) & 1 for w in (w0, w1) for b in (11, 12, 13)]
    if invert:
        return any(free)
    return not all(free)


def find_io4(args):
    hid, tty = args.io4_hid, args.io4_tty

    if not hid:
        want = f"HID_ID=0003:0000{IO4_VID:04X}:0000{IO4_PID:04X}"
        for d in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
            try:
                ue = (d / "device" / "uevent").read_text().upper()
            except OSError:
                continue
            if want in ue:
                parent = os.path.basename(os.path.dirname(os.path.realpath(d / "device")))
                if parent.endswith(":1.0") or hid is None:
                    hid = "/dev/" + d.name

    if tty == "-":
        tty = None
    elif not tty:
        for real, link in by_id_links(SERIAL_BY_ID, "*-if02").items():
            if usb_ids_of_tty(real) == (IO4_VID, IO4_PID):
                tty = link
                break

    return hid, tty


def open_serial(path):
    import termios
    import tty

    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[4] = attrs[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def led_frame(cols):
    rgb = bytearray(96)
    for i in range(31):
        if i % 2 == 0:
            lit = cols[COLS - 1 - i // 2]
            b, r, g = (255, 255, 255) if lit else (60, 20, 0)
        else:
            b, r, g = 120, 120, 0
        rgb[i * 3:i * 3 + 3] = bytes([b, r, g])
    return slider_frame(SL_LED, bytes([0x3F]) + bytes(rgb))


def run_io4(args, out):
    hid, tty = find_io4(args)
    if not tty:
        sys.exit("IO4-emulation slider not found in /dev/serial/by-id (0ca3:0021, *-if02). "
                 "Run --list, then pass --io4-tty /dev/serial/by-id/...-if02")

    sfd = open_serial(tty)
    hfd = None
    if hid and not args.no_air:
        hfd = os.open(hid, os.O_RDONLY | os.O_NONBLOCK)
    print(f"IO4 mode: slider {tty}, air {hid or 'disabled'}")

    def send(frame):
        try:
            os.write(sfd, frame)
        except BlockingIOError:
            pass

    send(slider_frame(SL_RESET))
    time.sleep(0.05)
    send(slider_frame(SL_SCAN_START))

    parser = SliderParser()
    cols, air = [False] * COLS, False
    last_rep = last_led = air_since = time.time()
    warned = air_hint = False
    fds = [sfd] if hfd is None else [sfd, hfd]

    try:
        while True:
            r, _, _ = select.select(fds, [], [], 0.05)
            now = time.time()
            changed = False

            if sfd in r:
                try:
                    data = os.read(sfd, 4096)
                except BlockingIOError:
                    data = b""
                for cmd, pl in parser.feed(data):
                    if cmd == SL_REPORT and len(pl) >= 32:
                        last_rep = now
                        nc = pads_to_cols(pl, args.threshold, not args.cfw_no_flip)
                        if nc != cols:
                            cols, changed = nc, True

            if hfd is not None and hfd in r:
                try:
                    rep = os.read(hfd, 64)
                except BlockingIOError:
                    rep = b""
                a = io4_air_blocked(rep, args.air_invert)
                if a is not None and a != air:
                    air, changed = a, True
                    air_since = now

            if air and not air_hint and now - air_since > 3.0:
                print("air has read 'blocked' for 3 s - if your hand isn't in the beams, "
                      "restart with --air-invert", file=sys.stderr)
                air_hint = True

            if changed:
                out.update(cols, air)
                if args.verbose:
                    show(cols, air)

            if now - last_rep > 1.0:
                send(slider_frame(SL_SCAN_START))
                last_rep = now
                if not warned:
                    print("no slider reports yet (resending scan start)...", file=sys.stderr)
                    warned = True

            if not args.no_led and now - last_led > 0.05:
                send(led_frame(cols))
                last_led = now
    finally:
        send(slider_frame(SL_SCAN_STOP))


def parse_args():
    ap = argparse.ArgumentParser(
        description="Turn a TASOLLER into a virtual multitouch screen (Waydroid / Sonolus).")
    ap.add_argument("--list", action="store_true", help="list input devices and exit")
    ap.add_argument("--learn", action="store_true", help="learn the keyboard-mode key layout")
    ap.add_argument("--cfw", action="store_true", help="read TASOLLER custom firmware 2.0 via USB instead of keyboard")
    ap.add_argument("--io4", action="store_true",
                    help="IO4-emulation firmware (e.g. Supernova): serial slider + IO4 HID air")
    ap.add_argument("--keyboard", action="store_true", help="force keyboard mode even if an IO4 board is present")
    ap.add_argument("--io4-tty", help="slider serial port (default: auto from /dev/serial/by-id/*-if02)")
    ap.add_argument("--io4-hid", help="IO4 hidraw node (default: auto)")
    ap.add_argument("--air-invert", action="store_true", help="IO4: flip air beam polarity")
    ap.add_argument("--no-led", action="store_true", help="IO4: don't drive the slider LEDs")
    ap.add_argument("--device", action="append",
                    help="evdev node(s) to read, e.g. /dev/input/by-id/usb-...-event-kbd")
    ap.add_argument("--x0", type=float, default=0.12, help="screen x of slider's left edge (0..1)")
    ap.add_argument("--x1", type=float, default=0.88, help="screen x of slider's right edge (0..1)")
    ap.add_argument("--y", type=float, default=0.86, help="screen y of the judgement line (0..1)")
    ap.add_argument("--mode", choices=["blobs", "lanes"], default="blobs",
                    help="blobs: one moving touch per finger (default). lanes: one fixed touch per column")
    ap.add_argument("--max-blob", type=int, default=4, help="split runs wider than this many columns")
    ap.add_argument("--max-jump", type=float, default=2.5, help="columns a finger may move per update")
    ap.add_argument("--slots", type=int, default=10, help="max simultaneous touches")
    ap.add_argument("--flick", type=float, default=0.15, help="flick distance (fraction of height)")
    ap.add_argument("--flick-ms", type=float, default=12, help="flick duration in ms")
    ap.add_argument("--flick-steps", type=int, default=4)
    ap.add_argument("--no-air", action="store_true", help="ignore air sensors")
    ap.add_argument("--threshold", type=int, default=20, help="CFW/IO4 pressure threshold (0-255)")
    ap.add_argument("--cfw-no-flip", action="store_true", help="CFW/IO4: don't reverse column order")
    ap.add_argument("--discord-id", default=DISCORD_CLIENT_ID,
                    help="Discord application ID for Rich Presence")
    ap.add_argument("--no-discord", action="store_true", help="don't show a Discord status")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args()


def main():
    args = parse_args()

    if args.list:
        return list_devices()
    if args.learn:
        return learn(args)

    out = TouchOut(args)
    print(f"Virtual touchscreen '{VIRT_NAME}' ({VIRT_VENDOR:04x}:{VIRT_PRODUCT:04x}) created.")

    def bye(*_):
        out.release_all()
        out.ui.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)

    explicit = args.io4 or args.cfw or args.keyboard or args.device
    if not explicit and find_io4(args)[1]:
        args.io4 = True

    if args.io4:
        runner, mode = run_io4, "IO4"
    elif args.cfw:
        runner, mode = run_cfw, "CFW"
    else:
        runner, mode = run_keyboard, "keyboard"

    if args.discord_id and not args.no_discord:
        DiscordPresence(args.discord_id, mode).start()

    try:
        runner(args, out)
    finally:
        out.release_all()


if __name__ == "__main__":
    main()
