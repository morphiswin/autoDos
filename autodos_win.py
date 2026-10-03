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
#             used for: dosbox\, tools\, games\, dosbox.conf, library.json
# ASSET_DIR = where bundled read-only assets are extracted
#             used for: logo.png, master_games.json
if getattr(_sys, "frozen", False):
    BASE_DIR  = Path(_sys.executable).parent
    ASSET_DIR = Path(_sys._MEIPASS)
else:
    BASE_DIR  = Path(__file__).parent
    ASSET_DIR = BASE_DIR
GAMES_DIR            = BASE_DIR / "games"
CONTROLLER_MAPS_DIR  = BASE_DIR / "controller_maps"
LIBRARY_FILE    = BASE_DIR / "library.json"
# The one DOSBox config file, beside AutoDOS.exe
DOSBOX_CONF     = BASE_DIR / "dosbox.conf"
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
CD_IMAGE_EXTS   = {".iso", ".bin", ".cue", ".img", ".mdf"}
CD_FOLDER_NAMES = {"cd", "cdrom", "cd-rom", "disc", "disk", "dvd",
                   "cd1", "cd2", "disc1", "disc2", "disk1", "disk2"}

_ISO_EXE_BLACKLIST = {
    "setup", "install", "uninst", "uninstall", "patch", "update",
    "config", "cfg", "register", "readme", "read", "help",
    "directx", "dxsetup", "dos4gw", "cwsdpmi", "himemx",
    "dosbox", "scummvm", "loadpats", "intro", "movie", "logo",
    "start", "run", "main", "fixsave", "convert",
}


def detect_cd_source(game_dir: Path) -> list:
    """Scan a game folder for CD images. Returns sorted list of path strings."""
    images = []
    for ext in CD_IMAGE_EXTS:
        images += list(game_dir.rglob(f"*{ext}"))
        images += list(game_dir.rglob(f"*{ext.upper()}"))
    if images:
        return [str(f) for f in drop_cue_tracks(sorted(set(images), key=_disc_order))]
    for child in game_dir.iterdir():
        if child.is_dir() and child.name.lower() in CD_FOLDER_NAMES:
            found = []
            for ext in CD_IMAGE_EXTS:
                found += list(child.glob(f"*{ext}"))
                found += list(child.glob(f"*{ext.upper()}"))
            if found:
                return [str(f) for f in drop_cue_tracks(sorted(set(found), key=_disc_order))]
    return []


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


