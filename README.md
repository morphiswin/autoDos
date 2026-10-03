# AutoDOS — Windows Edition

**Add a DOS game, double-click, play.** AutoDOS is a small launcher for DOS games
on Windows. It unpacks your ZIP or 7Z archives, works out how to start each
game, and launches it in DOSBox Staging with the settings eXoDOS recommends.

> This is a fork of [makuka97/autoDos](https://github.com/makuka97/autoDos).
> The changes are below, and every detail is in [CHANGES.md](CHANGES.md).

## What's new in this fork

- 🎮 **Any controller.** DirectInput or XInput, wired or wireless. Right-click
  a game → **Gamepad** to map each button to the joystick or a key, and test it
  live.
- 📦 **eXoDOS archives just work.** A game installs into its proper eXoDOS
  folder (e.g. `games\Syndicat`), so it matches the right settings.
- 💿 **CDs mount themselves.** ISO and CUE/BIN images in a game's CD folder
  appear as `D:`.
- ⚙️ **Settings that stick.** Per-game memory, EMS, XMS and every eXoDOS
  cycles format now reach DOSBox.
- 🔍 **Add CD finds the game.** It reads ISO and BIN discs to find the program
  that starts it.
- 🖥️ **Crisp fullscreen.** Direct3D output, sharp pixels and 4:3 aspect,
  tuned for a 1600x1200 screen.
- 🌙 **Dark theme** everywhere, plus safer imports and plenty of fixes.

## Using it

1. Put `AutoDOS.exe` next to the `dosbox\` folder (DOSBox Staging 0.82.2) and
   the `tools\` folder (`7za.exe`).
2. Click **Add Zip** and pick a game archive. It unpacks, sets itself up and
   launches.
3. Double-click a game to play it. Right-click it for Game Settings, Gamepad,
   Change EXE, Rename or Remove.

**Tip:** switch your controller on *before* starting a game. DOSBox only looks
for controllers when a game starts.

## Features

- Picks each game's program automatically, and asks when it isn't sure
- Built-in eXoDOS database of 7,600+ games for cycles, memory, EMS and XMS
- Tweak any game's settings, or search the database and apply another entry
- Multi-disc games: press Ctrl+F4 in DOSBox to swap discs
- Searchable library, saved in `library.json` next to the exe
- One standalone exe, no Python needed

## Building

```
python -m PyInstaller AutoDOS.spec
```

You need Python 3.12 with Pillow and PyInstaller. The display settings in
`dosbox.conf` are built into the exe, so rebuild after changing them.

## Credits

The original AutoDOS is by [makuka97](https://github.com/makuka97). Game
settings come from the [eXoDOS](https://www.retro-exo.com/exodos.html)
collection. Emulation is by
[DOSBox Staging](https://dosbox-staging.github.io).
