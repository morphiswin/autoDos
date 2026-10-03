"""AutoDOS — A lightweight personal DOS game launcher."""

import ctypes
import json
import re
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
import tkinter.ttk as ttk

# ── Constants ────────────────────────────────────────────────────────────────
import sys as _sys
# BASE_DIR  = folder where AutoDOS.exe (or autodos_win.py) lives
#             used for: dosbox\, tools\, games\, library.json
# ASSET_DIR = where bundled read-only assets are extracted
#             used for: logo.png, dosbox.conf, master_games.json
if getattr(_sys, "frozen", False):
    BASE_DIR  = Path(_sys.executable).parent
    ASSET_DIR = Path(_sys._MEIPASS)
else:
    BASE_DIR  = Path(__file__).parent
    ASSET_DIR = BASE_DIR
GAMES_DIR            = BASE_DIR / "games"
CONTROLLER_MAPS_DIR  = BASE_DIR / "controller_maps"
LIBRARY_FILE    = BASE_DIR / "library.json"
LOGO_FILE       = ASSET_DIR / "logo.png"
ICON_FILE       = ASSET_DIR / "icon.ico"

DOSBOX_BINS     = ["dosbox-staging", "dosbox"]
TOOLS_DIR       = BASE_DIR / "tools"
EXODOS_FILE     = ASSET_DIR / "master_games.json"
DOSBOX_FLATPAK  = "io.github.dosbox-staging"

# Windows bundled paths
DOSBOX_WIN_PATH = BASE_DIR / "dosbox" / "dosbox.exe"
TOOLS_7ZA       = TOOLS_DIR / "7za.exe"
TOOLS_UNRAR     = TOOLS_DIR / "unrar.exe"
# Run console tools like 7za.exe without flashing up a console window
_NO_WINDOW      = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Dark theme
BG              = "#1e1e1e"   # window
LIST_BG         = "#252526"   # lists and text boxes
ALT_ROW_BG      = "#2b2b2c"   # every other list row
SEL_BG          = "#264f78"   # selected row
BTN_BG          = "#333337"
BTN_ACTIVE      = "#45454a"   # button under the mouse
BTN_PRESSED     = "#55555b"
BORDER          = "#3f3f46"
TEXT            = "#e8e8e8"
MUTED           = "#8b8b8b"
DISABLED        = "#6b6b6b"
ACCENT          = "#3794ff"   # pad button being pressed
GOOD            = "#73c991"   # ExoDOS match found

SETUP_NAMES     = {"setup", "install", "config", "unins", "uninst", "uninstall"}
LAUNCHER_NAMES  = {"setup.exe", "install.exe", "play.exe", "start.exe", "launch.exe"}

# Default to All Files so user sees everything without switching filters
ARCHIVE_FILTERS = [
    ("All Files", "*.*"),
    ("Archives", "*.zip *.7z *.rar *.tar.gz *.tgz"),
]

# ── CD / ISO Support ──────────────────────────────────────────────────────────
# CD formats DOSBox Staging can mount: ISO, CUE+BIN, CUE+ISO, and CUE+ISO with
# FLAC/OPUS/OGG/MP3/WAV audio tracks (no CHD, MDF/MDS or CCD)
DISC_EXTS = {".cue", ".iso", ".bin", ".img"}

# Folders games keep their CD images in (not 'disk' folders, which usually
# hold floppy images)
GAME_CD_FOLDERS = {"cd", "cds", "cdrom", "cd-rom", "cd1", "cd2", "cd3", "cd4",
                   "disc", "disc1", "disc2", "disc3", "disc4", "dvd"}


