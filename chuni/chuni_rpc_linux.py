#!/usr/bin/env python3
import argparse
import ctypes
import glob
import json
import os
import re
import select
import socket
import struct
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

CHUNI_ROOT = os.environ.get("CHUNI_ROOT", "/mnt/crucial/games/chunithm")
LAUNCHER = os.path.expanduser(
    os.environ.get("CHUNI_LAUNCH", "/mnt/crucial/games/chunithm/App/bin/chunithm.sh"))

CLIENT_ID = os.environ.get("CHUNI_CLIENT_ID", "1562985238081044542")
IMG_LARGE = "logo_large_square"
LARGE_TEXT = "CHUNITHM"

PROC_PREFIX = "chusanapp"

MEM_ENABLED = True
MEM_BASE_OFFSET = 0x0185F37C
MUSIC_OFFSETS = [0x3C, 0x10, 0x1B4, 0x2B4]
DIFF_OFFSETS = [0x3C, 0x10, 0x1B4, 0x2B0]
DEFAULT_IMAGE_BASE = 0x00400000

BULK_LIMIT = 6
BATCH_WINDOW = 0.6
MIN_UPDATE_GAP = 4.0

DIFF_NAMES = {0: "BASIC", 1: "ADVANCED", 2: "EXPERT", 3: "MASTER",
              4: "ULTIMA", 5: "WORLD'S END"}

IN_OPEN = 0x20
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
EVENT = struct.Struct("iIII")

CHART_FILE = re.compile(r"^(\d+)_(\d+)\.c2s$", re.I)
CUE_DIR = re.compile(r"cueFile0*(\d+)$", re.I)
MUSIC_DIR = re.compile(r"music0*(\d+)$", re.I)

DEBUG = False


def log(msg):
    print(f"[rpc] {msg}", flush=True)


def dbg(msg):
    if DEBUG:
        print(f"[dbg] {msg}", flush=True)


def banner():
    print("\033[96mCHUNITHM \033[95m- Discord Rich Presence "
          "\033[38;2;88;101;242m[ Linux / Wine ]\033[0m\n")


def text(node, path):
    el = node.find(path)
    if el is None or not el.text:
        return ""
    return el.text.strip()


def data_roots():
    roots = sorted(glob.glob(os.path.join(CHUNI_ROOT, "App", "data", "A*")))
    roots += sorted(glob.glob(os.path.join(CHUNI_ROOT, "Option", "A*")))
    return [r for r in roots if os.path.isdir(r)]


def load_songs():
    songs = {}
    cues = {}

    for root in data_roots():
        for path in glob.glob(os.path.join(root, "music", "*", "Music.xml")):
            try:
                doc = ET.parse(path).getroot()
            except (ET.ParseError, OSError) as e:
                dbg(f"skip {path}: {e}")
                continue

            try:
                mid = int(text(doc, "name/id"))
            except ValueError:
                continue

            levels = {}
            for fumen in doc.iter("MusicFumenData"):
                if text(fumen, "enable").lower() == "false":
                    continue
                try:
                    kind = int(text(fumen, "type/id"))
                    lv = int(text(fumen, "level") or 0)
                    dec = int(text(fumen, "levelDecimal") or 0)
                except ValueError:
                    continue
                plus = "+" if dec >= 50 else ""
                levels[kind] = f"{lv}{plus} ({lv + dec / 100:.1f})"

            songs[mid] = {
                "title": text(doc, "name/str") or f"#{mid}",
                "artist": text(doc, "artistName/str"),
                "we_tag": text(doc, "worldsEndTagName/str"),
                "levels": levels,
            }

            try:
                cues[int(text(doc, "cueFileName/id"))] = mid
            except ValueError:
                pass

    return songs, cues


