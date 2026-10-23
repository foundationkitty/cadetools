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

IIDX_ROOT = os.environ.get("IIDX_ROOT", "/mnt/crucial/games/iidx")
WINEPREFIX = os.environ.get("IIDX_PREFIX", os.path.expanduser("~/.local/share/wineprefixes/iidx"))
LAUNCHER = os.environ.get("IIDX_LAUNCH", "")

CLIENT_ID = os.environ.get("IIDX_CLIENT_ID", "1225993043010912258")
IMG_LARGE = os.environ.get("IIDX_IMAGE", "")
LARGE_TEXT = "beatmania IIDX"

PROC_NAMES = ("spice64.exe", "spice.exe")

BULK_LIMIT = 4
BATCH_WINDOW = 0.6
MIN_UPDATE_GAP = 4.0

KNOWN_STRIDES = {20: 0x320, 21: 0x320, 22: 0x340, 23: 0x340, 24: 0x340,
                 25: 0x340, 26: 0x344, 27: 0x52C, 28: 0x52C}

IN_OPEN = 0x20
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
EVENT = struct.Struct("iIII")
SONG_FILE = re.compile(r"^(\d{4,5})(.*)\.([0-9a-z]+)$", re.I)

DEBUG = False


def log(msg):
    print(f"[rpc] {msg}", flush=True)


def dbg(msg):
    if DEBUG:
        print(f"[dbg] {msg}", flush=True)


def banner():
    print("\033[96mbeatmania IIDX \033[95m- Discord Rich Presence "
          "\033[38;2;88;101;242m[ Linux / Wine ]\033[0m\n")


def data_dirs():
    dirs = [os.path.join(IIDX_ROOT, "data")]
    dirs += sorted(glob.glob(os.path.join(IIDX_ROOT, "data_mods", "*")))
    return [d for d in dirs if os.path.isdir(d)]


def sound_ids():
    ids = set()
    for d in data_dirs():
        for path in glob.glob(os.path.join(d, "sound", "*")):
            name = os.path.basename(path)
            if name.isdigit():
                ids.add(int(name))
    return ids


def sjis(raw):
    return raw.split(b"\0", 1)[0].decode("cp932", errors="replace").strip()


def utf16(raw):
    return raw.decode("utf-16-le", errors="replace").split("\0", 1)[0].strip()


def is_text(raw):
    end = raw.find(b"\0")
    if end < 0:
        return False
    try:
        s = raw[:end].decode("cp932")
    except UnicodeDecodeError:
        return False
    return all(ord(c) >= 0x20 for c in s)