def scan_iso_for_exes(iso_path: str, skip_blacklisted: bool = True) -> list:
    """Scan a disc image's ISO 9660 file system for game executables.
    Reads .iso images and .bin data tracks (raw 2352-byte CD sectors).
    Returns paths on the disc in uppercase, root files first, e.g.
    ['WC3.EXE', 'GAME\\PLAY.BAT']. Blacklisted names are filtered out
    unless skip_blacklisted is False.
    """
    candidates = []
    try:
        with open(iso_path, "rb") as f:
            # Raw CD sectors start with a 12-byte sync pattern; their 2048 data
            # bytes follow the header (16 bytes in mode 1, 24 in mode 2)
            head = f.read(16)
            if head[:12] == b"\x00" + b"\xff" * 10 + b"\x00":
                size, skip = 2352, (24 if head[15] == 2 else 16)
            else:
                size, skip = 2048, 0

            def read(lba: int, length: int) -> bytes:
                data = b""
                for n in range(min((length + 2047) // 2048, 512)):
                    f.seek((lba + n) * size + skip)
                    data += f.read(2048)
                return data

            # Find the primary volume descriptor (from sector 16 on)
            root = None
            for lba in range(16, 32):
                vd = read(lba, 2048)
                if vd[1:6] != b"CD001" or vd[0] == 255:
                    break
                if vd[0] == 1:
                    root = vd[156:190]
                    break
            if not root:
                return []

            seen, visited = set(), set()

            def walk(lba: int, length: int, folder: str, depth: int):
                if depth > 8 or lba in visited:
                    return
                visited.add(lba)
                data, pos = read(lba, length), 0
                while pos + 34 <= len(data):
                    rec_len = data[pos]
                    if rec_len == 0:            # rest of this sector is padding
                        pos = (pos // 2048 + 1) * 2048
                        continue
                    rec, pos = data[pos:pos + rec_len], pos + rec_len
                    if len(rec) < 34:                # damaged record
                        continue
                    raw = rec[33:33 + rec[32]]
                    if raw in (b"\x00", b"\x01"):   # '.' and '..'
                        continue
                    name = raw.decode("ascii", "replace").upper().split(";")[0].rstrip(".")
                    if not name:
                        continue
                    if rec[25] & 2:                 # a folder
                        walk(int.from_bytes(rec[2:6], "little"),
                             int.from_bytes(rec[10:14], "little"),
                             folder + name + "\\", depth + 1)
                        continue
                    stem, _, ext = name.rpartition(".")
                    if ext not in ("EXE", "COM", "BAT"):
                        continue
                    if len(stem) < 2 or (skip_blacklisted
                                         and stem.lower() in _ISO_EXE_BLACKLIST):
                        continue
                    if folder + name not in seen:
                        seen.add(folder + name)
                        candidates.append(folder + name)

            walk(int.from_bytes(root[2:6], "little"),
                 int.from_bytes(root[10:14], "little"), "", 0)
    except Exception:
        return []

    # Sort: root files first, then EXE, BAT, COM, then alphabetical
    def rank(path):
        ext = path.rsplit(".", 1)[-1]
        return (path.count("\\"), {"EXE": 0, "BAT": 1, "COM": 2}.get(ext, 3), path)

    candidates.sort(key=rank)
    return candidates


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
# GAMEPAD — DirectInput and XInput controllers
# ══════════════════════════════════════════════════════════════════════════════
# DOSBox reads the controller itself; AutoDOS writes a DOSBox mapper file per
# game that says what each control does there. A mapper file replaces all of
# DOSBox's bindings, so it always carries DOSBox's own defaults in full.
#
# DOSBox reads controllers through SDL, which numbers their buttons and axes
# differently for XInput, DirectInput and other kinds of controller, so the
# Gamepad window reads them through DOSBox's own SDL2.dll: the numbers it
# saves are the ones DOSBox sees.

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

# A controller is a list of controls: D-pads and sticks, with the inputs that
# push them up, down, left and right, and buttons (and triggers) with the
# inputs that press them. Inputs are named the way DOSBox's mapper names them,
# less the "stick_N " in front:
#   {"id": "dpad", "label": "D-pad", "dirs": {"up": ["hat 0 1"], ...}}
#   {"id": "a",    "label": "A",     "binds": ["button 0"]}
PAD_DIRECTIONS = ("up", "down", "left", "right")
PAD_INPUT = re.compile(r"button \d+|axis \d+ [01]|hat 0 [1248]")

# The buttons in SDL's controller database, in the order the Gamepad window
# lists them, named as on Xbox controllers...
PAD_NAMES = {
    "a": "A", "b": "B", "x": "X", "y": "Y",
    "leftshoulder": "LB", "rightshoulder": "RB",
    "lefttrigger": "LT", "righttrigger": "RT",
    "back": "Back", "start": "Start", "guide": "Guide",
    "leftstick": "Left stick click", "rightstick": "Right stick click",
    "misc1": "Extra button", "paddle1": "Paddle 1", "paddle2": "Paddle 2",
    "paddle3": "Paddle 3", "paddle4": "Paddle 4", "touchpad": "Touchpad",
}
# ...and as on other kinds of controller, by SDL's controller type
_PLAYSTATION_NAMES = {"a": "Cross", "b": "Circle", "x": "Square", "y": "Triangle",
                      "leftshoulder": "L1", "rightshoulder": "R1",
                      "lefttrigger": "L2", "righttrigger": "R2",
                      "guide": "PS button", "leftstick": "L3", "rightstick": "R3"}
_NINTENDO_NAMES = {"a": "B", "b": "A", "x": "Y", "y": "X",
                   "leftshoulder": "L", "rightshoulder": "R",
                   "lefttrigger": "ZL", "righttrigger": "ZR",
                   "back": "Minus", "start": "Plus", "guide": "Home", "misc1": "Capture"}
PAD_TYPE_NAMES = {
    1: {"guide": "Xbox button"},                                       # Xbox 360
    2: {"back": "View", "start": "Menu", "guide": "Xbox button",
        "misc1": "Share"},                                             # Xbox One / Series
    3: dict(_PLAYSTATION_NAMES, back="Select"),                        # PlayStation 3
    4: dict(_PLAYSTATION_NAMES, back="Share", start="Options"),        # PlayStation 4
    7: dict(_PLAYSTATION_NAMES, back="Create", start="Options",
            misc1="Mic"),                                              # PlayStation 5
    5: _NINTENDO_NAMES, 11: _NINTENDO_NAMES, 12: _NINTENDO_NAMES,
    13: _NINTENDO_NAMES,                                               # Switch
}
_SDL_DPAD   = {"dpup": "up", "dpdown": "down", "dpleft": "left", "dpright": "right"}
_SDL_STICKS = {"leftx":  ("lstick", "left", "right"), "lefty":  ("lstick", "up", "down"),
               "rightx": ("rstick", "left", "right"), "righty": ("rstick", "up", "down")}

# What a D-pad or stick can do, and the DOSBox events for each direction.
# The PC joystick's axes 3 and 4 (and buttons 3 and 4, below) belong to the
# first joystick when DOSBox finds one controller and to the second when it
# finds two, so they're bound both ways.
PAD_DIR_CHOICES = [
    ("joystick",  "Joystick"),
    ("joystick2", "Joystick axes 3 + 4"),
    ("arrows",    "Arrow keys"),
    ("wasd",      "W A S D"),
    ("numpad",    "Numpad 8 4 6 2"),
    ("none",      "Not used"),
]
PAD_DIR_EVENTS = {
    "joystick":  {"up": ["jaxis_0_1-"], "down": ["jaxis_0_1+"],
                  "left": ["jaxis_0_0-"], "right": ["jaxis_0_0+"]},
    "joystick2": {"up": ["jaxis_0_3-", "jaxis_1_1-"], "down": ["jaxis_0_3+", "jaxis_1_1+"],
                  "left": ["jaxis_0_2-", "jaxis_1_0-"], "right": ["jaxis_0_2+", "jaxis_1_0+"]},
    "arrows":    {"up": ["key_up"], "down": ["key_down"],
                  "left": ["key_left"], "right": ["key_right"]},
    "wasd":      {"up": ["key_w"], "down": ["key_s"], "left": ["key_a"], "right": ["key_d"]},
    "numpad":    {"up": ["key_kp_8"], "down": ["key_kp_2"],
                  "left": ["key_kp_4"], "right": ["key_kp_6"]},
}
PAD_JOY_BUTTONS = [("joy1", "Joystick button 1"), ("joy2", "Joystick button 2"),
                   ("joy3", "Joystick button 3"), ("joy4", "Joystick button 4")]
PAD_JOY_EVENTS = {"joy1": ["jbutton_0_0"], "joy2": ["jbutton_0_1"],
                  "joy3": ["jbutton_0_2", "jbutton_1_0"],
                  "joy4": ["jbutton_0_3", "jbutton_1_1"]}
PAD_JOYSTICK_ACTIONS = {"joystick", "joystick2", "joy1", "joy2", "joy3", "joy4"}

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
       ("Left mouse button", "mouse_left"), ("Right mouse button", "mouse_right"),
       ("Middle mouse button", "mouse_middle")]
)
PAD_KEY_EVENTS = {event for _, event in PAD_KEYS}

PAD_PRESETS = {
    # Games with joystick support: the D-pad, left stick and the four face
    # buttons are a PC joystick, and Start presses Esc
    "Joystick": {"dpad": "joystick", "lstick": "joystick",
                 "a": "joy1", "b": "joy2", "x": "joy3", "y": "joy4", "start": "key_esc",
                 # a controller SDL doesn't know: its first four buttons
                 "button0": "joy1", "button1": "joy2", "button2": "joy3", "button3": "joy4"},
    # Keyboard-only games
    "Keyboard": {"dpad": "arrows", "lstick": "arrows",
                 "a": "key_lctrl", "b": "key_space", "x": "key_lalt", "y": "key_enter",
                 "leftshoulder": "key_lshift", "rightshoulder": "key_tab",
                 "lefttrigger": "key_comma", "righttrigger": "key_period",
                 "start": "key_esc",
                 "button0": "key_lctrl", "button1": "key_space", "button2": "key_lalt",
                 "button3": "key_enter", "button4": "key_lshift", "button5": "key_tab",
                 "button6": "key_comma", "button7": "key_period"},
}


def pad_controls(mapping: str, buttons: int, axes: int, hats: int,
                 rest: list, pad_type: int = 0) -> list:
    """A controller's controls, from SDL's controller database entry for it
    ('' if SDL doesn't know it) and its button, axis and hat counts. rest is
    where each axis sits untouched: an axis resting at one end is a trigger."""
    names = dict(PAD_NAMES, **PAD_TYPE_NAMES.get(pad_type, {}))
    dirs = {"dpad": {}, "lstick": {}, "rstick": {}}
    pressed_by = {}           # button -> the inputs that press it
    used = set()              # inputs the database entry covers: 'b3', 'a1', 'h0'

    for field in mapping.split(",")[2:]:
        target, _, source = field.partition(":")
        half = target[:1] if target[:1] in "+-" else ""
        target = target.lstrip("+-")
        m = re.fullmatch(r"([+-]?)([abh])(\d+)(?:\.([1248]))?(~?)", source.strip())
        if not m or (m[2] == "h") != bool(m[4]):
            continue
        sign, kind, num, mask, inverted = m.groups()
        num = str(int(num))
        if kind == "h" and num != "0":
            continue          # DOSBox only reads a controller's first hat
        used.add(kind + num)

        def press(plus=True):
            """The input that's pressed: a button, a hat direction, or an
            axis pushed toward its + end (its - end for plus=False)."""
            if kind == "b":
                return f"button {num}"
            if kind == "h":
                return f"hat 0 {mask}"
            toward_plus = (sign != "-") == plus
            return f"axis {num} {int(toward_plus != bool(inverted))}"

        if target in _SDL_DPAD:
            dirs["dpad"].setdefault(_SDL_DPAD[target], []).append(press())
        elif target in _SDL_STICKS:
            group, minus, plus = _SDL_STICKS[target]
            if half:
                dirs[group].setdefault(minus if half == "-" else plus, []).append(press())
            else:
                dirs[group].setdefault(minus, []).append(press(False))
                dirs[group].setdefault(plus, []).append(press())
        elif target in PAD_NAMES:
            pressed_by.setdefault(target, []).append(press())

    sticks = [{"id": group, "label": label, "dirs": dirs[group]}
              for group, label in (("dpad", "D-pad"), ("lstick", "Left stick"),
                                   ("rstick", "Right stick")) if dirs[group]]
    pressables = [{"id": button, "label": names[button], "binds": pressed_by[button]}
                  for button in PAD_NAMES if button in pressed_by]

    # Inputs the database entry leaves out (all of them for a controller SDL
    # doesn't know). DOSBox reads up to 10 axes and 36 buttons.
    taken = {c["id"] for c in sticks}

    def free_id(*ids):
        free = next((i for i in ids if i not in taken), ids[-1])
        taken.add(free)
        return free

    if hats > 0 and "h0" not in used:
        sticks.append({"id": free_id("dpad", "hat0"), "label": "D-pad",
                       "dirs": {"up": ["hat 0 1"], "down": ["hat 0 4"],
                                "left": ["hat 0 8"], "right": ["hat 0 2"]}})
    free_axes = [a for a in range(min(axes, 10)) if f"a{a}" not in used]
    triggers = [a for a in free_axes if a < len(rest) and abs(rest[a]) > 24000]
    stick_axes = [a for a in free_axes if a not in triggers]
    for x, y in zip(stick_axes[::2], stick_axes[1::2]):
        group = free_id("lstick", "rstick", f"axes{x}")
        sticks.append({"id": group,
                       "label": {"lstick": "Left stick", "rstick": "Right stick"}.get(
                           group, f"Stick (axes {x + 1} + {y + 1})"),
                       "dirs": {"up": [f"axis {y} 0"], "down": [f"axis {y} 1"],
                                "left": [f"axis {x} 0"], "right": [f"axis {x} 1"]}})
    if len(stick_axes) % 2:
        axis = stick_axes[-1]
        pressables.append({"id": f"axis{axis}", "label": f"Axis {axis + 1}",
                           "binds": [f"axis {axis} 1"]})
    for axis in triggers:
        pressables.append({"id": f"axis{axis}", "label": f"Trigger (axis {axis + 1})",
                           "binds": [f"axis {axis} {int(rest[axis] < 0)}"]})
    pressables += [{"id": f"button{b}", "label": f"Button {b + 1}", "binds": [f"button {b}"]}
                   for b in range(min(buttons, 36)) if f"b{b}" not in used]

    order = {"dpad": 0, "lstick": 1, "rstick": 2}
    return sorted(sticks, key=lambda c: order.get(c["id"], 3)) + pressables


def generic_pad_controls() -> list:
    """The controls to go by before AutoDOS has seen the controller: a D-pad,
    two sticks and 12 buttons, numbered as most controllers number them."""
    return pad_controls("", 12, 4, 1, [0] * 4)


# The controller the Gamepad window saw last. Games use it to know which
# button is which; each game's own setup only says what each control does.
PAD_FILE = CONTROLLER_MAPS_DIR / "controller.json"


def _valid_control(control) -> bool:
    """A control read from a file: only inputs DOSBox's mapper understands."""
    def inputs_ok(inputs):
        return isinstance(inputs, list) and all(
            isinstance(i, str) and PAD_INPUT.fullmatch(i) for i in inputs)
    if not isinstance(control, dict) or not isinstance(control.get("id"), str):
        return False
    if "dirs" in control:
        dirs = control["dirs"]
        return (isinstance(dirs, dict) and set(dirs) <= set(PAD_DIRECTIONS)
                and all(inputs_ok(v) for v in dirs.values()))
    return inputs_ok(control.get("binds"))


def remembered_pad() -> dict | None:
    """The controller the Gamepad window saw last: {"name", "controls"}."""
    try:
        pad = json.loads(PAD_FILE.read_text(encoding="utf-8"))
        controls = [dict(c, label=str(c.get("label") or c["id"]))
                    for c in pad["controls"] if _valid_control(c)]
        name = str(pad.get("name") or "")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return {"name": name, "controls": controls} if controls else None


def remember_pad(name: str, controls: list) -> None:
    try:
        CONTROLLER_MAPS_DIR.mkdir(exist_ok=True)
        PAD_FILE.write_text(json.dumps({"name": name, "controls": controls}, indent=1),
                            encoding="utf-8")
    except OSError:
        pass


def saved_pad_actions(entry: dict) -> dict:
    """The control -> action choices saved for a game ({} if none)."""
    saved = entry.get("gamepad")
    actions = saved.get("actions") if isinstance(saved, dict) else None
    return dict(actions) if isinstance(actions, dict) else {}


def pad_action(saved: dict, control_id: str) -> str:
    """What a control does in a game: what was saved, else the Joystick preset."""
    return saved.get(control_id, PAD_PRESETS["Joystick"].get(control_id, "none"))


def gamepad_mapper(controls: list, actions: dict) -> tuple:
    """A complete DOSBox mapper file for a game's controller setup, and
    whether the game should see a joystick: (file text, uses_joystick)."""
    binds, order = {}, []
    for line in DOSBOX_DEFAULT_BINDS.splitlines():
        event = line.split(None, 1)[0]
        binds[event] = re.findall(r'"([^"]*)"', line)
        order.append(event)

    def add(event, inputs):
        if event not in binds:
            binds[event] = []
            order.append(event)
        # DOSBox calls the first two controllers it finds stick_0 and stick_1:
        # binding both lets either one play
        binds[event] += [f"stick_{n} {i}" for n in (0, 1) for i in inputs]

    uses_joystick = False
    for control in controls:
        action = actions.get(control["id"], "none")
        if "dirs" in control:
            events = PAD_DIR_EVENTS.get(action, {})
            for direction, inputs in control["dirs"].items():
                for event in events.get(direction, ()):
                    add(event, inputs)
        else:
            events = PAD_JOY_EVENTS.get(action) or (
                [action] if action in PAD_KEY_EVENTS else [])
            for event in events:
                add(event, control["binds"])
        if events and action in PAD_JOYSTICK_ACTIONS:
            uses_joystick = True

    lines = [event + "".join(f' "{b}"' for b in binds[event]) for event in order]
    return "\n".join(lines) + "\n", uses_joystick


def write_gamepad_mapper(entry: dict) -> tuple:
    """Write a game's DOSBox mapper file, for the controller the Gamepad
    window saw last (or a typical one): (path, uses_joystick)."""
    pad = remembered_pad()
    controls = pad["controls"] if pad else generic_pad_controls()
    saved = saved_pad_actions(entry)
    text, uses_joystick = gamepad_mapper(
        controls, {c["id"]: pad_action(saved, c["id"]) for c in controls})
    CONTROLLER_MAPS_DIR.mkdir(exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\s]+', "_", str(entry.get("id") or "game")).strip("._") or "game"
    path = CONTROLLER_MAPS_DIR / (safe + ".map")
    path.write_text(text, encoding="ascii")
    return path, uses_joystick


# ── Reading controllers (for the Gamepad window) ─────────────────────────────

class _SDLGUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_uint8 * 16)]


