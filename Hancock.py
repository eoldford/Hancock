"""
Keeps the machine awake by requesting sleep prevention from Windows and
slowly moving the mouse cursor along the shape of a chosen phrase written
in cursive (default: "Lorem Ipsum"). Runs until cancelled with Ctrl+C.

Windows only: uses ctypes to call user32.dll / kernel32.dll directly
(SetCursorPos, GetSystemMetrics, SetThreadExecutionState). The cursive
letter shapes come from the Hershey "cursive" font via the `hershey-fonts`
pip package (pip install hershey-fonts), rendered at startup for whatever
text is requested -- see tmp/cursive_name_design_v3.py for how this was
first prototyped against a fixed name.

Moving the cursor alone isn't reliably recognized by Windows' sleep-idle
tracking (and can be ignored outright on managed devices with mouse-jiggler
detection). SetThreadExecutionState is the actual sanctioned API apps use
to tell the power manager "don't sleep" -- the cursor trace here is just
the visual, not the mechanism preventing sleep.

If you move the mouse or start typing, tracing pauses (via polled
GetAsyncKeyState, not a keyboard hook) until you've been idle again for
--resume-delay seconds, then glides back onto the path where it left off.
"""

import argparse
import ctypes
import random
import time
from ctypes import wintypes

from HersheyFonts import HersheyFonts

STEP_DELAY = 0.05      # seconds between cursor updates -- controls how slow the writing looks
WIDTH_RATIO = 0.85     # the text spans this fraction of the screen's width
RETURN_STEPS = 40      # points used to glide back to the start before re-tracing
RESUME_STEPS = 20      # points used to glide from a manual mouse move back onto the path
PAUSE_SECONDS = 3.0    # how long the mouse must sit still before resuming after a manual move
POSITION_TOLERANCE = 2 # pixels of slack before a position mismatch counts as a manual move
DEFAULT_TEXT = "Lorem Ipsum"
TIMEOUT_HOURS = 2.0    # stop automatically after this many hours
CONNECTOR_STEPS = 8    # points added when bridging a pen-lift (between strokes/letters)
SEGMENT_STEPS = 5      # points added along each raw font segment, for smoothness

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def prevent_sleep():
    kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)


def allow_sleep():
    kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def get_screen_size():
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)
    return width, height


def move_cursor(x, y):
    user32.SetCursorPos(int(x), int(y))


def get_cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def positions_match(a, b, tolerance=POSITION_TOLERANCE):
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def key_was_pressed():
    """Return True if any key was pressed since the last call. Relies on
    GetAsyncKeyState's low-order bit, which reports "pressed since the
    previous call" and resets itself each time it's read -- so this must be
    polled regularly (not called more than once per check) to stay accurate."""
    for vk in range(1, 256):
        if user32.GetAsyncKeyState(vk) & 0x0001:
            return True
    return False


def interpolate(p0, p1, steps):
    x0, y0 = p0
    x1, y1 = p1
    return [(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps) for i in range(1, steps + 1)]


def wait_for_user_to_settle(pause_seconds=PAUSE_SECONDS, poll_interval=0.3):
    """Block until neither the mouse has moved nor a key has been pressed for
    `pause_seconds`, resetting the countdown on any activity so it only
    resumes once the user is truly done (e.g. finished typing)."""
    last_pos = get_cursor_pos()
    last_change = time.time()
    while time.time() - last_change < pause_seconds:
        time.sleep(poll_interval)
        pos = get_cursor_pos()
        moved = not positions_match(pos, last_pos)
        typed = key_was_pressed()
        if moved or typed:
            last_pos = pos
            last_change = time.time()
    return last_pos


def build_name_path(text):
    """Render `text` with the Hershey cursive font into a single continuous
    pen path (in normalized font units), bridging pen-lifts between strokes
    and letters with straight connectors so the cursor never teleports."""
    h = HersheyFonts()
    h.load_default_font("cursive")
    h.normalize_rendering(1.0)
    strokes = list(h.strokes_for_text(text))
    if not strokes:
        raise ValueError(f"No renderable characters in: {text!r}")

    name_path = [strokes[0][0]]
    for stroke_index, stroke in enumerate(strokes):
        if stroke_index > 0:
            name_path += interpolate(name_path[-1], stroke[0], CONNECTOR_STEPS)
        for point in stroke[1:]:
            name_path += interpolate(name_path[-1], point, SEGMENT_STEPS)
    return name_path