class Discord:
    def __init__(self, client_id, enabled=True):
        self.client_id = client_id
        self.enabled = enabled
        self.sock = None
        self.pending = None
        self.sent = object()
        self.last_send = 0
        self.retry_at = 0

    def paths(self):
        base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
        subdirs = ("", "app/com.discordapp.Discord", "app/dev.vencord.Vesktop",
                   ".flatpak/dev.vencord.Vesktop/xdg-run",
                   ".flatpak/com.discordapp.Discord/xdg-run", "snap.discord")
        for sub in subdirs:
            for i in range(10):
                yield os.path.join(base, sub, f"discord-ipc-{i}")

    def send(self, op, payload):
        data = json.dumps(payload).encode()
        self.sock.sendall(struct.pack("<II", op, len(data)) + data)

    def recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed")
            buf += chunk
        return buf

    def recv(self):
        op, length = struct.unpack("<II", self.recv_exact(8))
        return op, json.loads(self.recv_exact(length) or b"{}")

    def connect(self):
        if not self.enabled or self.sock or time.time() < self.retry_at:
            return self.sock is not None

        for path in self.paths():
            if not os.path.exists(path):
                continue
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            try:
                s.connect(path)
                self.sock = s
                self.send(0, {"v": 1, "client_id": self.client_id})
                _, data = self.recv()
                if data.get("evt") != "READY":
                    raise ConnectionError(f"handshake refused: {data}")
            except Exception as e:
                dbg(f"{path}: {type(e).__name__}: {e}")
                s.close()
                self.sock = None
                continue
            log(f"connected to Discord ({path})")
            self.sent = object()
            return True

        self.retry_at = time.time() + 15
        dbg("no Discord IPC socket answered, retrying in 15 s")
        return False

    def set(self, activity):
        self.pending = activity

    def flush(self):
        if not self.enabled or self.pending == self.sent:
            return
        if time.time() - self.last_send < MIN_UPDATE_GAP:
            return
        if not self.connect():
            return

        try:
            self.send(1, {"cmd": "SET_ACTIVITY", "nonce": str(uuid.uuid4()),
                          "args": {"pid": os.getpid(), "activity": self.pending}})
            self.recv()
        except Exception as e:
            log(f"lost Discord connection ({type(e).__name__}), will retry")
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self.retry_at = time.time() + 5
            return

        self.sent = self.pending
        self.last_send = time.time()
        if self.pending:
            dbg(f"update {self.pending.get('details')!r} / {self.pending.get('state')!r}")

    def close(self):
        if not self.sock:
            return
        try:
            self.pending = None
            self.last_send = 0
            self.flush()
            self.sock.close()
        except Exception:
            pass


def find_game_pid():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip().lower().startswith(PROC_PREFIX):
                    return int(pid)
        except OSError:
            continue
    return None


def image_base(pid):
    try:
        with open(f"/proc/{pid}/maps") as f:
            starts = [int(line.split("-", 1)[0], 16) for line in f
                      if re.search(r"chusanapp[^/]*\.exe$", line.strip(), re.I)]
    except OSError:
        return DEFAULT_IMAGE_BASE
    return min(starts) if starts else DEFAULT_IMAGE_BASE


class Memory:
    def __init__(self, pid):
        self.pid = pid
        self.base = image_base(pid)
        self.f = None
        self.error = None
        try:
            self.f = open(f"/proc/{pid}/mem", "rb", buffering=0)
        except PermissionError:
            self.error = "permission"
        except OSError as e:
            self.error = str(e)
        dbg(f"image base 0x{self.base:08x}, mem open: {self.error or 'ok'}")

    def u32(self, addr):
        self.f.seek(addr)
        data = self.f.read(4)
        if len(data) != 4:
            raise OSError("short read")
        return struct.unpack("<I", data)[0]

    def chain(self, offsets):
        if not self.f:
            return None
        try:
            addr = self.u32(self.base + MEM_BASE_OFFSET)
            for off in offsets[:-1]:
                addr = self.u32(addr + off)
            val = self.u32(addr + offsets[-1])
        except (OSError, ValueError, OverflowError):
            return None
        return val - (1 << 32) if val & 0x80000000 else val

    def close(self):
        if self.f:
            self.f.close()


def ptrace_hint():
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope") as f:
            scope = f.read().strip()
    except OSError:
        scope = "?"
    log(f"can't read the game's memory (kernel.yama.ptrace_scope = {scope}).")
    log("  launch the game through this script (no --no-launch), or set")
    log('  boot.kernel.sysctl."kernel.yama.ptrace_scope" = 0; in NixOS.')
    log("  File watching still works without it.")