_SDL_FUNCTIONS = {
    "SDL_InitSubSystem":       (ctypes.c_int, [ctypes.c_uint32]),
    "SDL_Quit":                (None, []),
    "SDL_GetError":            (ctypes.c_char_p, []),
    "SDL_NumJoysticks":        (ctypes.c_int, []),
    "SDL_JoystickUpdate":      (None, []),
    "SDL_JoystickNameForIndex":        (ctypes.c_char_p, [ctypes.c_int]),
    "SDL_JoystickGetDeviceGUID":       (_SDLGUID, [ctypes.c_int]),
    "SDL_JoystickGetDeviceInstanceID": (ctypes.c_int32, [ctypes.c_int]),
    "SDL_JoystickOpen":        (ctypes.c_void_p, [ctypes.c_int]),
    "SDL_JoystickClose":       (None, [ctypes.c_void_p]),
    "SDL_JoystickGetAttached": (ctypes.c_int, [ctypes.c_void_p]),
    "SDL_JoystickNumAxes":     (ctypes.c_int, [ctypes.c_void_p]),
    "SDL_JoystickNumButtons":  (ctypes.c_int, [ctypes.c_void_p]),
    "SDL_JoystickNumHats":     (ctypes.c_int, [ctypes.c_void_p]),
    "SDL_JoystickGetAxis":     (ctypes.c_int16, [ctypes.c_void_p, ctypes.c_int]),
    "SDL_JoystickGetButton":   (ctypes.c_uint8, [ctypes.c_void_p, ctypes.c_int]),
    "SDL_JoystickGetHat":      (ctypes.c_uint8, [ctypes.c_void_p, ctypes.c_int]),
    "SDL_GameControllerMappingForDeviceIndex": (ctypes.c_void_p, [ctypes.c_int]),
    "SDL_GameControllerTypeForIndex":          (ctypes.c_int, [ctypes.c_int]),
    "SDL_free":                (None, [ctypes.c_void_p]),
}
_SDL_INIT_GAMECONTROLLER = 0x2000   # starts SDL's joystick support too