def build_screen_path(name_path):
    screen_width, screen_height = get_screen_size()

    xs = [p[0] for p in name_path]
    ys = [p[1] for p in name_path]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    width_scale = (screen_width * WIDTH_RATIO) / (x_max - x_min)
    height_scale = (screen_height * WIDTH_RATIO) / (y_max - y_min)
    scale = min(width_scale, height_scale)
    path_width = (x_max - x_min) * scale
    path_height = (y_max - y_min) * scale

    # randomize placement within the screen each call, so the trace doesn't
    # sit in the same spot for hours on end (reduces static-image screen wear)
    origin_x = random.uniform(0, screen_width - path_width) if screen_width > path_width else 0
    origin_y = random.uniform(0, screen_height - path_height) if screen_height > path_height else 0

    # flip y: font data has larger y = higher up, but screen y grows downward
    screen_path = [
        (origin_x + (x - x_min) * scale, origin_y + (y_max - y) * scale)
        for x, y in name_path
    ]

    # glide back to the start instead of jumping, so looping stays smooth
    return_path = interpolate(screen_path[-1], screen_path[0], RETURN_STEPS)

    return screen_path + return_path


def move_along_path(path, pause_seconds=PAUSE_SECONDS, deadline=None):
    """Move the cursor through path, one point at a time. If the user moves
    the mouse or presses a key, pause until they're done, then glide back
    onto the path at the same point (rather than jumping or fighting for
    control). Returns early once `deadline` (a time.time() value) has passed."""
    last_commanded = None
    for target in path:
        if deadline is not None and time.time() >= deadline:
            return
        key_pressed = key_was_pressed()  # must be called exactly once per iteration -- see key_was_pressed()
        mouse_moved = last_commanded is not None and not positions_match(get_cursor_pos(), last_commanded)
        if mouse_moved or key_pressed:
            if mouse_moved and key_pressed:
                print("Manual mouse and keyboard activity detected -- pausing...")
            elif mouse_moved:
                print("Manual mouse movement detected -- pausing...")
            else:
                print("Keyboard activity detected -- pausing...")
            resumed_at = wait_for_user_to_settle(pause_seconds=pause_seconds)
            print("Resuming.")
            for gx, gy in interpolate(resumed_at, target, RESUME_STEPS):
                move_cursor(gx, gy)
                time.sleep(STEP_DELAY)
        else:
            move_cursor(*target)
        last_commanded = target
        time.sleep(STEP_DELAY)


def parse_args():
    parser = argparse.ArgumentParser(description="Trace text in cursive with the mouse to keep the machine awake.")
    parser.add_argument(
        "-s", "--spell", type=str, default=DEFAULT_TEXT,
        help=f"text to trace in cursive (default: {DEFAULT_TEXT!r})",
    )
    parser.add_argument(
        "-r", "--resume-delay", type=float, default=PAUSE_SECONDS,
        help=f"seconds of no mouse movement or keystrokes before resuming (default: {PAUSE_SECONDS})",
    )
    parser.add_argument(
        "-t", "--timeout", type=float, default=TIMEOUT_HOURS,
        help=f"stop automatically after this many hours (default: {TIMEOUT_HOURS})",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    name_path = build_name_path(args.spell)
    deadline = time.time() + args.timeout * 3600

    print(f"Moving the mouse in a slow cursive trace of {args.spell!r} to keep the machine awake.")
    print(f"Moving the mouse or typing will pause tracing until you've stopped for {args.resume_delay}s.")
    print(f"Will stop automatically after {args.timeout} hour(s), or press Ctrl+C to stop sooner.")

    try:
        while time.time() < deadline:
            prevent_sleep()  # re-affirmed each pass through the name in case anything clears it
            path = build_screen_path(name_path)  # re-randomizes placement each pass
            move_along_path(path, pause_seconds=args.resume_delay, deadline=deadline)
        print(f"\nTimeout of {args.timeout} hour(s) reached. Stopping.")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        allow_sleep()


if __name__ == "__main__":
    main()
