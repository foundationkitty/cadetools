#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
import time
import traceback

try:
    from pypresence import Presence
except ImportError:
    sys.exit("pypresence is not installed. In nix-shell:\n"
             "  (python3.withPackages (ps: [ ps.pypresence ]))")

CLIENT_ID = os.environ.get("SDVX_CLIENT_ID", "1475931399554076875")
GAME_EXECUTABLE = os.environ.get("SDVX_EXE", "spice64.exe")
WINE = os.environ.get("SDVX_WINE", "wine")

GAME_NAME = "Sound Voltex"
LARGE_TEXT = "∇"
IMG_MENU = "nabla_logo"
IMG_PLAYING = "nabla_logo"

VERSION_DIR = "ver06"
LOG_CANDIDATES = ["log.txt", "spice.log", "../log.txt"]
MUSIC_DB_CANDIDATES = [
    "data/others/music_db.xml",
    "../data/others/music_db.xml",
    "others/music_db.xml",
    "music_db.xml",
]

SCENE_SELECT = "in MUSICSELECT"
SCENE_PLAY = "in ALTERNATIVE_GAME_SCENE"
SCENE_RESULT = "in RESULT_SCENE"
SCENE_TOTAL = "in T_RESULT_SCENE"
SCENE_MENU = ("in GAMEOVER", "in CARD_OUT_SCENE", "in TITLEDEMO")

EVENTS = [
    ("Hexa Diver", ("LoadingIFS", "hexa_diver", "blue")),
]

MODES = [
    ("standard_plus", "Normal Start"),
    ("premium", "Premium Time"),
    ("blaster", "Blaster Start"),
    ("paradise", "Paradise Start"),
    ("megamix", "MegaMix Battle"),
    ("arena", "Arena Battle"),
    ("standard", "Normal Start"),
    ("light", "Light Start"),
]

DUMP_KEYWORDS = (
    "SCENE", "MUSICSELECT", "GAMEOVER", "TITLEDEMO",
    "LoadingIFS", "Loading /data/music/", "ea3_report_posev",
)

DEBUG = False


def log(msg):
    print(f"[rpc] {msg}", flush=True)


def dbg(msg):
    if DEBUG:
        print(f"[dbg] {msg}", flush=True)


def print_banner():
    os.system("clear")
    C, M = "\033[96m", "\033[95m"
    P, R = "\033[38;2;88;101;242m", "\033[0m"
    print(f"{C} ___  ___  _   _ __  __ ___    _   _ ___  ___ _  _____ _____  __")
    print(f"{M} Sound Voltex {C}∇{M} Nabla {P}- Discord Rich Presence{R}")
    print(f"{P} [ Linux / Wine ]{R}\n")


def find_first(paths):
    for path in paths:
        if os.path.exists(path):
            return path
    return None


def load_song_map():
    xml_path = find_first(MUSIC_DB_CANDIDATES)
    if not xml_path:
        log("music_db.xml not found - song IDs will show instead of titles.")
        return {}

    try:
        with open(xml_path, encoding="cp932", errors="ignore") as f:
            content = f.read()
    except OSError as e:
        log(f"could not read {xml_path}: {e}")
        return {}

    pattern = r'<music id="(\d+)">.*?<title_name>(.*?)</title_name>'
    songs = {int(mid): name.strip()
             for mid, name in re.findall(pattern, content, re.DOTALL)}

    log(f"loaded {len(songs)} songs from {xml_path}")
    if not songs:
        log("parsed 0 songs - Nabla may use a different music_db layout.")
    return songs


class Discord:
    def __init__(self, client_id):
        self.client_id = client_id
        self.rpc = None
        self.last = None
        self.retry_at = 0.0
        self.connect()

    def connect(self):
        if time.time() < self.retry_at:
            return False
        try:
            self.rpc = Presence(self.client_id)
            self.rpc.connect()
            log("connected to Discord")
            if self.last:
                self.rpc.update(**self.last)
            return True
        except Exception as e:
            self.rpc = None
            self.retry_at = time.time() + 15
            dbg(f"discord connect failed: {type(e).__name__}: {e}")
            return False

    def update(self, **kwargs):
        self.last = kwargs
        if self.rpc is None and not self.connect():
            return
        try:
            self.rpc.update(**kwargs)
            dbg(f"update {kwargs.get('details')!r} / {kwargs.get('state')!r}")
        except Exception as e:
            log(f"lost Discord connection ({type(e).__name__}), will retry")
            self.rpc = None
            self.retry_at = time.time() + 5


def resolve_log(explicit):
    return (explicit or os.environ.get("SDVX_LOG")
            or find_first(LOG_CANDIDATES) or "log.txt")


def tail(path):
    f, pos, announced = None, 0, False
    while True:
        try:
            if f is None:
                if not os.path.exists(path):
                    if not announced:
                        log(f"waiting for {path} ...")
                        announced = True
                    time.sleep(0.5)
                    continue
                f = open(path, encoding="utf-8", errors="replace")
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                log(f"following {path}")
                announced = False

            line = f.readline()
            if line:
                pos = f.tell()
                yield line.rstrip("\r\n")
                continue

            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size < pos:
                dbg("log truncated, reopening")
                f.close()
                f = None
            else:
                time.sleep(0.1)
        except GeneratorExit:
            if f:
                f.close()
            raise
        except Exception as e:
            log(f"tail error: {type(e).__name__}: {e}")
            try:
                if f:
                    f.close()
            except Exception:
                pass
            f = None
            time.sleep(1)