def dosbox_sdl(dosbox) -> Path | None:
    """DOSBox's SDL2.dll, in the folder dosbox.exe is in."""
    if not dosbox:
        return None
    exe = shutil.which(dosbox[0]) or dosbox[0]
    dll = Path(exe).resolve().parent / "SDL2.dll"
    return dll if dll.is_file() else None


class Controllers:
    """The connected game controllers, read through DOSBox's own SDL2.dll so
    their buttons are numbered as DOSBox numbers them. Tk thread only: SDL
    gets its Windows messages through Tk's message loop."""

    def __init__(self, dll: Path):
        sdl = ctypes.CDLL(str(dll))
        for name, (restype, argtypes) in _SDL_FUNCTIONS.items():
            function = getattr(sdl, name)
            function.restype, function.argtypes = restype, argtypes
        if sdl.SDL_InitSubSystem(_SDL_INIT_GAMECONTROLLER) != 0:
            error = (sdl.SDL_GetError() or b"").decode("utf-8", "replace")
            sdl.SDL_Quit()
            raise OSError(error or "SDL couldn't start")
        self.sdl   = sdl
        self.stick = None

    def connected(self) -> list:
        """[(SDL instance id, name)] of the connected controllers, in the
        order DOSBox numbers them."""
        sdl = self.sdl
        sdl.SDL_JoystickUpdate()      # also picks up controllers coming and going
        pads = []
        for index in range(sdl.SDL_NumJoysticks()):
            name = (sdl.SDL_JoystickNameForIndex(index) or b"").decode("utf-8", "replace")
            # SDL's id for the controller says which Windows API reads it
            # (Xbox controllers it reads directly are XInput ones too)
            api = chr(sdl.SDL_JoystickGetDeviceGUID(index).data[14])
            xbox = sdl.SDL_GameControllerTypeForIndex(index) in (1, 2)
            kind = "XInput" if api in "xrw" or xbox else "DirectInput"
            pads.append((sdl.SDL_JoystickGetDeviceInstanceID(index),
                         f"{name or 'Controller'} ({kind})"))
        return pads

    def open(self, instance_id: int) -> list | None:
        """Start reading a controller from connected(): its controls, or
        None if it's gone."""
        self.close()
        sdl = self.sdl
        index = next((i for i in range(sdl.SDL_NumJoysticks())
                      if sdl.SDL_JoystickGetDeviceInstanceID(i) == instance_id), None)
        stick = sdl.SDL_JoystickOpen(index) if index is not None else None
        if not stick:
            return None
        self.stick = stick
        mapping, text = sdl.SDL_GameControllerMappingForDeviceIndex(index), ""
        if mapping:
            text = ctypes.string_at(mapping).decode("utf-8", "replace")
            sdl.SDL_free(mapping)
        axes = max(sdl.SDL_JoystickNumAxes(stick), 0)
        return pad_controls(text, max(sdl.SDL_JoystickNumButtons(stick), 0), axes,
                            max(sdl.SDL_JoystickNumHats(stick), 0),
                            [sdl.SDL_JoystickGetAxis(stick, a) for a in range(axes)],
                            sdl.SDL_GameControllerTypeForIndex(index))

    def read(self) -> tuple | None:
        """(buttons, axes, hat) of the open controller, None once it's gone."""
        sdl, stick = self.sdl, self.stick
        if not stick:
            return None
        sdl.SDL_JoystickUpdate()
        if not sdl.SDL_JoystickGetAttached(stick):
            self.close()
            return None
        return ([sdl.SDL_JoystickGetButton(stick, b)
                 for b in range(sdl.SDL_JoystickNumButtons(stick))],
                [sdl.SDL_JoystickGetAxis(stick, a)
                 for a in range(sdl.SDL_JoystickNumAxes(stick))],
                sdl.SDL_JoystickGetHat(stick, 0) if sdl.SDL_JoystickNumHats(stick) > 0 else 0)

    def close(self):
        if self.stick:
            self.sdl.SDL_JoystickClose(self.stick)
            self.stick = None

    def quit(self):
        """Let go of the controllers: DOSBox needs DirectInput ones to itself."""
        self.close()
        self.sdl.SDL_Quit()


