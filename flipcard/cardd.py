#!/usr/bin/env python3
import argparse
import configparser
import glob
import json
import os
import random
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time

CONFIG = os.path.expanduser(os.environ.get("CARDD_CONFIG", "~/.config/flipcard/cardd.ini"))
SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "cardd.sock")
HID_ID = "0003:0000F822:00000148"

DEFAULT_CONFIG = """[general]
cooldown = 3
notify = true

[spice]
enabled = true
host = 127.0.0.1
port = 1337
password =
unit = 0
processes = spice64.exe, spice.exe

[segatools:chunithm]
enabled = true
processes = chusanApp
tap_file = /mnt/crucial/games/chunithm/App/bin/DEVICE/tap.txt
"""


def log(msg):
    print("[cardd] " + msg, flush=True)


def load_config():
    if not os.path.exists(CONFIG):
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w") as f:
            f.write(DEFAULT_CONFIG)
        log("wrote default config to " + CONFIG)

    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    cfg.read(CONFIG)
    return cfg


def clean_id(s):
    s = re.sub(r"[^0-9A-Fa-f]", "", s or "").upper()
    if len(s) == 16 or (len(s) == 20 and s.isdigit()):
        return s
    return None


def running():
    names = set()
    for path in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(path) as f:
                names.add(f.read().strip().lower())
        except OSError:
            pass
    return names


def is_running(procs, value):
    for name in value.split(","):
        name = name.strip().lower()[:15]
        if name and any(p.startswith(name) for p in procs):
            return True
    return False


def rc4(key):
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 255
        s[i], s[j] = s[j], s[i]

    i = j = 0
    while True:
        i = (i + 1) & 255
        j = (j + s[i]) & 255
        s[i], s[j] = s[j], s[i]
        yield s[(s[i] + s[j]) & 255]


class Spice:
    def __init__(self, host, port, password):
        self.sock = socket.create_connection((host, port), timeout=3)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.key = rc4(password.encode()) if password else None

        res = self.call("control", "session_refresh")
        self.key = rc4(res["data"][0].encode())

    def crypt(self, data):
        if not self.key:
            return data
        return bytes(b ^ next(self.key) for b in data)

    def call(self, module, function, *params):
        req = {
            "id": random.randint(1, 2**63),
            "module": module,
            "function": function,
            "params": list(params),
        }
        self.sock.sendall(self.crypt(json.dumps(req, separators=(",", ":")).encode() + b"\0"))

        buf = b""
        while not buf.endswith(b"\0"):
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed")
            buf += self.crypt(chunk)

        if len(buf) <= 1:
            raise ValueError("request rejected, wrong password?")

        res = json.loads(buf[:-1])
        if res.get("errors"):
            raise RuntimeError(str(res["errors"]))
        return res

    def close(self):
        self.sock.close()


def send_spice(sec, card):
    if len(card) != 16:
        log("spice2x: spice2x needs a 16-hex card ID (not an Aime access code)")
        return False

    try:
        api = Spice(sec.get("host", "127.0.0.1"), sec.getint("port", 1337), sec.get("password", ""))
        try:
            api.call("card", "insert", sec.getint("unit", 0), card)
        finally:
            api.close()
    except (OSError, ValueError, RuntimeError) as e:
        log(f"spice2x: {e} - is SpiceAPI enabled on port {sec.get('port', '1337')} "
            "with the same password?")
        return False

    log("spice2x: inserted " + card)
    return True


def send_segatools(name, sec, card):
    path = sec.get("tap_file")
    if not path:
        log(name + ": no tap_file set")
        return False

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            f.write(card + "\n")
        os.replace(path + ".tmp", path)
    except OSError as e:
        log(f"{name}: could not write tap file: {e}")
        return False

    log(f"{name}: tapped {card}")
    return True


def notify(cfg, text):
    if not cfg.getboolean("general", "notify", fallback=False):
        return
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "cardd", "-t", "2500", "Card", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dispatch(cfg, card):
    procs = running()
    found = False
    sent = []

    if cfg.has_section("spice") and cfg.getboolean("spice", "enabled", fallback=True):
        sec = cfg["spice"]
        if is_running(procs, sec.get("processes", "spice64.exe, spice.exe")):
            found = True
            if send_spice(sec, card):
                sent.append("spice2x")

    for section in cfg.sections():
        if not section.startswith("segatools"):
            continue
        sec = cfg[section]
        name = section.split(":", 1)[-1]
        if sec.getboolean("enabled", fallback=True) and is_running(procs, sec.get("processes", "")):
            found = True
            if send_segatools(name, sec, card):
                sent.append(name)

    if sent:
        notify(cfg, f"{card} -> {', '.join(sent)}")
    elif found:
        notify(cfg, card + ": not delivered (see log)")
    else:
        log(f"tap {card}: no configured game is running")
        notify(cfg, card + ": no game running")