def _natural_key(name: str) -> list:
    """Sort key that puts 'Disc 2' before 'Disc 10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _disc_order(path: Path) -> tuple:
    return _natural_key(path.name), _natural_key(path.parent.name)


def cue_files(cue: Path) -> list:
    """Paths of the files a .cue sheet loads (its .bin/.iso data track and any
    audio tracks), in order."""
    data = cue.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    files = []
    for line in text.splitlines():
        m = re.match(r'\s*FILE\s+(?:"([^"]+)"|(\S+))', line, re.IGNORECASE)
        if m:
            files.append(os.path.normpath(str(cue.parent / (m.group(1) or m.group(2)))))
    return files


def drop_cue_tracks(images: list) -> list:
    """Leave out the files a .cue in the list loads itself, so a CUE+BIN set
    mounts as one disc. A .bin named like a .cue beside it counts as that
    cue's track even if the cue sheet can't be read."""
    tracks, cue_stems = set(), set()
    for f in images:
        if f.suffix.lower() == ".cue":
            try:
                tracks.update(os.path.normcase(p) for p in cue_files(f))
            except OSError:
                pass
            cue_stems.add(os.path.normcase(str(f.with_suffix(""))))
    return [f for f in images
            if os.path.normcase(os.path.normpath(str(f))) not in tracks
            and not (f.suffix.lower() == ".bin"
                     and os.path.normcase(str(f.with_suffix(""))) in cue_stems)]


def disc_data_file(image: str) -> str:
    """The file holding a disc's data: a .cue sheet's first track, otherwise
    the image itself."""
    if image.lower().endswith(".cue"):
        try:
            files = cue_files(Path(image))
        except OSError:
            files = []
        if files:
            return files[0]
    return image


def mountable_disc(image: str) -> str:
    """The image to hand DOSBox's imgmount. DOSBox can't read a .cue sheet
    saved with a UTF-8 byte order mark, so for one of those its data track is
    mounted instead (without the CD audio tracks)."""
    if image.lower().endswith(".cue"):
        try:
            with open(image, "rb") as f:
                if f.read(3) == b"\xef\xbb\xbf":
                    return disc_data_file(image)
        except OSError:
            pass
    return image


def find_game_discs(game_dir: Path) -> list:
    """Disc images to mount as D: for a game added from an archive.

    Looks in the game's CD folder (e.g. ExoDOS's Syndicat\\CD) for .cue, .iso,
    .bin and .img images, plus any .cue or .iso at the top of the game folder.
    Loose .bin/.img files elsewhere are skipped, since DOS games often ship
    data files with those extensions. Files a .cue points to (its .bin/.iso
    and audio tracks) are left for the .cue to load.
    """
    if not game_dir.is_dir():
        return []
    images = [f for f in game_dir.iterdir()
              if f.is_file() and f.suffix.lower() in (".cue", ".iso")]
    for child in game_dir.iterdir():
        if child.is_dir() and child.name.lower() in GAME_CD_FOLDERS:
            images += [f for f in child.iterdir()
                       if f.is_file() and f.suffix.lower() in DISC_EXTS]
    return [str(f) for f in drop_cue_tracks(sorted(images, key=_disc_order))]


# ── Helpers ──────────────────────────────────────────────────────────────────

def snap_memsize(mb: int) -> int:
    """Snap a memsize value to the nearest valid DOSBox value (power of 2, max 384)."""
    valid = [1, 2, 4, 8, 16, 32, 64, 128, 256, 384]
    return min(valid, key=lambda x: abs(x - mb))


def find_dosbox() -> tuple | None:
    """Return (binary, prefix_args) for the best DOSBox found, or None."""
    if DOSBOX_WIN_PATH.exists():
        return (str(DOSBOX_WIN_PATH), [])
    for b in DOSBOX_BINS + ["dosbox-staging.exe", "dosbox.exe"]:
        if shutil.which(b):
            return (b, [])
    return None


def load_exodos() -> dict:
    """Load the ExoDOS game database if present."""
    if EXODOS_FILE.exists():
        try:
            return json.loads(EXODOS_FILE.read_text(encoding="utf-8")).get("games", {})
        except Exception:
            pass
    return {}


def exodos_key(name: str) -> str:
    """Normalize a game name to an ExoDOS lookup key.
    Strips common noise words found in zip filenames (dos, win, gog, years etc.)
    """
    import re
    noise = r"\b(dos|win|windows|gog|cd|cdrom|version|v\d+|19\d\d|20\d\d)\b"
    cleaned = re.sub(noise, "", name.lower())
    # Strip all non-alphanumeric — matches the key format in master_games.json
    return re.sub(r"[^a-z0-9]", "", cleaned)


def _simple_key(name: str) -> str:
    """Simpler key — just strip everything non-alphanumeric, no noise removal."""
    import re
    return re.sub(r"[^a-z0-9]", "", name.lower())


def lookup_exodos(name: str) -> dict | None:
    """Return the ExoDOS entry for a game name.

    Strategy:
    1. Exact key match
    2. Simple key match (no noise stripping)
    3. Containment match — key inside query or query inside key (min 5 chars)
    4. Token match — every word in the query appears in the title
    Returns None if no confident match found.
    """
    db = load_exodos()
    if not db:
        return None

    key  = exodos_key(name)
    key2 = _simple_key(name)
    if not key and not key2:
        return None

    # 1. Exact match on either key form
    if key in db:
        return db[key]
    if key2 in db:
        return db[key2]

    # 2. Containment match — try both key forms
    MIN_OVERLAP = 5
    candidates = []
    for k, v in db.items():
        title_key = _simple_key(v.get("title", ""))
        for q in (key, key2):
            if not q or len(q) < MIN_OVERLAP:
                continue
            if q in k or k in q or q in title_key:
                overlap = min(len(q), len(k))
                score = overlap - abs(len(q) - len(k))
                candidates.append((score, k, v))
                break

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_key, best_entry = candidates[0]
        longer = max(len(key2 or key), len(best_key))
        overlap = min(len(key2 or key), len(best_key))
        if longer > 0 and overlap / longer >= 0.70:
            return best_entry

    # 3. Token match — split name into words, check all appear in title
    tokens = [t for t in re.sub(r"[^a-z0-9]", " ", name.lower()).split() if len(t) >= 3]
    if len(tokens) >= 2:
        for k, v in db.items():
            title_low = v.get("title", "").lower()
            if all(t in title_low for t in tokens):
                return v

    return None


def search_exodos(query: str) -> list:
    """Return list of (key, entry) tuples matching a search query."""
    db = load_exodos()
    if not db:
        return []
    q = exodos_key(query)
    q2 = _simple_key(query)
    results = []
    seen = set()
    for k, v in db.items():
        title_key = _simple_key(v.get("title", ""))
        if (q and (q in k or q in title_key)) or (q2 and (q2 in k or q2 in title_key)):
            if k not in seen:
                seen.add(k)
                results.append((k, v))
    return results


# Folder names that indicate a bundled emulator — skip exes inside these
DOSBOX_WRAPPER_NAMES = {"dosbox", "dosbox-staging", "scummvm", "boxer"}

# Known good launcher bat names inside game subfolders
GOOD_BAT_NAMES = {"command.bat", "play.bat", "start.bat", "game.bat", "run.bat", "launch.bat"}

# DOS utilities often shipped beside a game (e.g. ASKECHO.COM in ExoDOS menus)
DOS_UTILITY_COMS = {"command", "mouse", "keyb", "ansi", "doskey", "choice",
                    "askecho", "mode", "more", "debug", "edit", "graphics",
                    "format", "sys", "loadfix", "setver", "share", "lh"}

def score_exe(exe: Path, root: Path, stem: str, depth_offset: int = 0) -> int:
    """Score an EXE/COM/BAT candidate; higher = more likely the main game launcher."""
    name     = exe.name.lower()
    namestem = exe.stem.lower()
    ext      = exe.suffix.lower()
    score    = 0

    # Depth from the archive root (0 = at root, 1 = one subfolder deep, etc.)
    # depth_offset = 1 when root is the archive's single top-level folder
    depth = len(exe.relative_to(root).parts) - 1 + depth_offset

    # ── Penalise root-level .bat named after the game folder ──────────────────
    # These are almost always Windows OS launchers, not DOS game files
    if ext == ".bat" and depth == 0:
        score -= 8
        # Extra penalty if it matches the stem (e.g. CommandAndConquer.bat)
        if namestem == stem.lower() or namestem in stem.lower().replace("-","").replace("_",""):
            score -= 4

    # ── Known good launcher bat names inside subfolders ───────────────────────
    if ext == ".bat" and depth > 0 and name in GOOD_BAT_NAMES:
        score += 8

    # ── Prefer files one level deep (inside main game subfolder) ──────────────
    if depth == 1:
        score += 5
    elif depth == 0:
        score += 2
    elif depth == 2:
        score += 1

    # ── Exact exe name match to game stem ─────────────────────────────────────
    if namestem == stem.lower() and ext in (".exe", ".com"):
        score += 10

    # ── .COM files: picked over an .EXE beside them only when named after the
    #    game (many early games only have a .COM, e.g. SIERRA.COM, DIGGER.COM)
    if ext == ".com":
        score -= 4
        if namestem in DOS_UTILITY_COMS:
            score -= 6

    # ── Penalise emulator folders ──────────────────────────────────────────────
    parent_name = exe.parent.name.lower()
    if any(d in parent_name for d in DOSBOX_WRAPPER_NAMES):
        score -= 15

    # ── Penalise known setup/install/utility names ─────────────────────────────
    if name in LAUNCHER_NAMES:
        score -= 3
    for bad in SETUP_NAMES:
        if bad in name:
            score -= 5

    # ── Penalise utility folders ───────────────────────────────────────────────
    for util in ("ultrasnd", "ultrasnds", "midi", "sound", "audio", "install"):
        if util in str(exe.parent).lower():
            score -= 6

    return score


def detect_exe(game_dir: Path, stem: str, depth_offset: int = 0) -> tuple:
    """Return (all_launchers, best_guess) from extracted game directory.
    Considers .exe, .com and .bat files; best score wins automatically.
    """
    found = set()
    for pattern in ("*.exe", "*.EXE", "*.com", "*.COM", "*.bat", "*.BAT"):
        found.update(game_dir.rglob(pattern))
    all_files = sorted(found)
    if not all_files:
        return [], None
    if len(all_files) == 1:
        return all_files, all_files[0]
    score  = lambda e: score_exe(e, game_dir, stem, depth_offset)
    scored = sorted(all_files, key=score, reverse=True)
    top, second = scored[0], scored[1]
    ambiguous = abs(score(top) - score(second)) <= 3
    return scored, None if ambiguous else top


def extract_archive(archive: Path, dest: Path) -> None:
    """Extract a zip/7z/rar/tar archive to dest."""
    suffix   = archive.suffix.lower()
    suffixes = [s.lower() for s in archive.suffixes]
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif suffix == ".7z":
        sevenzip = (str(TOOLS_7ZA) if TOOLS_7ZA.exists()
                    else shutil.which("7z") or shutil.which("7za") or shutil.which("7zr"))
        if not sevenzip:
            raise RuntimeError("7za.exe not found. Place it in the tools\\ folder.")
        result = subprocess.run(
            [sevenzip, "x", str(archive), "-o" + str(dest), "-y"],
            capture_output=True, text=True, errors="replace",
            creationflags=_NO_WINDOW
        )
        if result.returncode != 0:
            raise RuntimeError("7z extraction failed: " + (result.stderr or result.stdout))
    elif suffix == ".rar":
        # Use bundled 7za.exe — it handles RAR natively, no UnRAR needed
        sevenzip = (str(TOOLS_7ZA) if TOOLS_7ZA.exists()
                    else shutil.which("7z") or shutil.which("7za"))
        if not sevenzip:
            raise RuntimeError("7za.exe not found. Place it in the tools\\ folder.")
        result = subprocess.run(
            [sevenzip, "x", str(archive), "-o" + str(dest), "-y"],
            capture_output=True, text=True, errors="replace",
            creationflags=_NO_WINDOW
        )
        if result.returncode != 0:
            raise RuntimeError("RAR extraction failed: " + (result.stderr or result.stdout))
    elif suffix in (".gz", ".tgz") or ".tar" in suffixes:
        with tarfile.open(archive) as tf:
            tf.extractall(dest)
    else:
        raise RuntimeError(f"Unsupported archive format: {archive.suffix}")


def archive_base_name(archive: Path) -> str:
    """Archive file name without its extension, e.g. 'Dr. Doom (1990)' for
    'Dr. Doom (1990).7z' (only the archive extension goes, not every dot)."""
    name = archive.name
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)].rstrip(" .") or name
    return archive.stem.rstrip(" .") or name


def remove_tree(path: Path) -> None:
    """Delete a folder tree, clearing read-only flags (common on files copied
    off CDs) that would otherwise stop the delete part-way. Never raises."""
    def retry_writable(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    if _sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry_writable)
    else:
        shutil.rmtree(path, onerror=retry_writable)


def move_into(src: Path, dest: Path) -> None:
    """Move an extracted game folder to dest. If dest already exists (the same
    game imported again), copy over it instead so files the game has written
    there, like save games, are kept."""
    if not dest.exists():
        for _ in range(20):
            try:
                os.rename(src, dest)
                return
            except PermissionError:
                time.sleep(0.25)  # e.g. a virus scanner still checking the new files
            except OSError:
                break
        shutil.copytree(src, dest)  # couldn't rename it; the staging copy is removed after
        return

    def copy_over(s, d):
        if os.path.exists(d):
            os.chmod(d, stat.S_IWRITE)  # read-only files copied off a CD
        return shutil.copy2(s, d)

    shutil.copytree(src, dest, dirs_exist_ok=True, copy_function=copy_over)


def dosbox_cycles_args(cycles) -> list:
    """DOSBox -set arguments for a game's cycles value, as ExoDOS gives it.

    'auto' (3000 cycles for real mode programs, max for protected mode), 'max',
    'fixed N' and plain N become DOSBox Staging's cpu_cycles and
    cpu_cycles_protected settings. Values with a limit ('max limit 35000',
    'auto limit 20000') go to DOSBox's classic 'cycles' setting unchanged,
    which still applies them exactly. A missing or unknown value counts as
    'auto'.
    """
    value  = str(cycles or "").strip()
    s      = value.lower()
    number = re.fullmatch(r"(?:fixed\s+)?(\d+)", s)
    if number:
        real = protected = str(min(max(int(number.group(1)), 50), 2000000))
    elif s == "max":
        real = protected = "max"
    elif re.fullmatch(r"(auto|max)\s.*", s):
        return ["-set", f"cycles={value}"]
    else:
        real, protected = "3000", "max"
    return ["-set", f"cpu_cycles={real}", "-set", f"cpu_cycles_protected={protected}"]


# ══════════════════════════════════════════════════════════════════════════════
# GAMEPAD — Microsoft SideWinder Game Pad (USB)
# ══════════════════════════════════════════════════════════════════════════════
# DOSBox reads the pad itself; AutoDOS writes a DOSBox mapper file per game that
# says what each pad control does there. A mapper file replaces all of DOSBox's
# bindings, so it always carries DOSBox's own defaults in full.

# DOSBox Staging 0.82.2's default bindings (saved from its mapper): hotkeys,
# every keyboard key and the modifier keys
DOSBOX_DEFAULT_BINDS = """\
hand_recwave "key 63 mod1"
hand_caprawmidi "key 63 mod1 mod2"
hand_scrshot "key 62 mod1"
hand_rendshot "key 62 mod2"
hand_video "key 64 mod1"
hand_mute "key 65 mod1"
hand_reloadshad "key 59 mod1"
hand_shutdown "key 66 mod1"
hand_fullscr "key 40 mod2"
hand_restart "key 74 mod1 mod2"
hand_capmouse "key 67 mod1"
hand_pause "key 72 mod2"
hand_mapper "key 58 mod1"
hand_speedlock "key 69 mod2"
hand_cycledown "key 68 mod1"
hand_cycleup "key 69 mod1"
hand_swapimg "key 61 mod1"
key_esc "key 41"
key_f1 "key 58"
key_f2 "key 59"
key_f3 "key 60"
key_f4 "key 61"
key_f5 "key 62"
key_f6 "key 63"
key_f7 "key 64"
key_f8 "key 65"
key_f9 "key 66"
key_f10 "key 67"
key_f11 "key 68"
key_f12 "key 69"
key_grave "key 53"
key_1 "key 30"
key_2 "key 31"
key_3 "key 32"
key_4 "key 33"
key_5 "key 34"
key_6 "key 35"
key_7 "key 36"
key_8 "key 37"
key_9 "key 38"
key_0 "key 39"
key_minus "key 45"
key_equals "key 46"
key_bspace "key 42"
key_tab "key 43"
key_q "key 20"
key_w "key 26"
key_e "key 8"
key_r "key 21"
key_t "key 23"
key_y "key 28"
key_u "key 24"
key_i "key 12"
key_o "key 18"
key_p "key 19"
key_lbracket "key 47"
key_rbracket "key 48"
key_enter "key 40"
key_capslock "key 57"
key_a "key 4"
key_s "key 22"
key_d "key 7"
key_f "key 9"
key_g "key 10"
key_h "key 11"
key_j "key 13"
key_k "key 14"
key_l "key 15"
key_semicolon "key 51"
key_quote "key 52"
key_backslash "key 49"
key_lshift "key 225"
key_oem102 "key 100"
key_z "key 29"
key_x "key 27"
key_c "key 6"
key_v "key 25"
key_b "key 5"
key_n "key 17"
key_m "key 16"
key_comma "key 54"
key_period "key 55"
key_slash "key 56"
key_abnt1 "key 135"
key_rshift "key 229"
key_lctrl "key 224"
key_lgui "key 227"
key_lalt "key 226"
key_space "key 44"
key_ralt "key 230"
key_rgui "key 231"
key_rctrl "key 228"
key_printscreen "key 70"
key_scrolllock "key 71"
key_pause "key 72"
key_insert "key 73"
key_home "key 74"
key_pageup "key 75"
key_delete "key 76"
key_end "key 77"
key_pagedown "key 78"
key_up "key 82"
key_left "key 80"
key_down "key 81"
key_right "key 79"
key_numlock "key 83"
key_kp_divide "key 84"
key_kp_multiply "key 85"
key_kp_minus "key 86"
key_kp_7 "key 95"
key_kp_8 "key 96"
key_kp_9 "key 97"
key_kp_plus "key 87"
key_kp_4 "key 92"
key_kp_5 "key 93"
key_kp_6 "key 94"
key_kp_1 "key 89"
key_kp_2 "key 90"
key_kp_3 "key 91"
key_kp_enter "key 88"
key_kp_0 "key 98"
key_kp_period "key 99"
mod_1 "key 224" "key 228"
mod_2 "key 226" "key 230"
mod_3 "key 227" "key 231"
"""

# The pad's buttons and the button numbers DOSBox sees for them (SDL's
# controller database, USB id 045e:0007). Start is button 8 on some models
# and 9 on others, so it's bound to both.
PAD_BUTTONS = [
    ("a", "A", (0,)), ("b", "B", (1,)), ("c", "C", (2,)),
    ("x", "X", (3,)), ("y", "Y", (4,)), ("z", "Z", (5,)),
    ("l", "L trigger", (6,)), ("r", "R trigger", (7,)),
    ("start", "Start", (8, 9)),
]
# Buttons that can be the joystick's fire buttons 1-4 in DOS games
PAD_JOYSTICK_BUTTONS = {"a": 1, "b": 2, "c": 3, "x": 4}

# The D-pad shows up as the stick's axes or as a hat, depending on the model
# and driver, so each direction is bound both ways (hat: 1 up, 2 right,
# 4 down, 8 left)
PAD_DPAD = {
    "up":    ("stick_0 axis 1 0", "stick_0 hat 0 1"),
    "down":  ("stick_0 axis 1 1", "stick_0 hat 0 4"),
    "left":  ("stick_0 axis 0 0", "stick_0 hat 0 8"),
    "right": ("stick_0 axis 0 1", "stick_0 hat 0 2"),
}
PAD_DPAD_CHOICES = [
    ("joystick", "Joystick"),
    ("arrows",   "Arrow keys"),
    ("wasd",     "W A S D"),
    ("numpad",   "Numpad 8 4 6 2"),
    ("none",     "Not used"),
]
PAD_DPAD_KEYS = {
    "arrows": {"up": "key_up", "down": "key_down", "left": "key_left", "right": "key_right"},
    "wasd":   {"up": "key_w",  "down": "key_s",    "left": "key_a",    "right": "key_d"},
    "numpad": {"up": "key_kp_8", "down": "key_kp_2", "left": "key_kp_4", "right": "key_kp_6"},
}

# Keys a button can press, as (name shown, DOSBox event)
PAD_KEYS = (
    [("Esc", "key_esc"), ("Enter", "key_enter"), ("Space", "key_space"),
     ("Ctrl", "key_lctrl"), ("Alt", "key_lalt"), ("Shift", "key_lshift"),
     ("Tab", "key_tab"), ("Backspace", "key_bspace"),
     ("Up", "key_up"), ("Down", "key_down"), ("Left", "key_left"), ("Right", "key_right"),
     ("Page Up", "key_pageup"), ("Page Down", "key_pagedown"),
     ("Home", "key_home"), ("End", "key_end"),
     ("Insert", "key_insert"), ("Delete", "key_delete")]
    + [(c, "key_" + c.lower()) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    + [(d, "key_" + d) for d in "1234567890"]
    + [(f"F{n}", f"key_f{n}") for n in range(1, 13)]
    + [(f"Numpad {d}", f"key_kp_{d}") for d in "0123456789"]
    + [("Numpad Enter", "key_kp_enter"), ("Numpad +", "key_kp_plus"),
       ("Numpad -", "key_kp_minus"), ("Numpad *", "key_kp_multiply"),
       ("Numpad /", "key_kp_divide"), ("Numpad .", "key_kp_period"),
       (", (comma)", "key_comma"), (". (period)", "key_period"),
       ("/ (slash)", "key_slash"), ("; (semicolon)", "key_semicolon"),
       ("' (quote)", "key_quote"), ("[", "key_lbracket"), ("]", "key_rbracket"),
       ("- (minus)", "key_minus"), ("= (equals)", "key_equals"),
       ("` (backquote)", "key_grave"), ("\\ (backslash)", "key_backslash"),
       ("Right Ctrl", "key_rctrl"), ("Right Alt", "key_ralt"),
       ("Right Shift", "key_rshift"), ("Caps Lock", "key_capslock"),
       ("Pause", "key_pause"),
       ("Left mouse button", "mouse_left"), ("Right mouse button", "mouse_right")]
)

PAD_PRESETS = {
    # Games with joystick support: D-pad and A/B/C/X are a PC joystick
    "Joystick": {"dpad": "joystick", "a": "joystick", "b": "joystick",
                 "c": "joystick", "x": "joystick", "y": "none", "z": "none",
                 "l": "none", "r": "none", "start": "key_esc"},
    # Keyboard-only games
    "Keyboard": {"dpad": "arrows", "a": "key_lctrl", "b": "key_lalt",
                 "c": "key_space", "x": "key_lshift", "y": "key_enter",
                 "z": "key_tab", "l": "key_comma", "r": "key_period",
                 "start": "key_esc"},
}


def gamepad_settings(entry: dict) -> dict:
    """A game's pad settings: what it saved, else the Joystick preset."""
    settings = dict(PAD_PRESETS["Joystick"])
    saved = entry.get("gamepad")
    if isinstance(saved, dict):
        settings.update({k: v for k, v in saved.items() if k in settings})
    return settings