def pad_input_pressed(name: str, state: tuple, threshold: int = 25000) -> bool:
    """Whether an input ('button 3', 'axis 1 0', 'hat 0 4') is pressed in a
    Controllers.read() state. Axes count past DOSBox's own threshold."""
    buttons, axes, hat = state
    kind, *n = name.split()
    if kind == "button":
        return int(n[0]) < len(buttons) and bool(buttons[int(n[0])])
    if kind == "hat":
        return bool(hat & int(n[1]))
    axis = int(n[0])
    if axis >= len(axes):
        return False
    return axes[axis] > threshold if n[1] == "1" else axes[axis] < -threshold


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
        self.warned_conf = False

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

        ttk.Button(bar, text="💿  Add CD",
                   style="Pill.TButton",
                   command=self._add_cd).pack(side=tk.LEFT, padx=(0, 6))

        ttk.Button(bar, text="🗑  Remove",
                   style="Pill.TButton",
                   command=self._remove_selected).pack(side=tk.RIGHT)

        # Widen the window if the buttons need it (large fonts or display
        # scaling would otherwise cut off Remove)
        bar.update_idletasks()
        need = bar.winfo_reqwidth() + 2 * 14
        if need > 520:
            self.root.geometry(f"{need}x700")
            self.root.minsize(need, 600)

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

    def _add_to_library(self, entry: dict, same: str = "archive_name"):
        """Add a newly imported game. Importing the same archive (for Add CD,
        the same folder) again replaces its old entry, keeping the name and
        gamepad setup set for it, and ids stay unique so renames and settings
        reach the right game."""
        old = [e for e in self.library if e.get(same) == entry[same]]
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

    # ── CD Add Pipeline ───────────────────────────────────────────────────────

    def _add_cd(self):
        """Pick a pre-extracted game folder — scan for ISOs and exe on disc."""
        folder = filedialog.askdirectory(
            title="Select game folder",
            parent=self.root,
        )
        if not folder:
            return
        dest = Path(folder)
        stem = dest.name
        if any(e["extracted_path"] == str(dest) for e in self.library):
            if not messagebox.askyesno("Duplicate",
                    "'" + stem + "' is already in your library. Re-import it?",
                    parent=self.root):
                return
        threading.Thread(
            target=self._ingest_cd_thread,
            args=(dest, stem),
            daemon=True,
        ).start()

    def _ingest_cd_thread(self, dest: Path, stem: str):
        """Worker: find ISOs, scan first ISO for exe candidates, show picker."""
        display_name = stem.replace("-", " ").replace("_", " ").title()
        try:
            exo     = lookup_exodos(display_name)
            cd_isos = detect_cd_source(dest)
        except OSError as e:
            self._show_error_later("Add CD Failed", str(e))
            return

        if not cd_isos:
            self._show_error_later(
                "No Disc Images Found",
                "No ISO, BIN, CUE, or IMG files found in that folder.")
            return

        # Build base entry with ExoDOS settings if matched
        entry = {
            "id":             stem,
            "name":           exo["title"] if exo and "title" in exo else display_name,
            "archive_name":   stem,
            "extracted_path": str(dest),
            "exe_path":       "",
            "date_added":     str(date.today()),
            "cycles":         str(exo.get("cycles") or "auto") if exo else "auto",
            "memsize":        snap_memsize(int(exo["memsize"])) if exo and exo.get("memsize") else 16,
            "xms":            bool(exo.get("xms", True)) if exo else True,
            "ems":            bool(exo.get("ems", True)) if exo else True,
            "cd_isos":        cd_isos,
            "cd_mount":       True,
            "cd_exe":         str(exo.get("exe", "")) if exo else "",
        }

        # No ExoDOS exe — scan the first disc's data track for candidates
        # (every program on it if all of them look like installers or tools)
        exe_candidates = []
        if not entry["cd_exe"]:
            data_track = disc_data_file(cd_isos[0])
            exe_candidates = (scan_iso_for_exes(data_track)
                              or scan_iso_for_exes(data_track, skip_blacklisted=False))
            if not exe_candidates:
                self._show_error_later(
                    "No Exe Found in Disc",
                    "Could not detect a game executable inside the disc image.")
                return

        def finish():
            self._add_to_library(entry, same="extracted_path")
            if entry["cd_exe"]:
                # ExoDOS gave us the exe, use it directly
                self._show_disc_tip(entry)
                self._launch_entry(entry)
            else:
                # Show ISO exe picker so user selects which exe to run
                IsoExePickerModal(self.root, exe_candidates, entry,
                                  on_confirm=self._on_iso_exe_picked)
        self.root.after(0, finish)

    def _on_iso_exe_picked(self, entry: dict, cd_exe: str):
        """Called when user picks an exe from the ISO picker."""
        entry["cd_exe"] = cd_exe
        for i, e in enumerate(self.library):
            if e["id"] == entry["id"]:
                self.library[i] = entry
                break
        self._save_library()
        self._show_disc_tip(entry)
        RenameModal(self.root, entry, on_save=self._on_rename_saved,
                    then=lambda: self._launch_entry(entry))

    def _show_disc_tip(self, entry: dict):
        """Show multi-disc tip if game has more than one ISO."""
        cd_isos = entry.get("cd_isos", [])
        if len(cd_isos) > 1:
            names = "\n".join("  Disc " + str(i+1) + ": " + Path(cd_isos[i]).name
                               for i in range(len(cd_isos)))
            msg = (entry["name"] + " has " + str(len(cd_isos)) + " discs:\n" +
                   names + "\n\nDisc 1 will be mounted as D: on launch.\n"
                   "Press Ctrl+F4 in DOSBox to swap discs.")
            messagebox.showinfo("Multi-Disc Game", msg, parent=self.root)

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
        Case 2 — CD_ONLY: imgmount all ISOs as D:, boot from D:, run cd_exe
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
        # dosbox.conf beside AutoDOS is DOSBox's only config: it skips its own
        # dosbox-staging.conf (and doesn't create one) and any dosbox.conf in
        # the folder it's started from
        conf_args = ["--noprimaryconf", "--nolocalconf"]
        if DOSBOX_CONF.is_file():
            conf_args += ["-conf", str(DOSBOX_CONF)]
        elif not self.warned_conf:
            self.warned_conf = True
            messagebox.showwarning(
                "dosbox.conf Not Found",
                "dosbox.conf isn't in the AutoDOS folder, so games will use "
                "DOSBox's default settings.\n\nPut dosbox.conf back here:\n"
                + str(BASE_DIR),
                parent=self.root)
        base_args = (conf_args
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
                    "Remove and re-add via Add CD to scan the disc.",
                    parent=self.root)
                return
            c_path  = str(extracted)
            mount_c = f'mount c "{c_path}"' if " " in c_path else f"mount c {c_path}"
            # The exe can be in a folder on the disc (e.g. GAME\PLAY.EXE)
            exe_folder, _, exe_name = cd_exe.replace("/", "\\").rpartition("\\")
            run_cmd = f'"{exe_name}"' if " " in exe_name else exe_name
            cmd = [binary] + prefix + base_args
            cmd += [
                "-c", mount_c,
                "-c", make_imgmount(cd_isos),
                "-c", "D:",
            ]
            if exe_folder:
                cmd += ["-c", f'cd "\\{exe_folder}"' if " " in exe_folder else f"cd \\{exe_folder}"]
            cmd += [
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
        GamepadModal(self.root, entry, self.dosbox, on_save=self._on_gamepad_saved)

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


# ── ISO Exe Picker Modal ─────────────────────────────────────────────────────

class IsoExePickerModal:
    """Pick which exe inside the ISO to launch the game with."""

    def __init__(self, parent, exe_candidates: list, entry: dict, on_confirm):
        self.exe_candidates = exe_candidates
        self.entry          = entry
        self.on_confirm     = on_confirm

        self.win = tk.Toplevel(parent)
        self.win.title("Select Game Executable")
        self.win.configure(bg=BG)
        self.win.geometry("420x300")
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.lift()
        self.win.focus_force()
        dark_title_bar(self.win)

        tk.Label(self.win,
                 text="Executables found on disc — select the one that runs the game:",
                 bg=BG, fg=TEXT, font=("TkDefaultFont", 11),
                 wraplength=380, justify=tk.LEFT
                 ).pack(anchor="w", padx=16, pady=(16, 8))

        frame = tk.Frame(self.win, bg=BORDER, bd=1)
        frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        self.lb = tk.Listbox(
            frame, bg=LIST_BG, fg=TEXT,
            selectbackground=SEL_BG, selectforeground=TEXT,
            activestyle="none", relief="flat", bd=0,
            font=("TkDefaultFont", 12), highlightthickness=0,
            exportselection=False, height=6)   # short enough for the buttons to fit
        self.lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=1, pady=1)
        self.lb.bind("<Double-Button-1>", lambda e: self._confirm())
        self.lb.bind("<Return>",          lambda e: self._confirm())

        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lb.configure(yscrollcommand=sb.set)

        for name in exe_candidates:
            self.lb.insert(tk.END, f"  {name}")
        self.lb.selection_set(0)
        self.lb.focus_set()

        btn_frame = tk.Frame(self.win, bg=BG)
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 14))

        ttk.Button(btn_frame, text="▶  Launch", style="Pill.TButton",
                   command=self._confirm).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Cancel", style="Pill.TButton",
                   command=self.win.destroy).pack(side=tk.LEFT)

    def _confirm(self):
        sel = self.lb.curselection()
        if not sel:
            return
        chosen = self.exe_candidates[sel[0]]
        self.win.destroy()
        self.on_confirm(self.entry, chosen)