def launch_game():
    if not os.path.exists(GAME_EXECUTABLE):
        log(f"{GAME_EXECUTABLE} not found - run this from the game folder.")
        return False
    try:
        subprocess.Popen(
            [WINE, GAME_EXECUTABLE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log(f"launched: {WINE} {GAME_EXECUTABLE}")
        return True
    except FileNotFoundError:
        log(f"'{WINE}' not found. Enter a shell that provides Wine first.")
        return False
    except Exception as e:
        log(f"failed to launch: {type(e).__name__}: {e}")
        return False


class State:
    def __init__(self, discord, songs):
        self.discord = discord
        self.songs = songs
        self.song = "..."
        self.scene = "Menu"
        self.mode = ""
        self.event = ""
        self.start = time.time()

    def details(self, verb=None):
        text = self.event or GAME_NAME
        if verb:
            text = f"{verb} {text}"
        if self.mode:
            text += f" ({self.mode})"
        return text

    def image(self):
        return IMG_MENU if self.scene in ("Menu", "Selecting") else IMG_PLAYING

    def to_menu(self):
        if self.scene == "Menu":
            return
        self.scene, self.mode, self.event = "Menu", "", ""
        self.discord.update(state="In Menu", details=GAME_NAME,
                            large_image=IMG_MENU, start=time.time())

    def to_selecting(self):
        if self.scene == "Selecting":
            return
        self.scene = "Selecting"
        state = f"Selecting: {self.song}"
        if self.song in ("Browsing...", "..."):
            state = "Choosing Song..."
        self.discord.update(state=state, details=self.details("Selecting"),
                            large_image=self.image(), large_text=LARGE_TEXT)

    def to_playing(self):
        if self.scene == "Playing":
            return
        self.scene = "Playing"
        self.start = time.time()
        state = f"Playing: {self.song}"
        if self.song in ("Browsing...", "..."):
            state = "Loading..."
        self.discord.update(state=state, details=self.details("Playing"),
                            large_image=self.image(), large_text=self.song,
                            start=self.start)

    def to_results(self):
        if self.scene == "Results":
            return
        self.scene = "Results"
        self.discord.update(state=f"Result: {self.song}",
                            details=self.details(), large_image=IMG_PLAYING,
                            large_text=self.song, start=self.start)

    def to_total_results(self):
        if self.scene == "TotalResults":
            return
        self.scene = "TotalResults"
        self.discord.update(state="Session Results", details=GAME_NAME,
                            large_image=IMG_MENU)

    def set_song(self, song_id):
        name = self.songs.get(song_id, str(song_id))
        if name == self.song:
            return
        self.song = name
        if self.scene == "Playing":
            self.discord.update(state=f"Playing: {name}",
                                details=self.details("Playing"),
                                large_image=self.image(), large_text=name,
                                start=self.start)
        elif self.scene == "Selecting":
            self.discord.update(state=f"Selecting: {name}",
                                details=self.details("Selecting"),
                                large_image=self.image(), large_text=name)

    def enter_event(self, name):
        if self.event == name:
            return
        self.event = name
        self.song, self.scene = "Browsing...", "Selecting"
        self.discord.update(state="Choosing Song...", details=self.details(),
                            large_image=IMG_MENU, large_text=LARGE_TEXT)


def handle(line, st):
    if "ea3_report_posev" in line and "/coin/kfc_game_s_" in line:
        for needle, label in MODES:
            if needle in line:
                st.mode = label
                dbg(f"mode -> {label}")
                break

    for name, needles in EVENTS:
        if all(n in line for n in needles):
            st.enter_event(name)
    if st.event and "LoadingIFS" in line and f"{VERSION_DIR}/ms_sel" in line:
        dbg(f"event {st.event!r} ended")
        st.event = ""

    if "Loading /data/music/" in line and ".png" in line:
        if "_b.png" in line or st.scene == "Playing" or st.event:
            m = re.search(r"music/(\d+)_", line)
            if m:
                st.set_song(int(m.group(1)))

    if SCENE_SELECT in line:
        if st.event and "ms_sel" in line:
            st.event = ""
        st.to_selecting()
    elif SCENE_PLAY in line:
        st.to_playing()
    elif SCENE_TOTAL in line:
        st.to_total_results()
    elif SCENE_RESULT in line:
        st.to_results()
    elif any(marker in line for marker in SCENE_MENU):
        st.to_menu()


def main():
    global DEBUG

    ap = argparse.ArgumentParser(
        description="SDVX Nabla Rich Presence (Linux/Wine)")
    ap.add_argument("--no-launch", action="store_true",
                    help="don't start the game, just follow the log")
    ap.add_argument("--log", help="path to spice2x's log file")
    ap.add_argument("--debug", action="store_true",
                    help="print parser activity")
    ap.add_argument("--dump", action="store_true",
                    help="print interesting raw log lines, no Discord")
    args = ap.parse_args()
    DEBUG = args.debug

    print_banner()

    if args.dump:
        log("dump mode - showing scene/song/mode lines. Ctrl+C to stop.")
        for line in tail(resolve_log(args.log)):
            if any(k in line for k in DUMP_KEYWORDS):
                print(line, flush=True)
        return 0

    if not os.environ.get("XDG_RUNTIME_DIR"):
        log("XDG_RUNTIME_DIR unset - Discord's socket may not be findable.")

    song_map = load_song_map()
    discord = Discord(CLIENT_ID)
    st = State(discord, song_map)

    if not args.no_launch and not launch_game():
        return 1

    discord.update(state="In Menu", details=GAME_NAME,
                   large_image=IMG_MENU, start=st.start)

    log("running - press Ctrl+C to stop")
    for line in tail(resolve_log(args.log)):
        try:
            handle(line, st)
        except Exception as e:
            dbg(f"parse error on {line[:80]!r}: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[rpc] stopped")
    except Exception:
        traceback.print_exc()
        input("Press Enter to exit...")