class Watcher:
    def __init__(self):
        self.fd = None
        self.watches = {}

        try:
            libc = ctypes.CDLL(None, use_errno=True)
            self.add = libc.inotify_add_watch
            self.add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
            if fd < 0:
                raise OSError(ctypes.get_errno(), "inotify_init1")
            self.fd = fd
        except (OSError, AttributeError) as e:
            log(f"file watching unavailable: {e}")
            return

        folders = []
        for root in data_roots():
            folders += glob.glob(os.path.join(root, "music", "*"))
            folders += glob.glob(os.path.join(root, "cueFile", "*"))

        for folder in folders:
            if not os.path.isdir(folder):
                continue
            wd = self.add(self.fd, folder.encode(), IN_OPEN)
            if wd < 0:
                if ctypes.get_errno() == 28:
                    log("ran out of inotify watches - raise them with")
                    log('  boot.kernel.sysctl."fs.inotify.max_user_watches" = 524288;')
                    break
                continue
            self.watches[wd] = folder
        log(f"watching {len(self.watches)} song folders")

    def read(self, timeout):
        if self.fd is None:
            time.sleep(timeout)
            return []

        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return []
        try:
            buf = os.read(self.fd, 1 << 16)
        except BlockingIOError:
            return []

        events = []
        i = 0
        while i + EVENT.size <= len(buf):
            wd, _, _, length = EVENT.unpack_from(buf, i)
            name = buf[i + EVENT.size:i + EVENT.size + length]
            i += EVENT.size + length
            name = name.split(b"\0", 1)[0].decode(errors="replace")
            if wd in self.watches and name:
                events.append((self.watches[wd], name))
        return events


def classify(folder, name, cues):
    base = os.path.basename(folder)

    m = CHART_FILE.match(name)
    if m:
        dm = MUSIC_DIR.search(base)
        mid = int(dm.group(1)) if dm else int(m.group(1))
        return "chart", mid, int(m.group(2))

    lower = name.lower()
    if lower.endswith(".awb") and not lower.startswith("systemvoice"):
        cm = CUE_DIR.search(base)
        if cm:
            cue = int(cm.group(1))
            return "preview", cues.get(cue, cue), None

    return None


class State:
    def __init__(self, discord, songs):
        self.discord = discord
        self.songs = songs
        self.mid = None
        self.diff = 0
        self.scene = None
        self.start = int(time.time())
        self.mem_locked = False

    def song_line(self, mid):
        song = self.songs.get(mid)
        if not song:
            return f"Song #{mid}"
        if song["artist"]:
            return f"{song['artist']} - {song['title']}"
        return song["title"]

    def diff_line(self, mid, diff):
        song = self.songs.get(mid, {})
        if diff == 5 or (song.get("we_tag") and mid >= 8000):
            return f"WORLD'S END [{song.get('we_tag') or '?'}]"
        lv = song.get("levels", {}).get(diff, "?")
        return f"{DIFF_NAMES.get(diff, 'UNKNOWN')} {lv}"

    def push(self, details, state):
        def fit(s):
            s = s[:128]
            return s if len(s) >= 2 else s + "  "

        self.discord.set({
            "details": fit(details),
            "state": fit(state),
            "timestamps": {"start": self.start},
            "assets": {"large_image": IMG_LARGE, "large_text": LARGE_TEXT},
        })

    def menu(self):
        if self.scene == "menu":
            return
        self.scene = "menu"
        self.start = int(time.time())
        self.push("In Menus", "Navigating...")

    def selecting(self, mid):
        if mid not in self.songs:
            return
        if self.scene == "select" and mid == self.mid:
            return
        if self.scene != "select":
            self.start = int(time.time())
        self.scene = "select"
        self.mid = mid
        self.push("Selecting Track...", self.song_line(mid))

    def playing(self, mid, diff):
        if mid not in self.songs:
            return
        if self.scene == "play" and (mid, diff) == (self.mid, self.diff):
            return
        self.scene = "play"
        self.mid = mid
        self.diff = diff
        self.start = int(time.time())
        self.push(f"Playing: {self.song_line(mid)}", self.diff_line(mid, diff))