def gamepad_mapper(settings: dict) -> tuple:
    """A complete DOSBox mapper file for a game's pad settings, and whether
    the game should see the pad as a joystick: (file text, uses_joystick)."""
    binds, order = {}, []
    for line in DOSBOX_DEFAULT_BINDS.splitlines():
        event = line.split(None, 1)[0]
        binds[event] = re.findall(r'"([^"]*)"', line)
        order.append(event)

    # DOSBox's default joystick bindings (it only adds these itself when no
    # mapper file is loaded), plus the D-pad's hat moving the stick
    joy = {}
    for n in range(6):
        joy[f"jbutton_0_{n}"] = [f"stick_0 button {n}"]
    for n in range(2):
        joy[f"jbutton_1_{n}"] = [f"stick_1 button {n}"]
    for axis in range(4):
        joy[f"jaxis_0_{axis}-"] = [f"stick_0 axis {axis} 0"]
        joy[f"jaxis_0_{axis}+"] = [f"stick_0 axis {axis} 1"]
    for axis in range(2):
        joy[f"jaxis_1_{axis}-"] = [f"stick_1 axis {axis} 0"]
        joy[f"jaxis_1_{axis}+"] = [f"stick_1 axis {axis} 1"]
    for n, hat in enumerate((1, 2, 4, 8)):
        joy[f"jhat_0_0_{n}"] = [f"stick_0 hat 0 {hat}"]
    joy["jaxis_0_1-"].append("stick_0 hat 0 1")
    joy["jaxis_0_1+"].append("stick_0 hat 0 4")
    joy["jaxis_0_0-"].append("stick_0 hat 0 8")
    joy["jaxis_0_0+"].append("stick_0 hat 0 2")

    def take_from_joystick(pad_binds):
        for event in joy:
            joy[event] = [b for b in joy[event] if b not in pad_binds]

    def add(event, pad_binds):
        if event not in binds:
            binds[event] = []
            order.append(event)
        binds[event].extend(pad_binds)

    dpad = settings.get("dpad", "joystick")
    uses_joystick = dpad == "joystick"
    if not uses_joystick:
        take_from_joystick({b for pair in PAD_DPAD.values() for b in pair})
        for direction, event in PAD_DPAD_KEYS.get(dpad, {}).items():
            add(event, list(PAD_DPAD[direction]))

    for control, _label, numbers in PAD_BUTTONS:
        choice = settings.get(control, "none")
        if choice == "joystick" and control in PAD_JOYSTICK_BUTTONS:
            uses_joystick = True
            continue
        pad_binds = [f"stick_0 button {n}" for n in numbers]
        take_from_joystick(set(pad_binds))
        if choice != "none" and choice != "joystick":
            add(choice, pad_binds)

    lines = [event + "".join(f' "{b}"' for b in binds[event]) for event in order]
    lines += [event + "".join(f' "{b}"' for b in joy[event]) for event in joy]
    return "\n".join(lines) + "\n", uses_joystick

