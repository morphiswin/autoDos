# What's changed in this version

This is a modified version of [AutoDOS](https://github.com/makuka97/autoDos) by
makuka97, built on the `windows-edition` branch (commit `d4e2fc6`). Everything
changed since then is listed below, grouped by what it affects. The commits are
listed at the end.

All of it was tested under Wine on Linux with the bundled DOSBox Staging 0.82.2.
I had no real Windows PC or physical controller to test with, so the controller
support was tested with simulated controllers.

---

## One DOSBox config: `dosbox.conf` next to `AutoDOS.exe`

- **DOSBox used to read three config files**, which is why there seemed to be
  three:
  - its own `%LOCALAPPDATA%\DOSBox\dosbox-staging.conf`, which it creates on
    first run;
  - any `dosbox.conf` in the folder it was started from (the AutoDOS folder);
  - the copy built into `AutoDOS.exe`, unpacked into
    `%LOCALAPPDATA%\Temp\_MEI…` on every launch, which overrode the other two.
- **Now `dosbox.conf` in the AutoDOS folder is the only one.** AutoDOS starts
  DOSBox with `--noprimaryconf --nolocalconf -conf <AutoDOS folder>\dosbox.conf`,
  so DOSBox neither reads nor creates its own config. The exe no longer
  carries a copy.
- **Edits take effect** the next time a game starts. There's no rebuild.
- **Per-game settings still apply on top** of the file: cycles, memory,
  EMS/XMS and the gamepad mapper file.
- **If `dosbox.conf` is missing**, games start with DOSBox's defaults and
  AutoDOS says so once.

## Display settings (in `dosbox.conf`)

- **Your DOSBox display settings are no longer forced to OpenGL.** The copy of
  `dosbox.conf` built into the exe said `output = opengl` and overrode your own
  settings on every launch. The file now uses texture output with the
  Direct3D renderer, and it's the only one (see above).
- **Tuned for a 19" 4:3 LCD at 1600x1200, 60 Hz:**
  - **Fullscreen** at the desktop resolution, so the monitor never switches
    modes.
  - **Alt+Enter window** of 1280x960 (exactly 4x 320x240).
  - **Crisp pixels:** nearest-neighbour scaling (`output = texturenb`), so a
    320x200 game scales by exactly 5x6 into even, sharp blocks instead of a
    bilinear blur.
  - **4:3 aspect correction on, integer scaling off** (`viewport = fit`), so
    games fill the screen without black borders. This also replaces the
    invalid `integer_scaling = false`.
  - **`vsync = auto`:** forcing vsync on makes 70 Hz VGA games drop frames on a
    60 Hz panel.
  - **Faster start:** higher process priority while DOSBox is in front, and no
    DOSBox welcome banner.
- The file is commented, so it can be adjusted for other monitors in Notepad.

## Adding games from archives (Add Zip)

- **Each game's folder is named after the folder inside the archive**, not the
  archive. An ExoDOS archive like `Syndicate Plus (1994).7z` holds one folder
  named with the game's ExoDOS id (`Syndicat`). That folder now becomes
  `games\Syndicat`, and the game is looked up in the ExoDOS database by that
  name. Archives with more than one thing at the top still go to
  `games\<archive name>`.
- **Imports are safer:**
  - Archives are extracted into a temporary folder that is always cleaned up.
  - The archive is checked for a launcher before anything is moved into
    `games\`.
  - A folder name another game already uses gets a ` (2)` suffix.
  - The move retries while a virus scanner is holding the new files.
- **Importing the same archive again replaces its library entry** instead of
  adding a duplicate. It keeps the game's name and gamepad setup, and replaces
  files over the existing folder so save games survive. Every entry gets a
  unique id, so renames and settings always reach the right game.
- **Full names are kept:** `Dr. Doom (1990)` no longer becomes `Dr`. Archive
  names with spaces are used as-is for the game's name.
- **`.COM` launchers are detected** (`SIERRA.COM`, `DIGGER.COM`…), so games
  with only a `.COM` can be imported. A `.COM` ranks below an `.EXE` beside it
  unless it's named after the game. DOS utilities like `MOUSE.COM` or
  `ASKECHO.COM` rank lower still.
- **Read-only files** (copied off CDs) no longer stop a re-import or Remove
  part-way.
- **7-Zip:** its output is read leniently (an accented letter in a path could
  fail the import), and it runs without flashing a console window.
- **Errors are reported:** the extraction-failure popup used to crash instead
  of showing, and other import errors failed silently. Both now show a message.
- **Change EXE** keeps the game where it is in the list and ranks programs the
  same way the import did.

## CD games

- **Disc images are mounted as `D:` for games added from an archive.** ExoDOS
  CD games such as Syndicate Plus keep their discs in a `CD` folder and expect
  them on `D:`; before, only Add CD games got a `D:` drive, so `run.bat`'s `d:`
  failed and the game couldn't start.
- **Formats:** ISO, CUE+BIN and CUE+ISO, with CD audio tracks (including
  FLAC/OPUS/OGG/MP3/WAV). DOSBox Staging can't mount CHD, MDF/MDS or CCD.
- **Several discs** mount as a set; switch discs with Ctrl+F4. Discs are sorted
  naturally ("Disc 2" before "Disc 10").
- **Finding the disc folder:**
  - The search runs from the game program's folder up to the game folder, so
    games imported with the older folder layout get `D:` too.
  - Files a `.cue` already loads are left to it, so a CUE+BIN pair is one
    disc.
  - Loose `.bin`/`.img` files outside the CD folder, and floppy `disk`
    folders, are ignored.
- **Cue sheets** are read robustly (byte order marks, `..` paths, unreadable
  sheets). DOSBox can't read a cue sheet saved with a byte order mark, so for
  those the data track is mounted instead.

## Per-game DOSBox settings

- **Memory, EMS and XMS are applied.** Memory used to be sent as
  `set memsize=N` in the DOS shell, which only sets an environment variable, so
  every game ran with 64 MB. EMS and XMS were never passed at all. All three
  are now set when DOSBox starts. This is what fixed Syndicate closing straight
  after its menu.
- **Every ExoDOS cycles format works:**
  - `auto` runs real-mode games at 3000 cycles and protected-mode games at max,
    as DOSBox and ExoDOS do.
  - `max`, `N` and `fixed N` set DOSBox's real and protected-mode cycles.
  - `max limit N` and `auto limit N` are passed to DOSBox unchanged.
  - Before, `fixed N` and `auto limit N` were rejected and fell back to 3000
    cycles.
- Games without an ExoDOS match now default to `auto` instead of 3000 cycles.

## Add CD

- **Add CD is on the toolbar** between Add Zip and Remove. (It was briefly
  removed in an earlier change and has been restored.)
- **The disc scan now works.** It looks inside the disc image for the program
  that starts the game. It used a library (`pycdlib`) the official exe didn't
  include, so it always came back empty. AutoDOS now reads ISO images and BIN
  data tracks (raw 2352-byte CD sectors, mode 1 and 2) itself.
- **Programs in a folder on the disc run** (e.g. `GAME\PLAY.EXE`).
- **If every program on the disc looks like an installer or tool**, all of
  them are listed. If the disc has none, AutoDOS says so instead of adding a
  game that can't start.
- **Adding the same folder again** replaces its entry instead of duplicating
  it.
- **The picker** uses the dark theme and its buttons always fit.

## Controllers (right-click a game → Gamepad)

- **The old Controller button is gone.** It only read Xbox-style (XInput) pads,
  and the mapper file it gave DOSBox contained only the pad's bindings, which
  left the keyboard dead in the game.
- **A per-game Gamepad window works with any DirectInput or XInput
  controller**, wired or wireless:
  - **Same numbering as DOSBox:** controllers are read through DOSBox's own
    `SDL2.dll`, so every button, axis and hat is numbered exactly as DOSBox
    numbers it. XInput and DirectInput pads number their buttons differently,
    so this matters.
  - **Real button names** from SDL's controller database: A/B/X/Y, LB/RB/LT/RT
    on Xbox pads; Cross/Circle/Square/Triangle, L1/R1/L2/R2 on PlayStation
    pads; and so on. A pad SDL doesn't know shows `Button 1`, `Button 2`…, and
    an axis resting at one end is shown as a trigger.
  - **Live test:** each row lights up while you press it, and D-pads and sticks
    show arrows for the direction.
  - **What a D-pad or stick can do:** Joystick, Joystick axes 3 + 4 (rudder or
    throttle in flight sims), Arrow keys, W A S D, Numpad 8 4 6 2, or Not used.
  - **What a button can do:** Joystick button 1–4, any keyboard key, the left,
    right or middle mouse button, or Not used.
  - **Presets:**
    - *Joystick* (the default): D-pad and left stick are the PC joystick,
      A/B/X/Y are joystick buttons 1–4, Start is Esc.
    - *Keyboard*: arrows; A = Ctrl, B = Space, X = Alt, Y = Enter, LB = Shift,
      RB = Tab, LT/RT = comma/period, Start = Esc.
  - **Wireless pads:** the window notices a controller switching on, off or
    going to sleep, and lets you pick one when several are connected.
  - **AutoDOS remembers the last controller the window found**
    (`controller_maps\controller.json`), and every game uses its layout. Each
    game saves only what each control does, so settings carry over to a new
    pad.
- **Every game starts with a complete DOSBox mapper file**
  (`controller_maps\<game>.map`): DOSBox's own keyboard and hotkey bindings
  plus the controller's.
  - Both of the first two controllers DOSBox finds are bound, so either one
    plays.
  - Joystick buttons 3–4 and axes 3–4 work whether DOSBox finds one controller
    or two.
  - When no control is set to Joystick, DOS doesn't see a joystick at all.
- **Switch the controller on before starting a game.** DOSBox Staging 0.82
  only looks for controllers when a game starts and doesn't reconnect one that
  drops out mid-game.

## Look and feel

- **Dark theme throughout:** main window, list, search box, dialogs, right-click
  menu, dropdowns, scrollbars and checkboxes, with a dark title bar on
  Windows 10/11.
- **Right-click menu:** Rename, Game Settings, Change EXE, Gamepad and Remove.
- **The main window widens** if the toolbar buttons need more room (large fonts
  or display scaling).

## Building

Build the same way as before. You'll need:

- Python 3.12 with Pillow and PyInstaller; run
  `python -m PyInstaller AutoDOS.spec`.
- Next to `AutoDOS.exe`:
  - `dosbox.conf`: the DOSBox settings.
  - `dosbox\`: DOSBox Staging 0.82.2, including its `SDL2.dll`, which the
    Gamepad window uses.
  - `tools\`: holds `7za.exe`.

---

## Commits

| Commit | Change |
| --- | --- |
| `bb328a8` | Use texture output with the Direct3D renderer in the bundled dosbox.conf |
| `63b75ba` | Tune dosbox.conf for fullscreen on a 4:3 1600x1200 screen |
| `46b51bb` | Retune dosbox.conf for a 60 Hz 4:3 1600x1200 LCD |
| `a7f598c` | Extract archives into a folder named after their top-level folder |
| `01e852e` | Mount CD images as D: for games added from an archive |
| `ae35116` | Apply per-game memory, EMS/XMS and all ExoDOS cycles formats |
| `f0275d9` | Review fixes for archive import, disc mounting and per-game settings |
| `768c33e` | Dark theme; remove the old XInput-only controller setup (and, briefly, Add CD) in favour of an interim SideWinder-only gamepad setup |
| `77f1b26` | Gamepad setup for any DirectInput or XInput controller; bring back Add CD |
| `fbaedaa` | Use one external dosbox.conf beside AutoDOS.exe |