def find_stride(data, start, count, ver):
    def score(stride):
        last = min(count, (len(data) - start) // stride)
        if last < count * 0.9:
            return 0
        sample = list(range(min(last, 48))) + list(range(last - 1, 47, -max(1, last // 16)))
        good = 0
        for i in sample:
            off = start + i * stride
            if data[off] != 0 and is_text(data[off:off + 64]) and is_text(data[off + 64:off + 128]):
                good += 1
        return good / len(sample) if sample else 0

    known = KNOWN_STRIDES.get(ver)
    if known and score(known) >= 0.95:
        return known
    for stride in range(0x200, 0x2000):
        if score(stride) >= 0.95:
            return stride
    return None


def parse_v33(data, path):
    magic, ver, count, idx_count = struct.unpack_from("<4sIII", data, 0)
    if magic != b"IIDX" or not count or not idx_count:
        return None

    table_end = 0x10 + idx_count * 4
    body = len(data) - table_end
    if body <= 0 or body % count:
        return None
    stride = body // count
    if stride < 0x400:
        return None
    index = struct.unpack_from(f"<{idx_count}i", data, 0x10)

    sample = range(0, count, max(1, count // 200))
    id_off, best = None, 0
    for off in range(0x300, stride - 3, 4):
        hits = 0
        for i in sample:
            sid = struct.unpack_from("<I", data, table_end + i * stride + off)[0]
            if sid < idx_count and index[sid] == i:
                hits += 1
        if hits > best:
            id_off, best = off, hits

    if best < len(sample) * 0.9:
        log(f"{path}: couldn't find the song ID field (version {ver})")
        return {}
    dbg(f"{os.path.basename(path)}: version {ver}, {count} songs, entry size "
        f"0x{stride:x}, song id at +0x{id_off:x}")

    songs = {}
    for i in range(count):
        off = table_end + i * stride
        sid = struct.unpack_from("<I", data, off + id_off)[0]
        title = utf16(data[off:off + 0x100]) or sjis(data[off + 0x100:off + 0x140])
        genre = utf16(data[off + 0x140:off + 0x1C0])
        artist = utf16(data[off + 0x1C0:off + 0x2C0])
        if not title or (artist == "event_data" and genre == "event_data"):
            continue
        songs[sid] = {"title": title, "artist": artist, "genre": genre}
    return songs


def parse_music_db(path, known_ids):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 0x10:
        return {}

    songs = parse_v33(data, path)
    if songs is not None:
        return songs

    magic, ver, count, idx_count = struct.unpack_from("<4sBxxxHHxxxx", data, 0)
    if magic != b"IIDX" or not count:
        log(f"{path}: not an IIDX music database")
        return {}

    table_end = 0x10 + idx_count * 2
    if table_end >= len(data):
        log(f"{path}: unexpected header (version {ver})")
        return {}
    index = struct.unpack_from(f"<{idx_count}H", data, 0x10)

    stride = find_stride(data, table_end, count, ver)
    if not stride:
        log(f"{path}: can't work out the entry size for version {ver} "
            f"({count} songs, {len(data)} bytes)")
        return {}
    count = min(count, (len(data) - table_end) // stride)
    dbg(f"{os.path.basename(path)}: version {ver}, {count} songs, entry size 0x{stride:x}")

    sample = range(0, count, max(1, count // 200))
    id_off, best = None, 0
    for off in range(256, stride - 1, 2):
        hits = 0
        for i in sample:
            sid = struct.unpack_from("<H", data, table_end + i * stride + off)[0]
            if sid < idx_count and index[sid] == i:
                hits += 1
        if hits > best:
            id_off, best = off, hits

    if id_off is not None and best >= len(sample) * 0.9:
        dbg(f"song id field at +0x{id_off:x}")
        ids = [struct.unpack_from("<H", data, table_end + i * stride + id_off)[0]
               for i in range(count)]
    else:
        owners = {}
        for sid, slot in enumerate(index):
            if slot >= count:
                continue
            if slot not in owners or (sid in known_ids and owners[slot] not in known_ids):
                owners[slot] = sid
        ids = [owners.get(i) for i in range(count)]

    songs = {}
    for i, sid in enumerate(ids):
        if sid is None:
            continue
        off = table_end + i * stride
        title = sjis(data[off:off + 64])
        genre = sjis(data[off + 128:off + 192])
        artist = sjis(data[off + 192:off + 256])
        if not title or (artist == "event_data" and genre == "event_data"):
            continue
        songs[sid] = {"title": title, "artist": artist, "genre": genre}
    return songs


def load_songs():
    known = sound_ids()
    paths = []
    for d in data_dirs():
        paths += sorted(glob.glob(os.path.join(d, "info", "*", "music_data.bin")), reverse=True)
        paths += sorted(glob.glob(os.path.join(d, "info", "*", "music_omni.bin")), reverse=True)

    songs = {}
    for path in paths:
        try:
            found = parse_music_db(path, known)
        except (OSError, struct.error) as e:
            log(f"could not read {path}: {e}")
            continue
        dbg(f"{path}: {len(found)} songs")
        songs.update(found)
    return songs, paths


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
                if f.read().strip().lower() in PROC_NAMES:
                    return int(pid)
        except OSError:
            continue
    return None


def launch():
    if LAUNCHER:
        path = os.path.expanduser(LAUNCHER)
        if not os.access(path, os.X_OK):
            log(f"launcher {path} not found or not executable.")
            return None
        log(f"launched {path}")
        return subprocess.Popen([path], start_new_session=True)

    exe = os.path.join(IIDX_ROOT, "spice64.exe")
    if not os.path.exists(exe):
        log(f"{exe} not found - set IIDX_ROOT or IIDX_LAUNCH.")
        return None

    env = dict(os.environ, WINEPREFIX=WINEPREFIX, LANG="ja_JP.UTF-8")
    try:
        proc = subprocess.Popen(["wine", "spice64.exe"], cwd=IIDX_ROOT, env=env,
                                start_new_session=True)
    except FileNotFoundError:
        log("'wine' not found - run this from a shell that has Wine.")
        return None
    log(f"launched wine spice64.exe (prefix {WINEPREFIX})")
    return proc


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
        for d in data_dirs():
            sound = os.path.join(d, "sound")
            if os.path.isdir(sound):
                folders.append(sound)
                folders += glob.glob(os.path.join(sound, "*"))

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


def classify(name):
    m = SONG_FILE.match(name)
    if not m:
        return None
    sid, rest, ext = int(m.group(1)), m.group(2).lower(), m.group(3).lower()
    if "pre" in rest:
        return "preview", sid
    if ext in ("1", "2dx", "s3p"):
        return "play", sid
    return None


class State:
    def __init__(self, discord, songs):
        self.discord = discord
        self.songs = songs
        self.sid = None
        self.scene = None
        self.start = int(time.time())

    def title(self, sid):
        song = self.songs.get(sid)
        return song["title"] if song else f"Song #{sid}"

    def artist(self, sid):
        song = self.songs.get(sid)
        return (song["artist"] or song["genre"]) if song else ""

    def push(self, details, state):
        def fit(text):
            text = (text or "")[:128]
            return text if len(text) >= 2 else (text + "  ")[:2]

        activity = {"details": fit(details), "state": fit(state),
                    "timestamps": {"start": self.start}}
        if IMG_LARGE:
            activity["assets"] = {"large_image": IMG_LARGE, "large_text": LARGE_TEXT}
        self.discord.set(activity)

    def menu(self):
        if self.scene == "menu":
            return
        self.scene = "menu"
        self.start = int(time.time())
        self.push("In Menus", "Navigating...")

    def selecting(self, sid):
        if self.scene == "select" and sid == self.sid:
            return
        if self.scene != "select":
            self.start = int(time.time())
        self.scene = "select"
        self.sid = sid
        title, artist = self.title(sid), self.artist(sid)
        self.push("Selecting Track...", f"{title} / {artist}" if artist else title)

    def playing(self, sid):
        if self.scene == "play" and sid == self.sid:
            return
        self.scene = "play"
        self.sid = sid
        self.start = int(time.time())
        self.push(f"Playing: {self.title(sid)}", self.artist(sid) or "beatmania IIDX")


def run(args, songs):
    discord = Discord(CLIENT_ID, enabled=not args.dump)
    state = State(discord, songs)
    watcher = Watcher()

    proc = None
    if not args.no_launch:
        proc = launch()
        if proc is None:
            return 1
    launched_at = time.time()

    pid = None
    seen = False
    last_seen = 0
    batch = []
    batch_start = 0
    next_poll = 0

    log("running - press Ctrl+C to stop")
    while True:
        now = time.time()
        if now >= next_poll:
            next_poll = now + 1
            if pid and not os.path.exists(f"/proc/{pid}"):
                pid = None
            if pid is None:
                pid = find_game_pid()
                if pid and not seen:
                    log(f"found game (pid {pid})")
                    state.menu()
                seen = seen or bool(pid)

            if pid:
                last_seen = now
            elif seen and now - last_seen > 10:
                log("game closed")
                seen = False
                state.scene = None
                discord.set(None)
                if proc:
                    break
            elif proc and not seen and now - launched_at > 120:
                log("game never showed up - check the launch command.")
                break

        events = watcher.read(0.2)
        if events:
            if not batch:
                batch_start = time.time()
            batch += events

        if batch and time.time() - batch_start >= BATCH_WINDOW:
            hits = [hit for hit in (classify(name) for _, name in batch) if hit]
            ids = {sid for _, sid in hits}

            if args.dump:
                for folder, name in batch:
                    print(f"  open: {os.path.relpath(os.path.join(folder, name), IIDX_ROOT)}",
                          flush=True)
                line = f"  -- batch: {len(batch)} opens, {len(ids)} songs"
                if len(ids) > BULK_LIMIT:
                    line += " (bulk, ignored)"
                elif ids:
                    line += ": " + ", ".join(state.title(i) for i in sorted(ids))
                print(line, flush=True)

            if hits and len(ids) <= BULK_LIMIT and (pid or args.no_launch):
                plays = [sid for kind, sid in hits if kind == "play"]
                if plays:
                    dbg(f"song files -> play {plays[-1]}")
                    state.playing(plays[-1])
                else:
                    dbg(f"preview -> select {hits[-1][1]}")
                    state.selecting(hits[-1][1])
            batch = []

        discord.flush()

    discord.close()
    return 0


def main():
    global DEBUG

    parser = argparse.ArgumentParser(description="beatmania IIDX Rich Presence (Linux/Wine)")
    parser.add_argument("--no-launch", action="store_true",
                        help="don't start the game, just watch for it")
    parser.add_argument("--dump", action="store_true",
                        help="print the song files the game opens, no Discord")
    parser.add_argument("--debug", action="store_true", help="print detector activity")
    args = parser.parse_args()
    DEBUG = args.debug

    banner()
    if not os.path.isdir(os.path.join(IIDX_ROOT, "data")):
        log(f"{IIDX_ROOT}/data not found - set IIDX_ROOT.")
        return 1

    songs, paths = load_songs()
    log(f"loaded {len(songs)} songs from {len(paths)} music database file(s)")
    if not songs:
        log("no songs parsed - presence will show song IDs instead of titles.")
    if args.dump:
        log("dump mode - go to song select, scroll, play a song. Ctrl+C to stop.")
    return run(args, songs)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[rpc] stopped")