def write_gamepad_mapper(entry: dict) -> tuple:
    """Write a game's DOSBox mapper file: (path, uses_joystick)."""
    text, uses_joystick = gamepad_mapper(gamepad_settings(entry))
    CONTROLLER_MAPS_DIR.mkdir(exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\s]+', "_", str(entry.get("id") or "game")).strip("._") or "game"
    path = CONTROLLER_MAPS_DIR / (safe + ".map")
    path.write_text(text, encoding="ascii")
    return path, uses_joystick


# ── Reading the pad live (for the Gamepad window's button test) ───────────────
# Uses the Windows joystick API, which sees DirectInput pads like the
# SideWinder (XInput only sees Xbox-style pads). Buttons count from 0 here,
# in the same order DOSBox numbers them.

class _JOYINFOEX(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "dwSize", "dwFlags", "dwXpos", "dwYpos", "dwZpos", "dwRpos", "dwUpos",
        "dwVpos", "dwButtons", "dwButtonNumber", "dwPOV", "dwReserved1", "dwReserved2")]


class _JOYCAPSW(ctypes.Structure):
    _fields_ = ([("wMid", ctypes.c_uint16), ("wPid", ctypes.c_uint16),
                 ("szPname", ctypes.c_wchar * 32)]
                + [(name, ctypes.c_uint32) for name in (
                    "wXmin", "wXmax", "wYmin", "wYmax", "wZmin", "wZmax",
                    "wNumButtons", "wPeriodMin", "wPeriodMax", "wRmin", "wRmax",
                    "wUmin", "wUmax", "wVmin", "wVmax", "wCaps", "wMaxAxes",
                    "wNumAxes", "wMaxButtons")]
                + [("szRegKey", ctypes.c_wchar * 32), ("szOEMVxD", ctypes.c_wchar * 260)])


def _winmm():
    try:
        return ctypes.windll.winmm
    except (AttributeError, OSError):
        return None