# ── Gamepad Modal ────────────────────────────────────────────────────────────

class GamepadModal:
    """Per-game controller setup: what each control on a DirectInput or XInput
    controller does in the game, with a live test of the controller."""

    def __init__(self, parent, entry: dict, dosbox, on_save):
        self.entry     = entry
        self.dosbox    = dosbox
        self.on_save   = on_save
        self.saved     = saved_pad_actions(entry)
        self.choices   = {}      # control id -> action picked in this window
        self.pads      = None    # Controllers, once SDL has started
        self.connected = []      # [(SDL instance id, name)]
        self.current   = None    # instance id of the controller being read
        self.lost      = False   # the controller being read went away
        self.rows      = {}      # control id -> (light, StringVar, control)
        self.lit       = {}      # control id -> what its light shows now
        self.ticks     = 0
        self.after_id  = None

        self.win = tk.Toplevel(parent)
        self.win.title("Gamepad — " + entry["name"])
        self.win.configure(bg=BG)
        self.win.geometry("480x660")
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.lift()
        self.win.focus_force()
        dark_title_bar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        self.heading = tk.Label(self.win, text="Looking for controllers…",
                                bg=BG, fg=TEXT, font=("TkDefaultFont", 11, "bold"),
                                anchor="w")
        self.heading.pack(fill=tk.X, padx=16, pady=(14, 0))
        # Shown when more than one controller is connected
        self.pad_var = tk.StringVar()
        self.pad_box = ttk.Combobox(self.win, textvariable=self.pad_var,
                                    state="readonly", font=("TkDefaultFont", 10))
        self.pad_box.bind("<<ComboboxSelected>>", self._on_pick)
        tk.Label(self.win,
                 text="Press a button on the controller and its row lights up. "
                      "Joystick works in games with joystick support; pick keys "
                      "for games that only use the keyboard.",
                 bg=BG, fg=MUTED, font=("TkDefaultFont", 9), justify=tk.LEFT,
                 wraplength=440).pack(anchor="w", padx=16, pady=(2, 10))

        presets = tk.Frame(self.win, bg=BG)
        presets.pack(fill=tk.X, padx=16, pady=(0, 10))
        tk.Label(presets, text="Preset:", bg=BG, fg=TEXT,
                 font=("TkDefaultFont", 10)).pack(side=tk.LEFT, padx=(0, 8))
        for name in PAD_PRESETS:
            ttk.Button(presets, text=name, style="Pill.TButton",
                       command=lambda n=name: self._apply(PAD_PRESETS[n])
                       ).pack(side=tk.LEFT, padx=(0, 6))

        # The controls, in a table that scrolls
        frame = tk.Frame(self.win, bg=BORDER)
        frame.pack(fill=tk.BOTH, expand=True, padx=16)
        self.canvas = tk.Canvas(frame, bg=LIST_BG, highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 1), pady=1)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(1, 0), pady=1)
        self.table = tk.Frame(self.canvas, bg=LIST_BG)
        table_id = self.canvas.create_window(0, 0, window=self.table, anchor="nw")
        self.table.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            table_id, width=e.width))
        self.canvas.bind("<MouseWheel>", self._on_wheel)

        self.status = tk.Label(self.win, text="", bg=BG, fg=MUTED,
                               font=("TkDefaultFont", 9), justify=tk.LEFT,
                               wraplength=440)
        self.status.pack(anchor="w", padx=16, pady=(10, 0))

        buttons = tk.Frame(self.win, bg=BG)
        buttons.pack(fill=tk.X, padx=16, pady=(12, 14))
        ttk.Button(buttons, text="💾  Save", style="Pill.TButton",
                   command=self._save).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text="Cancel", style="Pill.TButton",
                   command=self._close).pack(side=tk.LEFT)

        # Until a controller turns up: the one seen last, or a typical one
        self.last = remembered_pad()
        self._show(self.last["controls"] if self.last else generic_pad_controls())
        # Starting SDL can take a moment, so the window goes up first
        self.after_id = self.win.after(100, self._start)

    # ── Rows ──────────────────────────────────────────────────────────────

    @staticmethod
    def _choices(control: dict) -> list:
        """(action, name shown) choices for one control."""
        if "dirs" in control:
            return PAD_DIR_CHOICES
        return (PAD_JOY_BUTTONS + [("none", "Not used")]
                + [(event, name) for name, event in PAD_KEYS])

    def _name(self, control: dict, action: str) -> str:
        return dict(self._choices(control)).get(action, "Not used")

    def _action(self, control_id: str) -> str:
        if control_id in self.choices:
            return self.choices[control_id]
        return pad_action(self.saved, control_id)

    def _show(self, controls: list):
        """Fill the table with a controller's controls."""
        for child in self.table.winfo_children():
            child.destroy()
        self.rows, self.lit = {}, {}
        for i, control in enumerate(controls):
            bg = LIST_BG if i % 2 == 0 else ALT_ROW_BG
            row = tk.Frame(self.table, bg=bg)
            row.pack(fill=tk.X)
            light = tk.Label(row, text="●", bg=bg, fg=BORDER, width=3,
                             font=("TkDefaultFont", 11))
            light.pack(side=tk.LEFT, padx=(6, 2))
            label = tk.Label(row, text=control["label"], bg=bg, fg=TEXT,
                             font=("TkDefaultFont", 11), anchor="w")
            label.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=5)
            var = tk.StringVar(value=self._name(control, self._action(control["id"])))
            box = ttk.Combobox(row, textvariable=var, state="readonly", width=22,
                               height=14, values=[n for _, n in self._choices(control)],
                               font=("TkDefaultFont", 10))
            box.pack(side=tk.RIGHT, padx=8, pady=4)
            box.bind("<<ComboboxSelected>>",
                     lambda e, c=control, v=var: self._picked(c, v))
            for widget in (row, light, label, box):
                widget.bind("<MouseWheel>", self._on_wheel)
            self.rows[control["id"]] = (light, var, control)
        self.canvas.yview_moveto(0)

    def _picked(self, control: dict, var: tk.StringVar):
        actions = {name: action for action, name in self._choices(control)}
        self.choices[control["id"]] = actions.get(var.get(), "none")

    def _apply(self, preset: dict):
        for control_id, (_light, var, control) in self.rows.items():
            self.choices[control_id] = preset.get(control_id, "none")
            var.set(self._name(control, self.choices[control_id]))

    def _on_wheel(self, event):
        """Scroll the table (and not the dropdown under the mouse)."""
        self.canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1),
                                 "units")
        return "break"

    # ── Controllers ───────────────────────────────────────────────────────

    def _start(self):
        """Start reading controllers through DOSBox's SDL2.dll."""
        self.after_id = None
        try:
            self.win.grab_set()   # no starting games while this holds the controller
        except tk.TclError:
            pass
        dll = dosbox_sdl(self.dosbox)
        try:
            if not dll:
                raise OSError("DOSBox's SDL2.dll wasn't found")
            self.pads = Controllers(dll)
        except (OSError, AttributeError) as e:
            self.heading.config(text="Can't read controllers")
            self.status.config(text=f"Can't read controllers here ({e}). "
                                    "You can still set up the buttons below.")
            return
        self._poll()

    def _poll(self):
        """Keep up with controllers coming and going, and light up the
        controls being pressed."""
        state = self.pads.read() if self.current is not None else None
        if self.current is not None and state is None:
            self.current, self.lost = None, True   # switched off, asleep or unplugged
            self.ticks = 0
        if self.ticks % (20 if self.current is not None else 10) == 0:
            self._update_list()
        self.ticks += 1
        self._light(state)
        self.after_id = self.win.after(50, self._poll)

    def _update_list(self):
        connected = self.pads.connected()
        if connected != self.connected:
            self.connected = connected
            self.pad_box.config(values=[name for _, name in connected])
            if len(connected) > 1:
                self.pad_box.pack(fill=tk.X, padx=16, pady=(6, 0), after=self.heading)
            else:
                self.pad_box.pack_forget()
            if self.current is not None:
                self._title()
        if self.current is None:
            if connected:
                self._use(connected[0][0])
            else:
                self._none_found()

    def _on_pick(self, _event=None):
        name = self.pad_var.get()
        self._use(next((i for i, n in self.connected if n == name), None))

    def _use(self, instance_id):
        """Read and show one of the connected controllers."""
        controls = self.pads.open(instance_id) if instance_id is not None else None
        if controls is None:
            return
        name = dict(self.connected).get(instance_id, "Controller")
        self.current, self.lost = instance_id, False
        self.pad_var.set(name)
        self._title()
        self.status.config(
            text="Press its buttons to test them: the matching row lights up. "
                 "Switch it on before you start a game, as DOSBox only looks for "
                 "controllers when a game starts.")
        # Games go by the controller seen last to know which button is which
        remember_pad(name, controls)
        self._show(controls)

    def _title(self):
        """Heading: the controller's name, or how many there are to pick from."""
        if len(self.connected) > 1:
            self.heading.config(text=f"{len(self.connected)} controllers found. Pick one to set up:")
        else:
            self.heading.config(text=dict(self.connected).get(self.current, "Controller"))

    def _none_found(self):
        self.heading.config(text="No controller found")
        text = ("The controller was switched off or went to sleep. " if self.lost else "")
        text += "Switch it on (or plug in its receiver) to test its buttons here."
        if self.last:
            text += f" Showing the buttons of {self.last['name'] or 'the controller seen last'}."
        self.status.config(text=text)

    def _light(self, state):
        """Light up the rows of the controls being pressed (arrows for a
        D-pad or stick)."""
        for control_id, (light, _var, control) in self.rows.items():
            if state is None:
                shown = ("●", BORDER)
            elif "dirs" in control:
                arrows = "".join(
                    arrow for direction, arrow in zip(PAD_DIRECTIONS, "↑↓←→")
                    if any(pad_input_pressed(i, state, 16000)
                           for i in control["dirs"].get(direction, ())))
                shown = (arrows, ACCENT) if arrows else ("●", BORDER)
            else:
                pressed = any(pad_input_pressed(i, state) for i in control["binds"])
                shown = ("●", ACCENT if pressed else BORDER)
            if self.lit.get(control_id) != shown:
                self.lit[control_id] = shown
                light.config(text=shown[0], fg=shown[1])

    # ── Save / close ──────────────────────────────────────────────────────

    def _save(self):
        actions = dict(self.saved)
        actions.update({control_id: self._action(control_id) for control_id in self.rows})
        actions.update(self.choices)
        self.on_save(self.entry, {"actions": actions})
        self._close()

    def _close(self):
        if self.after_id:
            self.win.after_cancel(self.after_id)
            self.after_id = None
        if self.pads:
            self.pads.quit()      # DOSBox needs DirectInput controllers to itself
            self.pads = None
        self.win.destroy()


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    """Launch the AutoDOS application."""
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
