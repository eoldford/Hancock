# Hancock

Keeps a Windows or macOS machine awake and, as a visual, moves the mouse cursor
through a little game — cycling between tic-tac-toe, Connect Four, Pong,
and Hangman playing themselves. Pass `--spell <text>` to trace that text
in cursive instead. Runs until you cancel it (Ctrl+C) or it hits its own
timeout.

### Why "Hancock"?

A "John Hancock" is American slang for a signature. This tool keeps your
mouse busy to avoid your system from going asleep while long-running
agents are working.

## Why this exists

On a managed laptop where you can't touch the power settings, the obvious
fix — a "mouse jiggler" that just calls `SetCursorPos` in a loop — turned
out not to be reliable: moving the cursor alone isn't consistently
recognized by Windows' sleep-idle tracking, and can be ignored outright on
devices with mouse-jiggler detection enabled.

`Hancock.py` instead calls `SetThreadExecutionState` with
`ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED` — the actual Windows API apps
like video players and installers use to say "don't sleep, I need this."
On macOS the equivalent is `caffeinate -d -i`, which Hancock runs as a
child process for as long as it's running. The cursor tracing is purely
cosmetic on either OS; it isn't what prevents sleep.

Note: this cannot and does not try to override a hard **lock-screen**
policy enforced by IT via Group Policy. If your device locks the session
on a fixed timer regardless of activity, that's a security control, not a
power setting, and no user-mode script should (or can) bypass it.

## Requirements

- Windows or macOS
- Python 3.9 or newer (see [Installing Python](#installing-python) if you
  don't have it)
- [`hershey-fonts`](https://pypi.org/project/hershey-fonts/) — renders the
  cursive letter shapes at startup for whatever text you ask for
- [`pynput`](https://pypi.org/project/pynput/) — **macOS only**, for cursor
  control and keystroke detection

Install everything for your platform with:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Upgrading pip first matters: older pip versions can miss the prebuilt
packages and try to compile them from source instead, which usually fails.
(On macOS, use `python3` in place of `python` if `python` isn't found.)

**Windows:** no other dependencies. Mouse/keyboard control and sleep
prevention use `ctypes` calls into `user32.dll` / `kernel32.dll` directly.

**macOS:** sleep prevention uses the built-in `caffeinate` command, and
screen size comes from CoreGraphics via `ctypes`. The first time you run
it, grant the terminal app you launch it from (Terminal, iTerm, VS Code,
etc.) both of these in **System Settings → Privacy & Security**:

- **Accessibility** — needed to move the cursor
- **Input Monitoring** — needed to notice when you start typing

Restart the terminal app after granting them.

### Installing Python

Check whether you already have it:

```bash
python --version     # Windows
python3 --version    # macOS
```

If that prints 3.9 or newer, you're set. Otherwise:

- **Windows:** `winget install Python.Python.3.13`, or download the
  installer from [python.org](https://www.python.org/downloads/) and tick
  **"Add python.exe to PATH"** on the first screen. Open a new terminal
  afterwards.
- **macOS:** `brew install python` if you use
  [Homebrew](https://brew.sh/), or download the installer from
  [python.org](https://www.python.org/downloads/).

Optionally, keep Hancock's packages separate from the rest of your system
in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate      # macOS
.venv\Scripts\activate         # Windows
```

## Usage

```bash
python Hancock.py
```

Defaults to cycling through tic-tac-toe, Connect Four, Pong, and Hangman,
each playing out a scripted game against itself. Options:

| Flag | Meaning | Default |
|---|---|---|
| `-s`, `--spell` | Text to trace in cursive instead of playing games | `None` (plays games) |
| `-r`, `--resume-delay` | Seconds of no mouse movement or keystrokes before resuming after you interrupt it | `3.0` |
| `-t`, `--timeout` | Stop automatically after this many hours | `2.0` |
| `--version` | Print the version and exit | |

Examples:

```bash
python Hancock.py
python Hancock.py -s "Evan P Oldford"
python Hancock.py -s "Evan P Oldford" -r 5
python Hancock.py -t 12
```

## Behavior

- **Won't fight you for the mouse.** If you move the mouse or start
  typing, tracing pauses immediately. It waits until you've been fully
  idle (no mouse movement, no keystrokes — checked via polled
  `GetAsyncKeyState` on Windows, or a `pynput` listener on macOS) for `--resume-delay` seconds,
  then glides smoothly from wherever you left the cursor back onto the
  path at the exact point it paused, rather than jumping or resuming from
  the start.
- **Randomized placement.** Each full pass picks a new random on-screen
  position (still fully within screen bounds), so the trace doesn't sit
  over the same pixels for hours on end.
- **Self-limiting.** Stops on its own after `--timeout` hours, or
  immediately on Ctrl+C. Either way it releases the sleep-prevention
  request on exit.

## How the games are built

Each game (`build_tic_tac_toe_path()`, `build_connect_four_path()`,
`build_pong_path()`, `build_hangman_path()`) picks a scripted game at
random from a fixed set and renders its board/moves as strokes — the grid
or court lines, then the pieces, moves, or ball path in order, plus a
winning line or hanged figure where applicable. Hangman's revealed-word
outcome reuses the cursive font path below, rescaled into the board.

All strokes, from games or cursive text alike, are passed through
`build_continuous_path()`, which bridges the pen-lifts between them with
straight connectors so the cursor traces one continuous path instead of
teleporting.

## How the cursive shapes are built

Letters come from the Hershey "cursive" font (a single-stroke font
designed for pen plotters, which maps naturally onto cursor movement).
`build_name_path()` renders the requested text into strokes.

A `tmp/` folder alongside this file holds the throwaway scripts used while
prototyping (font/shape comparisons, path preview renders, etc.). It's not
tracked in this repo and isn't needed to run `Hancock.py` — it's local
scratch work, kept only as a personal record of how the approach was
chosen.