def find_gamepad():
    """The first connected game controller as (id, name, x range, y range),
    or None."""
    winmm = _winmm()
    if not winmm:
        return None
    info = _JOYINFOEX(dwSize=ctypes.sizeof(_JOYINFOEX), dwFlags=0xFF)
    for joy_id in range(16):
        if winmm.joyGetPosEx(joy_id, ctypes.byref(info)) != 0:
            continue
        caps = _JOYCAPSW()
        if winmm.joyGetDevCapsW(joy_id, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
            caps.wXmax = caps.wYmax = 65535
        if caps.wMid == 0x045E and caps.wPid in (0x0007, 0x0027):
            name = "SideWinder Game Pad"
        else:
            name = "Gamepad"
        return (joy_id, name, (caps.wXmin, caps.wXmax or 65535),
                (caps.wYmin, caps.wYmax or 65535))
    return None


def read_gamepad(pad) -> tuple | None:
    """(pressed button numbers, pressed D-pad directions) for a pad from
    find_gamepad(), or None if it's been unplugged."""
    winmm = _winmm()
    joy_id, _name, (xmin, xmax), (ymin, ymax) = pad
    info = _JOYINFOEX(dwSize=ctypes.sizeof(_JOYINFOEX), dwFlags=0xFF)   # JOY_RETURNALL
    if not winmm or winmm.joyGetPosEx(joy_id, ctypes.byref(info)) != 0:
        return None
    buttons = {n for n in range(32) if info.dwButtons >> n & 1}
    dirs = set()
    for pos, low, high, neg, plus in ((info.dwXpos, xmin, xmax, "left", "right"),
                                      (info.dwYpos, ymin, ymax, "up", "down")):
        span = max(high - low, 1)
        if pos < low + span // 4:
            dirs.add(neg)
        elif pos > high - span // 4:
            dirs.add(plus)
    if info.dwPOV != 0xFFFF:                     # hat, in hundredths of a degree
        angle = info.dwPOV / 100
        if angle >= 292.5 or angle <= 67.5:
            dirs.add("up")
        if 22.5 <= angle <= 157.5:
            dirs.add("right")
        if 112.5 <= angle <= 247.5:
            dirs.add("down")
        if 202.5 <= angle <= 337.5:
            dirs.add("left")
    return buttons, dirs


def dark_title_bar(window) -> None:
    """Ask Windows 10/11 to draw a window's title bar dark."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        on = ctypes.c_int(1)
        for attribute in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (newer, older Windows 10)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except Exception:
        pass

# ── App ──────────────────────────────────────────────────────────────────────

class App:
    """Main AutoDOS application."""

    def __init__(self, root: tk.Tk):
        self.root    = root
        self.library: list = []
        self.dosbox  = find_dosbox()

        self._setup_window()
        self._build_ui()
        self._load_library()

    # ── Window setup ──────────────────────────────────────────────────────

    def _setup_window(self):
        """Configure the root window."""
        self.root.title("AutoDOS")
        self.root.configure(bg=BG)
        self.root.geometry("520x700")
        self.root.minsize(480, 600)

        if ICON_FILE.exists():
            try:
                self.root.iconbitmap(str(ICON_FILE))
            except Exception:
                pass

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=LIST_BG,
                        bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                        troughcolor=LIST_BG, selectbackground=SEL_BG,
                        selectforeground=TEXT, insertcolor=TEXT, focuscolor=BORDER)
        style.configure("TScrollbar", background=BTN_BG, troughcolor=LIST_BG,
                        bordercolor=LIST_BG, lightcolor=BTN_BG, darkcolor=BTN_BG,
                        arrowcolor=MUTED, borderwidth=0)
        style.map("TScrollbar", background=[("pressed", BTN_PRESSED), ("active", BTN_ACTIVE)])
        style.configure("Pill.TButton",
                        background=BTN_BG, foreground=TEXT,
                        relief="flat", borderwidth=0,
                        bordercolor=BTN_BG, lightcolor=BTN_BG, darkcolor=BTN_BG,
                        padding=(16, 8), font=("TkDefaultFont", 11))
        style.map("Pill.TButton",
                  background=[("disabled", BTN_BG), ("pressed", BTN_PRESSED), ("active", BTN_ACTIVE)],
                  foreground=[("disabled", DISABLED)],
                  bordercolor=[("pressed", BTN_PRESSED), ("active", BTN_ACTIVE)],
                  lightcolor=[("pressed", BTN_PRESSED), ("active", BTN_ACTIVE)],
                  darkcolor=[("pressed", BTN_PRESSED), ("active", BTN_ACTIVE)],
                  relief=[("active", "flat")])
        style.configure("TCombobox", fieldbackground=LIST_BG, background=BTN_BG,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER,
                        lightcolor=LIST_BG, darkcolor=LIST_BG, padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", LIST_BG)],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", LIST_BG)],
                  selectforeground=[("readonly", TEXT)],
                  background=[("pressed", BTN_PRESSED), ("active", BTN_ACTIVE)],
                  bordercolor=[("focus", SEL_BG)])
        # the list that drops down from a combobox
        self.root.option_add("*TCombobox*Listbox.background", LIST_BG)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SEL_BG)
        self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.root.option_add("*TCombobox*Listbox.font", ("TkDefaultFont", 10))
        style.configure("TCheckbutton", background=BG, foreground=TEXT,
                        indicatorbackground=LIST_BG, indicatorforeground=TEXT,
                        indicatormargin=(0, 0, 4, 0), focuscolor=BG)
        style.map("TCheckbutton",
                  background=[("active", BG)],
                  indicatorbackground=[("pressed", BTN_PRESSED), ("active", BTN_ACTIVE)])
        dark_title_bar(self.root)

    # ── UI Build ──────────────────────────────────────────────────────────

    def _build_ui(self):
        """Construct all UI widgets."""
        self._build_logo()
        self._build_list()
        self._build_buttons()
        tk.Label(self.root, text="Double-click to launch  ·  Right-click for settings, gamepad or remove",
                 bg=BG, fg=MUTED, font=("TkDefaultFont", 9, "bold")
                 ).pack(side=tk.BOTTOM, pady=(0, 6))

    def _build_logo(self):
        """Load and display the logo image."""
        self.logo_img = None
        if LOGO_FILE.exists():
            try:
                from PIL import Image, ImageTk
                img = Image.open(LOGO_FILE).convert("RGBA")
                img.thumbnail((200, 160), Image.LANCZOS)
                self.logo_img = ImageTk.PhotoImage(img)
            except Exception:
                try:
                    self.logo_img = tk.PhotoImage(file=str(LOGO_FILE))
                except Exception:
                    pass

        if self.logo_img:
            lbl = tk.Label(self.root, image=self.logo_img, bg=BG, bd=0)
            lbl.image = self.logo_img  # keep reference — prevents GC on Windows
            lbl.pack(pady=(18, 10))
        else:
            tk.Label(self.root, text="AutoDOS", bg=BG, fg=TEXT,
                     font=("TkDefaultFont", 28, "bold")).pack(pady=(18, 10))

    def _build_list(self):
        """Build the search bar + scrollable game library panel."""
        outer = tk.Frame(self.root, bg=BG, padx=12)
        outer.pack(fill=tk.BOTH, expand=True, padx=0, pady=(0, 8))

        # ── Search bar ────────────────────────────────────────────────────
        search_frame = tk.Frame(outer, bg=BG)
        search_frame.pack(fill=tk.X, pady=(0, 6))

        self.search_var = tk.StringVar()

        search_box = tk.Entry(
            search_frame, textvariable=self.search_var,
            bg=LIST_BG, fg=TEXT, relief="flat", bd=0,
            font=("TkDefaultFont", 12), insertbackground=TEXT,
            selectbackground=SEL_BG, selectforeground=TEXT,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=SEL_BG,
        )
        search_box.pack(fill=tk.X, ipady=5)
        search_box.insert(0, "Search games...")
        search_box.config(fg=MUTED)

        def on_focus_in(e):
            if search_box.get() == "Search games...":
                search_box.delete(0, tk.END)
                search_box.config(fg=TEXT)

        def on_focus_out(e):
            if not search_box.get():
                search_box.insert(0, "Search games...")
                search_box.config(fg=MUTED)

        search_box.bind("<FocusIn>",  on_focus_in)
        search_box.bind("<FocusOut>", on_focus_out)
        search_box.bind("<Escape>",   lambda e: (self.search_var.set(""), search_box.delete(0, tk.END), search_box.insert(0, "Search games..."), search_box.config(fg=MUTED)))

        # ── Game list ─────────────────────────────────────────────────────
        frame = tk.Frame(outer, bg=BG)
        frame.pack(fill=tk.BOTH, expand=True)

        container = tk.Frame(frame, bg=BORDER, bd=1, relief="flat")
        container.pack(fill=tk.BOTH, expand=True)

        inner = tk.Frame(container, bg=LIST_BG)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self.listbox = tk.Listbox(
            inner,
            bg=LIST_BG, fg=TEXT,
            selectbackground=SEL_BG, selectforeground=TEXT,
            activestyle="none", relief="flat", bd=0,
            font=("TkDefaultFont", 13),
            highlightthickness=0,
            selectmode=tk.SINGLE,
        )
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.listbox.bind("<Double-Button-1>", lambda e: self._launch_selected())
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<ButtonRelease-3>", self._show_context_menu)

        sb = ttk.Scrollbar(inner, orient=tk.VERTICAL, command=self.listbox.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.configure(yscrollcommand=sb.set)

        # Filtered indices — maps listbox position to library index
        self._filtered_indices: list = []

        # Attach search trace now that listbox exists
        self.search_var.trace_add("write", self._on_search)

    def _build_buttons(self):
        """Build the bottom button bar."""
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill=tk.X, padx=14, pady=(0, 4))

        self.btn_launch = ttk.Button(bar, text="▶  Launch",
                                     style="Pill.TButton",
                                     command=self._launch_selected)
        self.btn_launch.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_launch.state(["disabled"])

        ttk.Button(bar, text="＋  Add Zip",
                   style="Pill.TButton",
                   command=self._add_archive).pack(side=tk.LEFT, padx=(0, 6))

        ttk.Button(bar, text="🗑  Remove",
                   style="Pill.TButton",
                   command=self._remove_selected).pack(side=tk.RIGHT)

    # ── Library ───────────────────────────────────────────────────────────

    def _load_library(self):
        """Load library.json and populate the listbox."""
        if LIBRARY_FILE.exists():
            try:
                self.library = json.loads(LIBRARY_FILE.read_text())
            except Exception:
                self.library = []
        self._refresh_list()

    def _save_library(self):
        """Persist library to library.json."""
        LIBRARY_FILE.write_text(json.dumps(self.library, indent=2))

    def _refresh_list(self, query: str = ""):
        """Repopulate the listbox, optionally filtered by search query."""
        self.listbox.delete(0, tk.END)
        self._filtered_indices = []
        q = query.strip().lower()
        for i, entry in enumerate(self.library):
            if q and q not in entry["name"].lower():
                continue
            self._filtered_indices.append(i)
            j = len(self._filtered_indices) - 1
            self.listbox.insert(tk.END, f"  {entry['name']}")
            bg = LIST_BG if j % 2 == 0 else ALT_ROW_BG
            self.listbox.itemconfig(j, bg=bg)
        self.btn_launch.state(["disabled"])

    def _on_search(self, *_):
        """Live filter the game list as user types."""
        q = self.search_var.get()
        if q == "Search games...":
            q = ""
        self._refresh_list(q)

    def _get_selected_entry(self):
        """Return the library entry for the current listbox selection, or None."""
        sel = self.listbox.curselection()
        if not sel:
            return None
        lib_idx = self._filtered_indices[sel[0]] if self._filtered_indices else sel[0]
        return self.library[lib_idx]

    def _get_selected_lib_index(self):
        """Return the library index for the current listbox selection, or None."""
        sel = self.listbox.curselection()
        if not sel:
            return None
        return self._filtered_indices[sel[0]] if self._filtered_indices else sel[0]

    def _on_select(self, _event=None):
        """Enable/disable Launch button based on selection."""
        if self.listbox.curselection():
            self.btn_launch.state(["!disabled"])
        else:
            self.btn_launch.state(["disabled"])

    # ── Context Menu ──────────────────────────────────────────────────────

    def _show_context_menu(self, event):
        """Show right-click context menu only when clicking on an actual game row."""
        lb_idx = self.listbox.nearest(event.y)

        # Bail out if no games or empty space
        if not self._filtered_indices or lb_idx < 0 or lb_idx >= len(self._filtered_indices):
            return

        bbox = self.listbox.bbox(lb_idx)
        if not bbox:
            return
        _, row_y, _, row_h = bbox
        if event.y < row_y or event.y > row_y + row_h:
            return

        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(lb_idx)
        self._on_select()

        lib_idx = self._filtered_indices[lb_idx]
        entry   = self.library[lib_idx]

        menu = tk.Menu(self.root, tearoff=0, bg=BTN_BG, fg=TEXT,
                       activebackground=SEL_BG, activeforeground=TEXT,
                       relief="flat", bd=0, font=("TkDefaultFont", 11))
        menu.add_command(label="✏  Rename",       command=lambda: self._rename_game(entry))
        menu.add_command(label="⚙  Game Settings", command=lambda: self._show_game_settings(entry))
        menu.add_command(label="🔄  Change EXE",   command=lambda: self._show_exe_picker(entry))
        menu.add_command(label="🎮  Gamepad",      command=lambda: self._show_gamepad(entry))
        menu.add_separator()
        menu.add_command(label="🗑  Remove",        command=self._remove_selected)

        # Dismiss on any click outside
        menu.bind("<FocusOut>", lambda e: menu.unpost())
        self.root.bind("<Button-1>", lambda e: menu.unpost(), add="+")

        # Force window to be fully rendered before computing popup position
        self.root.update_idletasks()
        x = self.root.winfo_pointerx()
        y = self.root.winfo_pointery()

        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    # ── Ingest Pipeline ───────────────────────────────────────────────────

    def _add_archive(self):
        """Open file picker using tkinter dialog (Windows native)."""
        path = filedialog.askopenfilename(
            title="Select a game archive",
            filetypes=ARCHIVE_FILTERS,
            parent=self.root,
        )
        if not path:
            return
        archive = Path(path)

        if any(e["archive_name"] == archive.name for e in self.library):
            if not messagebox.askyesno("Duplicate",
                    f"'{archive.name}' is already in your library. Re-import it?",
                    parent=self.root):
                return

        GAMES_DIR.mkdir(exist_ok=True)

        threading.Thread(
            target=self._ingest_thread,
            args=(archive,),
            daemon=True,
        ).start()

    def _ingest_thread(self, archive: Path):
        """Worker: extract, detect EXE, update library."""
        archive_stem = archive_base_name(archive)
        try:
            GAMES_DIR.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".importing-", dir=GAMES_DIR))
        except OSError as e:
            self._show_error_later("Extraction Failed", str(e))
            return
        try:
            try:
                extract_archive(archive, staging)
            except Exception as e:
                self._show_error_later("Extraction Failed", str(e))
                return

            items = list(staging.iterdir())
            if len(items) == 1 and items[0].is_dir():
                # ExoDOS archives hold one folder named with the game's ExoDOS
                # id (e.g. 'Syndicat'): it becomes games\Syndicat, and the game
                # is named and looked up by it. depth_offset keeps exe scoring
                # as if the files were still inside that folder.
                src, stem, depth_offset = items[0], items[0].name, 1
            else:
                src, stem, depth_offset = staging, archive_stem, 0

            if not detect_exe(src, stem, depth_offset)[0]:
                self._show_error_later(
                    "No Executable Found",
                    "No .exe, .com or .bat files were found in this archive.")
                return

            dest = self._free_game_folder(stem, archive.name)
            move_into(src, dest)
            exes, best = detect_exe(dest, stem, depth_offset)

            exo = lookup_exodos(stem.replace("-", " ").replace("_", " ").title())
            display_name = (archive_stem if " " in archive_stem else
                            archive_stem.replace("-", " ").replace("_", " ").title())

            # ExoDOS match found — apply silently, no popup

            entry = {
                "id":             dest.name,
                "name":           exo["title"] if exo and exo.get("title") else display_name,
                "archive_name":   archive.name,
                "extracted_path": str(dest),
                "exe_path":       "",
                "date_added":     str(date.today()),
                "cycles":         str(exo.get("cycles") or "auto") if exo else "auto",
                "memsize":        snap_memsize(int(exo["memsize"])) if exo and exo.get("memsize") else 16,
                "xms":            bool(exo.get("xms", True)) if exo else True,
                "ems":            bool(exo.get("ems", True)) if exo else True,
                "depth_offset":   depth_offset,
            }
        except Exception as e:
            self._show_error_later("Import Failed", str(e))
            return
        finally:
            remove_tree(staging)

        def finish():
            if best:
                entry["exe_path"] = str(best.relative_to(dest))
                self._add_to_library(entry)
                RenameModal(self.root, entry, on_save=self._on_rename_saved,
                            then=lambda: self._launch_entry(entry))
            else:
                ExePickerModal(self.root, exes, dest, entry,
                               on_confirm=self._on_exe_picked)
        self.root.after(0, finish)

    def _show_error_later(self, title: str, message: str):
        """Show an error box from a worker thread."""
        self.root.after(0, lambda: messagebox.showerror(
            title, message or "Unknown error", parent=self.root))

    def _free_game_folder(self, name: str, archive_name: str) -> Path:
        """games\\<name> for a new import, or games\\<name> (2), (3)... if that
        folder already belongs to a different game in the library. Importing
        the same archive again reuses its folder."""
        library = list(self.library)

        def taken(folder: Path) -> bool:
            if not folder.exists():
                return False
            if not folder.is_dir():
                return True
            key = os.path.normcase(os.path.abspath(folder))
            return any(e.get("extracted_path")
                       and os.path.normcase(os.path.abspath(e["extracted_path"])) == key
                       and e.get("archive_name") != archive_name
                       for e in library)

        folder, n = GAMES_DIR / name, 2
        while taken(folder):
            folder, n = GAMES_DIR / f"{name} ({n})", n + 1
        return folder

    def _add_to_library(self, entry: dict):
        """Add a newly imported game. Importing the same archive again replaces
        its old entry, keeping the name and gamepad setup set for it, and
        ids stay unique so renames and settings reach the right game."""
        old = [e for e in self.library if e.get("archive_name") == entry["archive_name"]]
        if old:
            entry["name"] = old[0].get("name") or entry["name"]
            if old[0].get("gamepad"):
                entry["gamepad"] = old[0]["gamepad"]
            self.library = [e for e in self.library if not any(e is o for o in old)]
        ids = {e["id"] for e in self.library}
        base, n = entry["id"], 2
        while entry["id"] in ids:
            entry["id"], n = f"{base} ({n})", n + 1
        self.library.append(entry)
        self._save_library()
        self._refresh_list()

    def _on_exe_picked(self, entry: dict, chosen: Path, dest: Path):
        """Called when user picks an EXE from the picker modal."""
        entry["exe_path"] = str(chosen.relative_to(dest))
        if any(e is entry for e in self.library):
            self._save_library()           # Change EXE on a game already listed
            self._refresh_list()
        else:
            self._add_to_library(entry)    # finishing a new import
        RenameModal(self.root, entry, on_save=self._on_rename_saved,
                    then=lambda: self._launch_entry(entry))

    # ── Launch ────────────────────────────────────────────────────────────

    def _launch_selected(self):
        """Launch the currently selected game."""
        entry = self._get_selected_entry()
        if not entry:
            return
        self._launch_entry(entry)

    def _launch_entry(self, entry: dict):
        """Launch a game entry via DOSBox.

        Case 1 — SIMPLE: exe on C:, optional ISO on D:
        Case 2 — CD_ONLY (games added with the old Add CD button): imgmount
                 all ISOs as D:, boot from D:, run cd_exe
        """
        if not self.dosbox:
            messagebox.showerror(
                "DOSBox Not Found",
                "DOSBox Staging not found.\n"
                "Place dosbox.exe in the dosbox\\ folder next to autodos_win.py,\n"
                "or download from: https://dosbox-staging.github.io",
                parent=self.root)
            return

        extracted = Path(entry["extracted_path"])
        exe_rel   = entry.get("exe_path", "")
        # Add CD games store their discs (older entries can list a .cue's .bin
        # as an extra disc, so those tracks are dropped again here)
        cd_isos   = [str(f) for f in drop_cue_tracks([Path(p) for p in entry.get("cd_isos", [])])]
        cd_mount  = entry.get("cd_mount", False)
        cd_exe    = entry.get("cd_exe", "")
        binary, prefix = self.dosbox

        # ── Per-game DOSBox settings ─────────────────────────────────────────
        # Passed with -set so they override dosbox.conf before DOSBox starts:
        # memory, EMS and XMS can't change once it's running ('set memsize='
        # in the DOS shell only sets an environment variable).
        try:
            memsize = snap_memsize(int(entry.get("memsize", 16)))
        except (TypeError, ValueError):
            memsize = 16
        base_args = (["-conf", str(ASSET_DIR / "dosbox.conf")]
                     + dosbox_cycles_args(entry.get("cycles"))
                     + ["-set", f"memsize={memsize}",
                        "-set", f"ems={str(bool(entry.get('ems', True))).lower()}",
                        "-set", f"xms={str(bool(entry.get('xms', True))).lower()}"])
        # The game's gamepad setup, as a DOSBox mapper file. When no pad
        # control is set to Joystick, DOS doesn't see a joystick at all.
        try:
            mapper, uses_joystick = write_gamepad_mapper(entry)
            base_args += ["-set", f"mapperfile={mapper}"]
            if not uses_joystick:
                base_args += ["-set", "joysticktype=hidden"]
        except OSError:
            pass  # launch with DOSBox's own default bindings instead

        def make_imgmount(isos):
            isos   = [mountable_disc(iso) for iso in isos]
            quoted = " ".join(f'"{iso}"' if " " in iso else iso for iso in isos)
            return f"imgmount D {quoted} -t iso"

        # ── Case 2: CD-only — exe lives on the disc ───────────────────────────
        if cd_mount and cd_isos:
            if not cd_exe:
                messagebox.showwarning(
                    "No Disc Exe Set",
                    "No executable set for this CD game.\n"
                    "Remove it and add the game's 7z instead.",
                    parent=self.root)
                return
            c_path  = str(extracted)
            mount_c = f'mount c "{c_path}"' if " " in c_path else f"mount c {c_path}"
            run_cmd = f'"{cd_exe}"' if " " in cd_exe else cd_exe
            cmd = [binary] + prefix + base_args
            cmd += [
                "-c", mount_c,
                "-c", make_imgmount(cd_isos),
                "-c", "D:",
                "-c", run_cmd,
                "-c", "exit",
            ]
            threading.Thread(target=self._dosbox_thread, args=(cmd, entry), daemon=True).start()
            return

        # ── Case 1: exe on C:, optional ISO on D: ────────────────────────────
        if not exe_rel:
            self._show_exe_picker(entry)
            return

        exe_name    = Path(exe_rel).name
        exe_dir     = (extracted / exe_rel).parent
        exe_dir_str = str(exe_dir)
        mount_c     = f'mount c "{exe_dir_str}"' if " " in exe_dir_str else f"mount c {exe_dir_str}"
        run_cmd     = f'"{exe_name}"' if " " in exe_name else exe_name

        if "cd_isos" not in entry:
            # Games added from an archive: mount the disc images in the game's
            # CD folder as D: (e.g. ExoDOS CD games). Looks from the exe's
            # folder up to the game folder, which also covers games imported
            # inside a folder named after their archive.
            for folder in (d for d in (exe_dir, *exe_dir.parents)
                           if d.is_relative_to(extracted)):
                cd_isos = find_game_discs(folder)
                if cd_isos:
                    break

        cmd = [binary] + prefix + base_args
        cmd += [
            "-c", mount_c,
        ]
        if cd_isos:
            cmd += ["-c", make_imgmount(cd_isos)]
        cmd += ["-c", "c:", "-c", run_cmd, "-c", "exit"]
        threading.Thread(target=self._dosbox_thread, args=(cmd, entry), daemon=True).start()

    def _dosbox_thread(self, cmd: list, entry: dict):
        """Worker: run DOSBox and wait for it to close."""
        proc = subprocess.Popen(cmd, shell=False)
        proc.wait()
        if proc.returncode != 0:
            self.root.after(0, lambda: self._show_exe_picker(entry))

    def _on_settings_saved(self, entry: dict):
        """Save updated game settings to library."""
        for i, e in enumerate(self.library):
            if e["id"] == entry["id"]:
                self.library[i] = entry
                break
        self._save_library()

    def _show_exe_picker(self, entry: dict):
        """Open EXE picker modal for failed or ambiguous launch."""
        dest = Path(entry["extracted_path"])
        exes, _ = detect_exe(dest, entry["id"], entry.get("depth_offset", 0))
        if not exes:
            messagebox.showerror("Error", "No executables found in game directory.", parent=self.root)
            return
        ExePickerModal(self.root, exes, dest, entry, on_confirm=self._on_exe_picked)

    # ── Rename ───────────────────────────────────────────────────────────────

    def _rename_game(self, entry: dict):
        """Open rename dialog for a game from the context menu."""
        RenameModal(self.root, entry, on_save=self._on_rename_saved)

    def _on_rename_saved(self, entry: dict, new_name: str):
        """Save renamed game back to library."""
        entry["name"] = new_name
        for i, e in enumerate(self.library):
            if e["id"] == entry["id"]:
                self.library[i] = entry
                break
        self._save_library()
        self._refresh_list()

    def _show_game_settings(self, entry: dict):
        GameSettingsModal(self.root, entry, on_save=self._on_settings_saved)

    # ── Gamepad ───────────────────────────────────────────────────────────

    def _show_gamepad(self, entry: dict):
        """Open the gamepad setup for a game from the context menu."""
        GamepadModal(self.root, entry, on_save=self._on_gamepad_saved)

    def _on_gamepad_saved(self, entry: dict, settings: dict):
        """Save a game's gamepad setup to the library."""
        entry["gamepad"] = settings
        self._save_library()

    # ── Remove ────────────────────────────────────────────────────────────

    def _remove_selected(self):
        """Remove selected game from library.
        Only offers to delete files if the folder is inside AutoDOS games dir.
        External CD game folders are never deleted.
        """
        lib_idx = self._get_selected_lib_index()
        if lib_idx is None:
            return
        entry      = self.library[lib_idx]
        ext_path   = Path(entry["extracted_path"])
        is_managed = ext_path.is_relative_to(GAMES_DIR)

        if is_managed:
            result = messagebox.askyesnocancel(
                "Remove Game",
                "Remove '" + entry["name"] + "' from your library?\n\n"
                "Yes = remove entry AND delete files\n"
                "No  = remove entry only, keep files",
                parent=self.root)
            if result is None:
                return
            if result:
                remove_tree(Path(entry["extracted_path"]))
        else:
            result = messagebox.askyesno(
                "Remove Game",
                "Remove '" + entry["name"] + "' from your library?\n"
                "The game folder will not be deleted.",
                parent=self.root)
            if not result:
                return

        self.library.pop(lib_idx)
        self._save_library()
        self._refresh_list()


# ── Modals ───────────────────────────────────────────────────────────────────

class ExePickerModal:
    """Modal dialog for selecting an EXE when auto-detection is ambiguous or fails."""

    def __init__(self, parent: tk.Tk, exes: list, dest: Path,
                 entry: dict, on_confirm):
        self.dest       = dest
        self.entry      = entry
        self.on_confirm = on_confirm
        self.exes       = exes

        self.win = tk.Toplevel(parent)
        self.win.title("Choose Executable")
        self.win.configure(bg=BG)
        self.win.geometry("460x320")
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.lift()
        self.win.focus_force()
        dark_title_bar(self.win)

        tk.Label(self.win,
                 text="Multiple executables found.\nSelect the correct one to launch:",
                 bg=BG, fg=TEXT, font=("TkDefaultFont", 11),
                 justify=tk.LEFT).pack(anchor="w", padx=16, pady=(16, 8))

        frame = tk.Frame(self.win, bg=BORDER, bd=1)
        frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        self.lb = tk.Listbox(
            frame, bg=LIST_BG, fg=TEXT,
            selectbackground=SEL_BG, selectforeground=TEXT,
            activestyle="none", relief="flat", bd=0,
            font=("TkDefaultFont", 11), highlightthickness=0,
            exportselection=False)
        self.lb.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.lb.bind("<Double-Button-1>", lambda e: self._confirm())
        self.lb.bind("<Return>", lambda e: self._confirm())

        for exe in exes:
            try:
                rel = exe.relative_to(dest)
            except ValueError:
                rel = exe
            self.lb.insert(tk.END, f"  {rel}")

        self.lb.selection_set(0)
        self.lb.focus_set()

        btn_frame = tk.Frame(self.win, bg=BG)
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 14))

        ttk.Button(btn_frame, text="▶  Launch", style="Pill.TButton",
                   command=self._confirm).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Cancel", style="Pill.TButton",
                   command=self.win.destroy).pack(side=tk.LEFT)

    def _confirm(self):
        """Confirm selection and invoke callback."""
        sel = self.lb.curselection()
        if not sel:
            return
        chosen = self.exes[sel[0]]
        self.win.destroy()
        self.on_confirm(self.entry, chosen, self.dest)


