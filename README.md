# Hancock

Keeps a Windows machine awake and, as a visual, slowly traces a chosen
phrase in cursive with the mouse cursor. Runs until you cancel it (Ctrl+C)
or it hits its own timeout.

## Why this exists

On a managed laptop where you can't touch the power settings, the obvious
fix — a "mouse jiggler" that just calls `SetCursorPos` in a loop — turned
out not to be reliable: moving the cursor alone isn't consistently
recognized by Windows' sleep-idle tracking, and can be ignored outright on
devices with mouse-jiggler detection enabled.

`Hancock.py` instead calls `SetThreadExecutionState` with
`ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED` — the actual Windows API apps
like video players and installers use to say "don't sleep, I need this."
The cursor tracing is purely cosmetic; it isn't what prevents sleep.

Note: this cannot and does not try to override a hard **lock-screen**
policy enforced by IT via Group Policy. If your device locks the session
on a fixed timer regardless of activity, that's a security control, not a
power setting, and no user-mode script should (or can) bypass it.

## Requirements

- Windows
- Python 3
- [`hershey-fonts`](https://pypi.org/project/hershey-fonts/) — renders the
  cursive letter shapes at startup for whatever text you ask for:

  ```bash
  pip install hershey-fonts
  ```

No other dependencies. Mouse/keyboard control and sleep prevention use
`ctypes` calls into `user32.dll` / `kernel32.dll` directly.

## Usage

```bash
python Hancock.py
```

Defaults to tracing "Lorem Ipsum". Options:

| Flag | Meaning | Default |
|---|---|---|
| `-s`, `--spell` | Text to trace in cursive | `"Lorem Ipsum"` |
| `-r`, `--resume-delay` | Seconds of no mouse movement or keystrokes before resuming after you interrupt it | `3.0` |
| `-t`, `--timeout` | Stop automatically after this many hours | `2.0` |

Examples:

```bash
python Hancock.py -s "Evan P Oldford"
python Hancock.py -s "Evan P Oldford" -r 5
python Hancock.py -t 12
```

## Behavior

- **Won't fight you for the mouse.** If you move the mouse or start
  typing, tracing pauses immediately. It waits until you've been fully
  idle (no mouse movement, no keystrokes — checked via polled
  `GetAsyncKeyState`, not a keyboard hook) for `--resume-delay` seconds,
  then glides smoothly from wherever you left the cursor back onto the
  path at the exact point it paused, rather than jumping or resuming from
  the start.
- **Randomized placement.** Each full pass through the text picks a new
  random on-screen position (still fully within screen bounds), so the
  trace doesn't sit over the same pixels for hours on end.
- **Self-limiting.** Stops on its own after `--timeout` hours, or
  immediately on Ctrl+C. Either way it releases the sleep-prevention
  request on exit.

## How the cursive shapes are built

Letters come from the Hershey "cursive" font (a single-stroke font
designed for pen plotters, which maps naturally onto cursor movement).
`build_name_path()` renders the requested text into strokes, then bridges
the pen-lifts between strokes/letters with straight connectors so the
cursor traces one continuous path instead of teleporting between letters.

A `tmp/` folder alongside this file holds the throwaway scripts used while
prototyping (font/shape comparisons, path preview renders, etc.). It's not
tracked in this repo and isn't needed to run `Hancock.py` — it's local
scratch work, kept only as a personal record of how the approach was
chosen.