def run(args, songs, cues):
    discord = Discord(CLIENT_ID, enabled=not args.dump)
    state = State(discord, songs)
    watcher = Watcher()

    proc = None
    if not args.no_launch:
        if not os.access(LAUNCHER, os.X_OK):
            log(f"launcher {LAUNCHER} not found or not executable "
                "(set CHUNI_LAUNCH, or use --no-launch).")
            return 1
        proc = subprocess.Popen([LAUNCHER])
        log(f"launched {LAUNCHER}")

    pid = None
    mem = None
    warned = False
    last_mem = None
    batch = []
    batch_start = 0
    next_poll = 0

    log("running - press Ctrl+C to stop")
    while True:
        if proc and proc.poll() is not None:
            log("game launcher exited")
            break

        now = time.time()
        if now >= next_poll:
            next_poll = now + 1

            if pid and not os.path.exists(f"/proc/{pid}"):
                log("game closed")
                pid = None
                if mem:
                    mem.close()
                mem = None
                state.mem_locked = False
                state.scene = None
                discord.set(None)

            if pid is None:
                pid = find_game_pid()
                if pid:
                    log(f"found game (pid {pid})")
                    state.menu()
                    if MEM_ENABLED:
                        mem = Memory(pid)
                        if mem.error == "permission" and not warned:
                            ptrace_hint()
                            warned = True

            if mem and mem.f:
                m_id = mem.chain(MUSIC_OFFSETS)
                d_id = mem.chain(DIFF_OFFSETS)
                if args.dump and (m_id, d_id) != last_mem:
                    known = "  <- known song" if m_id in songs else ""
                    print(f"  mem: music={m_id} diff={d_id}{known}", flush=True)
                last_mem = (m_id, d_id)

                if m_id in songs and not state.mem_locked:
                    state.mem_locked = True
                    log("memory pointers look valid - using memory method")

                if state.mem_locked and m_id is not None:
                    if m_id == -1 and state.mid is not None:
                        state.playing(state.mid, state.diff)
                    elif m_id in songs:
                        state.diff = d_id if d_id in DIFF_NAMES else 0
                        state.selecting(m_id)
                    else:
                        state.menu()

        events = watcher.read(0.2)
        if events:
            if not batch:
                batch_start = time.time()
            batch += events

        if batch and time.time() - batch_start >= BATCH_WINDOW:
            hits = [hit for hit in (classify(f, n, cues) for f, n in batch) if hit]
            ids = {hit[1] for hit in hits}

            if args.dump:
                for folder, name in batch:
                    print(f"  open: {os.path.relpath(os.path.join(folder, name), CHUNI_ROOT)}",
                          flush=True)
                bulk = " (bulk, ignored)" if len(ids) > BULK_LIMIT else ""
                print(f"  -- batch: {len(batch)} opens, {len(ids)} songs{bulk}", flush=True)

            if hits and len(ids) <= BULK_LIMIT and pid and not state.mem_locked:
                charts = [hit for hit in hits if hit[0] == "chart"]
                if charts:
                    _, mid, diff = charts[-1]
                    dbg(f"chart open -> play {mid} diff {diff}")
                    state.playing(mid, diff)
                else:
                    _, mid, _ = hits[-1]
                    dbg(f"preview open -> select {mid}")
                    state.selecting(mid)
            batch = []

        discord.flush()

    discord.close()
    return 0


def main():
    global DEBUG

    parser = argparse.ArgumentParser(description="CHUNITHM Rich Presence (Linux/Wine)")
    parser.add_argument("--no-launch", action="store_true",
                        help="don't start the game, just watch for it")
    parser.add_argument("--dump", action="store_true",
                        help="print what both detectors see, no Discord")
    parser.add_argument("--debug", action="store_true", help="print detector activity")
    args = parser.parse_args()
    DEBUG = args.debug

    banner()
    if not os.path.isdir(CHUNI_ROOT):
        log(f"{CHUNI_ROOT} not found - set CHUNI_ROOT.")
        return 1

    songs, cues = load_songs()
    log(f"loaded {len(songs)} songs from {len(data_roots())} data folders")
    if not songs:
        log("no Music.xml parsed - check CHUNI_ROOT.")
    if args.dump:
        log("dump mode - go to song select, scroll, play one song. Ctrl+C to stop.")
    return run(args, songs, cues)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[rpc] stopped")