# ── Game Settings Modal ──────────────────────────────────────────────────────

class GameSettingsModal:
    """Per-game DOSBox settings dialog."""

    def __init__(self, parent: tk.Tk, entry: dict, on_save):
        self.entry   = dict(entry)
        self.on_save = on_save
        self.parent  = parent

        self.win = tk.Toplevel(parent)
        self.win.title("Game Settings — " + entry["name"])
        self.win.configure(bg=BG)
        self.win.geometry("480x520")
        self.win.minsize(480, 520)
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.lift()
        self.win.focus_force()
        dark_title_bar(self.win)

        self._build()

    def _build(self):
        entry = self.entry

        # ── Header ──────────────────────────────────────────────────────
        exo = lookup_exodos(entry["name"])
        if exo:
            status = "ExoDOS match: " + exo.get("title", entry["name"])
            color  = GOOD
        else:
            status = "No ExoDOS match — set manually or search below"
            color  = MUTED

        tk.Label(self.win, text=status, bg=BG, fg=color,
                 font=("TkDefaultFont", 9, "bold")
                 ).pack(anchor="w", padx=16, pady=(14, 0))

        tk.Frame(self.win, bg=BORDER, height=1).pack(fill=tk.X, padx=16, pady=10)

        # ── Settings form ────────────────────────────────────────────────
        form = tk.Frame(self.win, bg=BG)
        form.pack(fill=tk.X, padx=16)
        form.columnconfigure(1, weight=1)

        def field(label, var, row, hint=None):
            tk.Label(form, text=label, bg=BG, fg=TEXT,
                     font=("TkDefaultFont", 11), anchor="w", width=14
                     ).grid(row=row, column=0, sticky="w", pady=5)
            e = tk.Entry(form, textvariable=var, bg=LIST_BG, fg=TEXT,
                         relief="flat", bd=0, font=("TkDefaultFont", 11),
                         insertbackground=TEXT, selectbackground=SEL_BG,
                         selectforeground=TEXT, highlightcolor=SEL_BG,
                         highlightthickness=1, highlightbackground=BORDER)
            e.grid(row=row, column=1, sticky="ew", pady=5)
            if hint:
                tk.Label(form, text=hint, bg=BG, fg=MUTED,
                         font=("TkDefaultFont", 8)
                         ).grid(row=row+1, column=1, sticky="w")

        def checkbox(label, var, row):
            tk.Label(form, text=label, bg=BG, fg=TEXT,
                     font=("TkDefaultFont", 11), anchor="w", width=14
                     ).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Checkbutton(form, variable=var
                            ).grid(row=row, column=1, sticky="w", pady=5)

        self.cycles_var = tk.StringVar(value=str(entry.get("cycles") or "auto"))
        self.mem_var    = tk.StringVar(value=str(entry.get("memsize", 16)))
        self.xms_var    = tk.BooleanVar(value=bool(entry.get("xms", True)))
        self.ems_var    = tk.BooleanVar(value=bool(entry.get("ems", True)))

        field("CPU Cycles:", self.cycles_var, 0,
              "e.g.  auto  |  3000  |  max  |  max limit 35000")
        field("Mem (MB):", self.mem_var, 2)
        checkbox("XMS:", self.xms_var, 4)
        checkbox("EMS:", self.ems_var, 5)

        tk.Frame(self.win, bg=BORDER, height=1).pack(fill=tk.X, padx=16, pady=10)

        # ── Save button ───────────────────────────────────────────────────
        save_row = tk.Frame(self.win, bg=BG)
        save_row.pack(fill=tk.X, padx=16, pady=(0, 6))

        ttk.Button(save_row, text="💾  Save", style="Pill.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(save_row, text="Cancel", style="Pill.TButton",
                   command=self.win.destroy).pack(side=tk.LEFT)

        tk.Frame(self.win, bg=BORDER, height=1).pack(fill=tk.X, padx=16, pady=(6, 10))

        # ── ExoDOS search ─────────────────────────────────────────────────
        tk.Label(self.win, text="Search ExoDOS Database",
                 bg=BG, fg=TEXT, font=("TkDefaultFont", 11, "bold")
                 ).pack(anchor="w", padx=16, pady=(0, 6))

        search_row = tk.Frame(self.win, bg=BG)
        search_row.pack(fill=tk.X, padx=16, pady=(0, 6))

        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(search_row, textvariable=self.search_var,
                                     bg=LIST_BG, fg=TEXT, relief="flat", bd=0,
                                     font=("TkDefaultFont", 11),
                                     insertbackground=TEXT, selectbackground=SEL_BG,
                                     selectforeground=TEXT, highlightcolor=SEL_BG,
                                     highlightthickness=1,
                                     highlightbackground=BORDER)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.search_entry.bind("<Return>", lambda e: self._do_search())
        self.search_entry.bind("<KeyRelease>", self._on_key)

        ttk.Button(search_row, text="Search", style="Pill.TButton",
                   command=self._do_search).pack(side=tk.LEFT)

        # Results listbox
        res_frame = tk.Frame(self.win, bg=BORDER, bd=1)
        res_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 6))

        self.results_lb = tk.Listbox(
            res_frame, bg=LIST_BG, fg=TEXT,
            selectbackground=SEL_BG, selectforeground=TEXT,
            activestyle="none", relief="flat", bd=0,
            font=("TkDefaultFont", 11), highlightthickness=0,
            exportselection=False, height=5)
        self.results_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                             padx=1, pady=1)
        self.results_lb.bind("<Double-Button-1>", lambda e: self._apply_result())
        self.results_lb.bind("<Return>", lambda e: self._apply_result())

        sb = ttk.Scrollbar(res_frame, orient=tk.VERTICAL,
                           command=self.results_lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.results_lb.configure(yscrollcommand=sb.set)

        self.search_matches = []

        apply_row = tk.Frame(self.win, bg=BG)
        apply_row.pack(fill=tk.X, padx=16, pady=(0, 12))

        ttk.Button(apply_row, text="✓  Apply & Save",
                   style="Pill.TButton",
                   command=self._apply_result).pack(side=tk.LEFT)

        self.status_label = tk.Label(self.win, text="", bg=BG, fg=MUTED,
                                     font=("TkDefaultFont", 9))
        self.status_label.pack(anchor="w", padx=16, pady=(0, 8))

    def _on_key(self, event):
        """Live search as user types."""
        if len(self.search_var.get()) >= 3:
            self._do_search()

    def _do_search(self):
        """Search ExoDOS and populate results list."""
        q = self.search_var.get().strip()
        if not q:
            return
        results = search_exodos(q)
        self.results_lb.delete(0, tk.END)
        self.search_matches = results[:20]
        if not results:
            self.status_label.config(text="No matches found.", fg=MUTED)
            return
        for k, v in self.search_matches:
            cycles = v.get("cycles", "?")
            mem    = v.get("memsize", "?")
            self.results_lb.insert(
                tk.END,
                f"  {v.get('title', k)}   [{cycles} cycles / {mem}MB]"
            )
        self.results_lb.selection_set(0)
        self.status_label.config(
            text=f"{len(results)} result(s) — double-click or Apply to use",
            fg=MUTED)

    def _apply_result(self):
        """Apply the selected ExoDOS result and save immediately."""
        sel = self.results_lb.curselection()
        if not sel:
            return
        _, exo = self.search_matches[sel[0]]
        self.cycles_var.set(str(exo.get("cycles", "auto")))
        self.mem_var.set(str(exo.get("memsize", 16)))
        self.xms_var.set(bool(exo.get("xms", True)))
        self.ems_var.set(bool(exo.get("ems", True)))
        # Auto-save so user doesn't need to click Save separately
        self._save()

    def _save(self):
        """Save settings to entry and call callback."""
        try:
            memsize = int(self.mem_var.get())
        except ValueError:
            memsize = 16
        self.entry["cycles"]  = self.cycles_var.get().strip()
        self.entry["memsize"] = memsize
        self.entry["xms"]     = self.xms_var.get()
        self.entry["ems"]     = self.ems_var.get()
        self.win.destroy()
        self.on_save(self.entry)


# ── Rename Modal ─────────────────────────────────────────────────────────────

class RenameModal:
    """Quick rename dialog shown after a game is added to the library.
    Only changes the display name — nothing else is touched.
    """

    def __init__(self, parent, entry: dict, on_save, then=None):
        self.entry   = entry
        self.on_save = on_save
        self.then    = then   # optional callback after save (e.g. launch)

        self.win = tk.Toplevel(parent)
        self.win.title("Name Your Game")
        self.win.configure(bg=BG)
        self.win.geometry("420x160")
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.lift()
        self.win.focus_force()
        dark_title_bar(self.win)

        tk.Label(self.win,
                 text="Give this game a name for your library:",
                 bg=BG, fg=TEXT, font=("TkDefaultFont", 11)
                 ).pack(anchor="w", padx=16, pady=(18, 6))

        self.name_var = tk.StringVar(value=entry["name"])
        name_entry = tk.Entry(
            self.win, textvariable=self.name_var,
            bg=LIST_BG, fg=TEXT, relief="flat", bd=0,
            font=("TkDefaultFont", 13), insertbackground=TEXT,
            selectbackground=SEL_BG, selectforeground=TEXT,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=SEL_BG,
        )
        name_entry.pack(fill=tk.X, padx=16, ipady=6)
        name_entry.select_range(0, tk.END)
        name_entry.focus_set()
        name_entry.bind("<Return>", lambda e: self._save())
        name_entry.bind("<Escape>", lambda e: self._skip())

        btn_frame = tk.Frame(self.win, bg=BG)
        btn_frame.pack(fill=tk.X, padx=16, pady=(12, 0))

        ttk.Button(btn_frame, text="OK", style="Pill.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Skip", style="Pill.TButton",
                   command=self._skip).pack(side=tk.LEFT)

    def _save(self):
        new_name = self.name_var.get().strip()
        if not new_name:
            new_name = self.entry["name"]
        self.win.destroy()
        self.on_save(self.entry, new_name)
        if self.then:
            self.then()

    def _skip(self):
        """Keep current name and continue."""
        self.win.destroy()
        if self.then:
            self.then()


# ── Gamepad Modal ────────────────────────────────────────────────────────────

class GamepadModal:
    """Per-game setup for a Microsoft SideWinder Game Pad: what the D-pad and
    each button do in the game, with a live test of the pad's buttons."""

    def __init__(self, parent, entry: dict, on_save):
        self.entry     = entry
        self.on_save   = on_save
        self.pad       = None
        self.next_scan = 0.0
        self.after_id  = None

        self.win = tk.Toplevel(parent)
        self.win.title("Gamepad — " + entry["name"])
        self.win.configure(bg=BG)
        self.win.geometry("460x610")
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.lift()
        self.win.focus_force()
        dark_title_bar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        tk.Label(self.win, text="Microsoft SideWinder Game Pad",
                 bg=BG, fg=TEXT, font=("TkDefaultFont", 11, "bold")
                 ).pack(anchor="w", padx=16, pady=(14, 0))
        tk.Label(self.win,
                 text="Joystick: games with joystick support use the pad as a "
                      "PC joystick.\nPick keys for games that only use the keyboard.",
                 bg=BG, fg=MUTED, font=("TkDefaultFont", 9), justify=tk.LEFT
                 ).pack(anchor="w", padx=16, pady=(2, 10))

        presets = tk.Frame(self.win, bg=BG)
        presets.pack(fill=tk.X, padx=16, pady=(0, 10))
        tk.Label(presets, text="Preset:", bg=BG, fg=TEXT,
                 font=("TkDefaultFont", 10)).pack(side=tk.LEFT, padx=(0, 8))
        for name in PAD_PRESETS:
            ttk.Button(presets, text=name, style="Pill.TButton",
                       command=lambda n=name: self._apply(PAD_PRESETS[n])
                       ).pack(side=tk.LEFT, padx=(0, 6))

        table = tk.Frame(self.win, bg=BORDER)
        table.pack(fill=tk.X, padx=16)
        self.lights, self.vars = {}, {}
        settings = gamepad_settings(entry)
        rows = [("dpad", "D-pad")] + [(c, label) for c, label, _ in PAD_BUTTONS]
        for i, (control, label) in enumerate(rows):
            bg = LIST_BG if i % 2 == 0 else ALT_ROW_BG
            row = tk.Frame(table, bg=bg)
            row.pack(fill=tk.X, padx=1, pady=(1 if i == 0 else 0, 1))
            light = tk.Label(row, text="●", bg=bg, fg=BORDER,
                             font=("TkDefaultFont", 11))
            light.pack(side=tk.LEFT, padx=(10, 6))
            tk.Label(row, text=label, bg=bg, fg=TEXT, font=("TkDefaultFont", 11),
                     width=10, anchor="w").pack(side=tk.LEFT, pady=5)
            var = tk.StringVar(value=self._name(control, settings[control]))
            ttk.Combobox(row, textvariable=var, state="readonly", width=24, height=14,
                         values=[name for _, name in self._choices(control)],
                         font=("TkDefaultFont", 10)
                         ).pack(side=tk.RIGHT, padx=8, pady=4)
            self.lights[control], self.vars[control] = light, var

        self.status = tk.Label(self.win, text="", bg=BG, fg=MUTED,
                               font=("TkDefaultFont", 9), justify=tk.LEFT,
                               wraplength=420)
        self.status.pack(anchor="w", padx=16, pady=(10, 0))

        buttons = tk.Frame(self.win, bg=BG)
        buttons.pack(fill=tk.X, padx=16, pady=(12, 14))
        ttk.Button(buttons, text="💾  Save", style="Pill.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="Cancel", style="Pill.TButton",
                   command=self._close).pack(side=tk.LEFT)

        self._poll()

    @staticmethod
    def _choices(control: str) -> list:
        """(setting, name shown) choices for one pad control."""
        if control == "dpad":
            return PAD_DPAD_CHOICES
        choices = []
        if control in PAD_JOYSTICK_BUTTONS:
            choices.append(("joystick", f"Joystick button {PAD_JOYSTICK_BUTTONS[control]}"))
        choices.append(("none", "Not used"))
        return choices + [(event, name) for name, event in PAD_KEYS]

    def _name(self, control: str, value: str) -> str:
        names = dict(self._choices(control))
        return names.get(value, "Not used")

    def _apply(self, settings: dict):
        for control, var in self.vars.items():
            var.set(self._name(control, settings.get(control, "none")))

    def _poll(self):
        """Light up the rows of the pad controls being pressed."""
        now = time.monotonic()
        if self.pad is None and now >= self.next_scan:
            self.next_scan = now + 2
            self.pad = find_gamepad()
            self.status.config(
                text=(self.pad[1] + " found. Press its buttons to test them: "
                      "the matching row lights up.") if self.pad else
                     "No gamepad found. Plug it in to test its buttons here.")
        state = read_gamepad(self.pad) if self.pad else None
        if self.pad and state is None:
            self.pad = None
            self.status.config(text="The gamepad was unplugged.")
        buttons, dirs = state or (set(), set())
        for control, _label, numbers in PAD_BUTTONS:
            pressed = any(n in buttons for n in numbers)
            self.lights[control].config(fg=ACCENT if pressed else BORDER)
        self.lights["dpad"].config(fg=ACCENT if dirs else BORDER)
        self.after_id = self.win.after(50, self._poll)

    def _save(self):
        settings = {}
        for control, var in self.vars.items():
            values = {name: value for value, name in self._choices(control)}
            settings[control] = values.get(var.get(), "none")
        self.on_save(self.entry, settings)
        self._close()

    def _close(self):
        if self.after_id:
            self.win.after_cancel(self.after_id)
        self.win.destroy()

# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    """Launch the AutoDOS application."""
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