def find_flipper():
    for path in glob.glob("/sys/class/hidraw/hidraw*/device/uevent"):
        try:
            with open(path) as f:
                if "HID_ID=" + HID_ID in f.read().upper():
                    return "/dev/" + path.split("/")[4]
        except OSError:
            pass
    return None


class Flipper:
    def __init__(self):
        self.fd = None
        self.last = bytes(8)
        self.told = None

    def connect(self):
        if self.fd is not None:
            return True

        path = find_flipper()
        if not path:
            if self.told != "missing":
                log("waiting for the Flipper (Apps > USB > HINATA USB Reader, hold OK for CardIO)")
                self.told = "missing"
            return False

        try:
            self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except PermissionError:
            if self.told != "perm":
                log(f"no permission for {path} - add the udev rule from the README")
                self.told = "perm"
            return False

        self.last = bytes(8)
        self.told = None
        log(f"Flipper connected ({path})")
        return True

    def read(self):
        try:
            data = os.read(self.fd, 64)
        except BlockingIOError:
            return []
        except OSError:
            log("Flipper disconnected")
            os.close(self.fd)
            self.fd = None
            return []

        if not data:
            return []
        if data[0] == 2 and len(data) % 9 == 0:
            return [data[i:i + 9] for i in range(0, len(data), 9)]
        return [data]

    def card(self, report):
        if len(report) < 9 or report[0] != 2:
            return None

        cur = bytes(report[1:9])
        new = cur != self.last and any(cur)
        self.last = cur
        return cur.hex().upper() if new else None


def daemon_up():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.connect(SOCK)
        return True
    except OSError:
        return False
    finally:
        s.close()


def cmd_run(args):
    load_config()
    flipper = Flipper()
    last_seen = {}

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if os.path.exists(SOCK):
        os.unlink(SOCK)
    ctl = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    ctl.bind(SOCK)
    os.chmod(SOCK, 0o600)

    log("running - config " + CONFIG)

    def handle(card, source):
        cfg = load_config()
        now = time.time()
        if now - last_seen.get(card, 0) < cfg.getfloat("general", "cooldown", fallback=3):
            return
        last_seen[card] = now
        log(f"tap from {source}: {card}")
        dispatch(cfg, card)

    try:
        while True:
            fds = [ctl]
            if flipper.connect():
                fds.append(flipper.fd)

            ready, _, _ = select.select(fds, [], [], 2)

            if ctl in ready:
                card = clean_id(ctl.recv(256).decode(errors="replace"))
                if card:
                    handle(card, "command")

            if flipper.fd is not None and flipper.fd in ready:
                for report in flipper.read():
                    card = flipper.card(report)
                    if card:
                        handle(card, "Flipper")
    finally:
        ctl.close()
        if os.path.exists(SOCK):
            os.unlink(SOCK)


def cmd_dump(args):
    flipper = Flipper()
    log("dump mode - tap a card on the Flipper. Ctrl+C to stop.")

    while True:
        if not flipper.connect():
            time.sleep(1)
            continue

        ready, _, _ = select.select([flipper.fd], [], [], 1)
        if not ready:
            continue

        for report in flipper.read():
            card = flipper.card(report)
            extra = f"  -> card {card}" if card else ""
            print(f"report id={report[0]} len={len(report)} data={report.hex()}{extra}", flush=True)


def cmd_tap(args):
    card = clean_id(args.card)
    if not card:
        sys.exit(f"'{args.card}' isn't a card ID - use 16 hex characters "
                 "or a 20-digit Aime access code")

    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(card.encode(), SOCK)
        log("sent " + card)
    except OSError:
        log("cardd isn't running - dispatching directly")
        dispatch(load_config(), card)
    finally:
        s.close()


def cmd_status(args):
    cfg = load_config()
    procs = running()

    print("config:   " + CONFIG)
    print("flipper:  " + (find_flipper() or "not found"))
    print("daemon:   " + ("running" if daemon_up() else "not running"))

    if cfg.has_section("spice"):
        sec = cfg["spice"]
        state = "RUNNING" if is_running(procs, sec.get("processes", "spice64.exe, spice.exe")) else "not running"
        print(f"spice2x:  {state} (api {sec.get('host')}:{sec.get('port')})")

    for section in cfg.sections():
        if not section.startswith("segatools"):
            continue
        sec = cfg[section]
        state = "RUNNING" if is_running(procs, sec.get("processes", "")) else "not running"
        print(f"{section.split(':', 1)[-1] + ':':<10}{state} (tap file {sec.get('tap_file')})")


def main():
    parser = argparse.ArgumentParser(description="Flipper Zero card reader for spice2x and segatools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("dump")
    sub.add_parser("tap").add_argument("card")
    sub.add_parser("status")
    args = parser.parse_args()

    commands = {"run": cmd_run, "dump": cmd_dump, "tap": cmd_tap, "status": cmd_status}
    commands[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
