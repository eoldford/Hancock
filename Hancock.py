"""
Keeps the machine awake by requesting sleep prevention from the OS and
slowly moving the mouse cursor through a little game, cycling between
tic-tac-toe, Connect Four, Pong, Snowman, and a maze solve. Pass
--spell <text> to trace that text in cursive instead. Runs until cancelled
with Ctrl+C.

Runs on Windows and macOS. Cursive mode's letter shapes come from the
Hershey "cursive" font via the `hershey-fonts` pip package (pip install
hershey-fonts), rendered at startup for whatever text is requested -- see
tmp/cursive_name_design_v3.py for how this was first prototyped against a
fixed name.

Windows: uses ctypes to call user32.dll / kernel32.dll directly
(SetCursorPos, GetCursorPos, GetSystemMetrics, GetAsyncKeyState,
SetThreadExecutionState).

macOS: sleep prevention runs `caffeinate -d -i` as a child process; screen
size comes from CoreGraphics via ctypes; cursor control and keystroke
detection use `pynput` (pip install pynput). The terminal app running
Python needs Accessibility and Input Monitoring permission in System
Settings -> Privacy & Security.

Moving the cursor alone isn't reliably recognized by the OS's sleep-idle
tracking (and can be ignored outright on managed devices with mouse-jiggler
detection). SetThreadExecutionState (Windows) and caffeinate (macOS) are
the actual sanctioned ways to tell the power manager "don't sleep" -- the
cursor trace here is just the visual, not the mechanism preventing sleep.

If you move the mouse or start typing, tracing pauses until you've been
idle again for --resume-delay seconds, then glides back onto the path where
it left off.

A window that draws the trace on a canvas as the cursor moves is on by
default (needs `pygame-ce`: pip install pygame-ce; falls back to running
without it if that's missing) -- pass --no-show to disable it.
"""

__version__ = "1.0.2-022"

import sys

# Checked before the other imports so an old interpreter gets a clear message
# instead of a confusing failure further down.
MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    sys.exit(
        f"Hancock needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; "
        f"this is Python {sys.version_info[0]}.{sys.version_info[1]}."
    )

import argparse
import ctypes
import math
import os
import platform
import queue
import random
import threading
import time

try:
    from HersheyFonts import HersheyFonts
    HERSHEY_AVAILABLE = True
except ImportError:
    HERSHEY_AVAILABLE = False

STEP_DELAY = 0.05      # seconds between cursor updates -- controls how slow the writing looks
WIDTH_RATIO = 0.85     # the text spans this fraction of the screen's width
RETURN_STEPS = 40      # points used to glide back to the start before re-tracing
RESUME_STEPS = 20      # points used to glide from a manual mouse move back onto the path
PAUSE_SECONDS = 60.0   # how long the mouse must sit still before resuming after a manual move
POSITION_TOLERANCE = 2 # pixels of slack before a position mismatch counts as a manual move
TIMEOUT_HOURS = 2.0    # stop automatically after this many hours
CONNECTOR_STEPS = 8    # points added when bridging a pen-lift (between strokes/letters)
SEGMENT_STEPS = 5      # points added along each raw font segment, for smoothness
BASE_LINE_STEP_LENGTH = 0.2  # normalized-unit distance per step for board/court/wall/ground
                             # outlines -- these are long straight strokes, so without
                             # this they'd draw much faster than the shorter pieces/marks
PONG_BALL_STEP_LENGTH = 0.05 # normalized-unit distance per step for the ball's bounce
                             # path -- same idea, so a rally doesn't zip across the court
                             # in under 2 seconds
MAZE_PATH_STEP_LENGTH = 0.1  # normalized-unit distance per step for tracing the maze's
                             # solution path -- same idea, so the walk through corridors
                             # isn't instant
DRIFT_MARGIN = 20          # pixels inset from the literal screen corner for --drift mode
DRIFT_HOLD_STEPS = 20      # points used to hold position once --drift mode has arrived,
                           # instead of recomputing a zero-length path every pass

# Scripted tic-tac-toe games (cell moves as (col, row, symbol), 0-indexed),
# chosen at random each pass. "win" is the winning triple of (col, row), or
# None for a drawn-out game with no winner.
TTT_GAMES = [
    {
        "moves": [(0, 0, "X"), (1, 1, "O"), (1, 0, "X"), (2, 1, "O"), (2, 0, "X")],
        "win": [(0, 0), (1, 0), (2, 0)],
    },
    {
        "moves": [
            (1, 1, "X"), (0, 0, "O"), (2, 0, "X"), (0, 2, "O"),
            (0, 1, "X"), (2, 1, "O"), (1, 0, "X"), (1, 2, "O"), (2, 2, "X"),
        ],
        "win": None,
    },
]

# Scripted Connect Four games (dropped pieces as (col, row, player),
# 0-indexed, on a 7-wide x 6-tall board), chosen at random each time this
# game comes up in the cycle. "win" is the winning 4-in-a-row of (col, row).
CONNECT_FOUR_GAMES = [
    {
        "moves": [
            (0, 0, "R"), (0, 1, "Y"), (1, 0, "R"), (1, 1, "Y"),
            (3, 0, "R"), (3, 1, "Y"), (2, 0, "R"),
        ],
        "win": [(0, 0), (1, 0), (2, 0), (3, 0)],
    },
    {
        # A diagonal win needs a "staircase" of filler pieces underneath
        # each step so nothing floats over an empty cell.
        "moves": [
            (0, 0, "R"),
            (1, 0, "Y"), (1, 1, "R"),
            (2, 0, "Y"), (2, 1, "Y"), (2, 2, "R"),
            (3, 0, "Y"), (3, 1, "Y"), (3, 2, "Y"), (3, 3, "R"),
        ],
        "win": [(0, 0), (1, 1), (2, 2), (3, 3)],
    },
]

# Scripted Pong rallies: a continuous back-and-forth ball path (in
# normalized court units, 0-4 x 0-2.5), chosen at random each time this
# game comes up in the cycle. The court outline and paddles are fixed.
PONG_RALLIES = [
    [(0.2, 1.0), (2.0, 2.3), (3.8, 0.3), (1.5, 0.2), (0.2, 1.8), (2.5, 0.1), (3.8, 2.0), (0.2, 1.2)],
    [(0.2, 2.0), (1.3, 0.2), (3.0, 2.2), (3.8, 1.0), (1.8, 0.2), (0.2, 1.5), (3.8, 2.1)],
]

# Ground line (fixed) for the snowman game, in normalized units where
# larger y is higher up -- the ground sits at y=0. The snowman itself
# (drawn only on a loss) is built from circles in build_snowman_path().
SNOWMAN_BASE = [
    [(0, 0), (2, 0)],  # ground
]

# Scripted snowman rounds: a word, and whether it ends fully revealed (win)
# or fully built with the word left blank (lose), chosen at random each
# time this game comes up in the cycle.
SNOWMAN_ROUNDS = [
    {"word": "Mouse", "outcome": "win"},
    {"word": "Cursor", "outcome": "lose"},
    {"word": "Pixel", "outcome": "win"},
]

# Scripted mazes (walls as (x0, y0)-(x1, y1) segments, path as the solution
# route through them), chosen at random each time this game comes up in the
# cycle. The second maze is just the first mirrored left-to-right.
MAZES = [
    {
        "walls": [
            [(1, 3), (4, 3)],  # top wall, entrance gap on the left
            [(0, 0), (3, 0)],  # bottom wall, exit gap on the right
            [(0, 0), (0, 3)],  # left wall
            [(4, 0), (4, 3)],  # right wall
            [(0, 2), (3, 2)],  # upper divider, gap on the right
            [(1, 1), (4, 1)],  # lower divider, gap on the left
        ],
        "path": [
            (0.5, 3.0), (0.5, 2.5), (3.5, 2.5), (3.5, 1.5),
            (0.5, 1.5), (0.5, 0.5), (3.5, 0.5), (3.5, 0.0),
        ],
    },
    {
        "walls": [
            [(0, 3), (3, 3)],  # top wall, entrance gap on the right
            [(1, 0), (4, 0)],  # bottom wall, exit gap on the left
            [(0, 0), (0, 3)],  # left wall
            [(4, 0), (4, 3)],  # right wall
            [(1, 2), (4, 2)],  # upper divider, gap on the left
            [(0, 1), (3, 1)],  # lower divider, gap on the right
        ],
        "path": [
            (3.5, 3.0), (3.5, 2.5), (0.5, 2.5), (0.5, 1.5),
            (3.5, 1.5), (3.5, 0.5), (0.5, 0.5), (0.5, 0.0),
        ],
    },
]

# ---------------------------------------------------------------------------
# Platform layer -- the same six functions on Windows and macOS:
# prevent_sleep, allow_sleep, get_screen_size, move_cursor, get_cursor_pos,
# key_was_pressed.
# ---------------------------------------------------------------------------

_SYSTEM = platform.system()

if _SYSTEM == "Windows":
    from ctypes import wintypes

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

    # Virtual-key ranges Microsoft's VK code table documents as reserved or
    # unassigned -- no physical key ever reports in these, on any keyboard
    # layout, so key_was_pressed() skips them to cut the per-call count.
    _VK_UNASSIGNED_RANGES = (
        (0x07, 0x07), (0x0A, 0x0B), (0x0E, 0x0F), (0x3A, 0x40),
        (0x5E, 0x5E), (0x88, 0x8F), (0x97, 0x9F), (0xB8, 0xB9),
    )
    _VK_CODES = tuple(
        vk for vk in range(1, 256)
        if not any(lo <= vk <= hi for lo, hi in _VK_UNASSIGNED_RANGES)
    )

    def key_was_pressed():
        """Return True if any key was pressed since the last call. Relies on
        GetAsyncKeyState's low-order bit, which reports "pressed since the
        previous call" and resets itself each time it's read -- so this must be
        polled regularly (not called more than once per check) to stay accurate."""
        for vk in _VK_CODES:
            if user32.GetAsyncKeyState(vk) & 0x0001:
                return True
        return False

elif _SYSTEM == "Darwin":
    import ctypes.util
    import subprocess

    try:
        from pynput import keyboard as _pkeyboard
        from pynput import mouse as _pmouse
    except ImportError:
        sys.exit(
            "pynput is required on macOS. Install it with:\n"
            "    pip install -r requirements.txt\n"
            "Then grant your terminal app Accessibility and Input Monitoring\n"
            "permission in System Settings -> Privacy & Security."
        )

    # Main-display size in points (the same coordinate space pynput uses, so
    # Retina scaling doesn't need special handling).
    _CG = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
    _CG.CGMainDisplayID.restype = ctypes.c_uint32
    _CG.CGDisplayPixelsWide.restype = ctypes.c_size_t
    _CG.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
    _CG.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    _CG.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]

    _mouse = _pmouse.Controller()
    _caffeinate_proc = None

    # macOS has no equivalent of GetAsyncKeyState's "pressed since last call"
    # bit, so a background pynput listener sets a flag that key_was_pressed()
    # reads and clears -- same once-per-call semantics as the Windows version.
    _key_flag = threading.Event()
    _key_listener = _pkeyboard.Listener(on_press=lambda key: _key_flag.set())
    _key_listener.daemon = True
    _key_listener.start()

    def prevent_sleep():
        # caffeinate -d (display) -i (idle system sleep); -w ties it to this
        # process, so it exits on its own even if Python is killed abruptly.
        global _caffeinate_proc
        if _caffeinate_proc is None or _caffeinate_proc.poll() is not None:
            _caffeinate_proc = subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())])

    def allow_sleep():
        global _caffeinate_proc
        if _caffeinate_proc is not None:
            _caffeinate_proc.terminate()
            _caffeinate_proc = None

    def get_screen_size():
        display = _CG.CGMainDisplayID()
        return int(_CG.CGDisplayPixelsWide(display)), int(_CG.CGDisplayPixelsHigh(display))

    def move_cursor(x, y):
        _mouse.position = (int(x), int(y))

    def get_cursor_pos():
        x, y = _mouse.position
        return int(x), int(y)

    def key_was_pressed():
        """Return True if any key was pressed since the last call."""
        if _key_flag.is_set():
            _key_flag.clear()
            return True
        return False

else:
    sys.exit(f"Unsupported platform: {_SYSTEM!r} (Hancock supports Windows and macOS)")


# Set to a queue.Queue when --show is active; the tracing loop posts
# ("path", points, pen_down) at the start of each pass and ("pt", index)
# after each cursor step, and the overlay window draws from it (skipping
# segments where pen_down is False). None means no overlay.
_draw_queue = None


def positions_match(a, b, tolerance=POSITION_TOLERANCE):
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}")


def interpolate(p0, p1, steps):
    x0, y0 = p0
    x1, y1 = p1
    return [(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps) for i in range(1, steps + 1)]


def wait_for_user_to_settle(pause_seconds=PAUSE_SECONDS, poll_interval=0.3, stop_event=None):
    """Block until neither the mouse has moved nor a key has been pressed for
    `pause_seconds`, resetting the countdown on any activity so it only
    resumes once the user is truly done (e.g. finished typing). Returns
    early if `stop_event` is set."""
    last_pos = get_cursor_pos()
    last_change = time.time()
    while time.time() - last_change < pause_seconds:
        if stop_event is not None and stop_event.is_set():
            break
        time.sleep(poll_interval)
        pos = get_cursor_pos()
        moved = not positions_match(pos, last_pos)
        typed = key_was_pressed()
        if moved or typed:
            last_pos = pos
            last_change = time.time()
    return last_pos


def build_continuous_path(strokes, step_length=None):
    """Bridge a list of pen-lifted strokes (each a list of points, in
    normalized units) into one continuous pen path, interpolating connectors
    between strokes and smoothing each stroke's own segments, so the cursor
    never teleports. Returns (path, pen_down), where pen_down[i] says
    whether the segment from path[i-1] to path[i] is part of a stroke (True)
    or just a connector bridging a pen-lift (False) -- --show uses this to
    skip drawing the connectors.

    By default each raw segment gets a fixed SEGMENT_STEPS points, which
    looks right for the short segments pieces/marks/letters are made of.
    Pass `step_length` for long straight strokes (board/court/wall/ground
    outlines) so their step count scales with length instead -- otherwise
    they'd draw much faster than everything else sharing the same fixed
    step count."""
    path = [strokes[0][0]]
    pen_down = [False]
    for stroke_index, stroke in enumerate(strokes):
        if stroke_index > 0:
            connector = interpolate(path[-1], stroke[0], CONNECTOR_STEPS)
            path += connector
            pen_down += [False] * len(connector)
        for point in stroke[1:]:
            if step_length is not None:
                distance = math.hypot(point[0] - path[-1][0], point[1] - path[-1][1])
                steps = max(2, round(distance / step_length))
            else:
                steps = SEGMENT_STEPS
            segment = interpolate(path[-1], point, steps)
            path += segment
            pen_down += [True] * len(segment)
    return path, pen_down


def join_paths(*path_pen_pairs):
    """Concatenate several (path, pen_down) pairs end-to-end, bridging each
    gap with its own pen-lift connector (so the cursor still glides between
    them smoothly, but --show won't draw a transit line)."""
    path, pen_down = list(path_pen_pairs[0][0]), list(path_pen_pairs[0][1])
    for next_path, next_pen in path_pen_pairs[1:]:
        connector = interpolate(path[-1], next_path[0], CONNECTOR_STEPS)
        path += connector
        pen_down += [False] * len(connector)
        path += next_path[1:]
        pen_down += next_pen[1:]
    return path, pen_down


def build_circle_stroke(cx, cy, radius, points=16):
    """A closed circular stroke (returns to its own start point) centered
    at (cx, cy)."""
    return [
        (cx + radius * math.cos(2 * math.pi * i / points), cy + radius * math.sin(2 * math.pi * i / points))
        for i in range(points + 1)
    ]


def rescale_path(path, box):
    """Rescale `path` to fit within `box` (x0, y0, x1, y1), preserving
    aspect ratio, anchored at the box's bottom-left corner."""
    x0, y0, x1, y1 = box
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    scale = min((x1 - x0) / (x_max - x_min), (y1 - y0) / (y_max - y_min))
    return [(x0 + (x - x_min) * scale, y0 + (y - y_min) * scale) for x, y in path]


DOODLE_STEP_LENGTH = 0.0125 # normalized-unit distance covered per interpolation step -- keeps
                          # doodle speed constant regardless of how far apart two random
                          # waypoints land, instead of the fixed step count elsewhere that
                          # speeds up over longer hops


def build_doodle_path(num_points=10, bounds=(0, 0, 4, 2.5)):
    """A smooth, meandering random path (in normalized units), used in place
    of cursive text when hershey-fonts isn't installed. Each segment's step
    count scales with its length, so the cursor moves at a constant speed
    throughout rather than flying through longer hops between waypoints.
    Returns (path, pen_down) like build_continuous_path -- doodling has no
    pen lifts, so every segment after the first is drawn."""
    x0, y0, x1, y1 = bounds
    waypoints = [(random.uniform(x0, x1), random.uniform(y0, y1)) for _ in range(num_points)]
    path = [waypoints[0]]
    pen_down = [False]
    for point in waypoints[1:]:
        distance = math.hypot(point[0] - path[-1][0], point[1] - path[-1][1])
        steps = max(2, round(distance / DOODLE_STEP_LENGTH))
        segment = interpolate(path[-1], point, steps)
        path += segment
        pen_down += [True] * len(segment)
    return path, pen_down


def build_drift_path(deadline):
    """A straight path from wherever the cursor currently is toward the
    screen's bottom-left corner -- used by --drift mode. Already in
    screen pixels (unlike every other path builder), since
    build_screen_path's normalized-unit scaling doesn't apply here. Paced
    from the time remaining until `deadline` (a time.time() value) rather
    than a fixed speed, so the cursor arrives right as the timeout does,
    however long that is -- recomputed fresh from the current position
    and remaining time each pass, so a manual move or a pause/resume
    doesn't throw off the arrival time. Once arrived (within a pixel),
    returns a short "hold" instead of a near-zero-length path, so the
    loop doesn't spin once there -- the next pass will still notice
    straightaway if the user moves the mouse away and resume drifting."""
    cx, cy = get_cursor_pos()
    screen_width, screen_height = get_screen_size()
    target = (DRIFT_MARGIN, screen_height - 1 - DRIFT_MARGIN)
    distance = math.hypot(target[0] - cx, target[1] - cy)
    if distance < 1:
        path = [(cx, cy)] * DRIFT_HOLD_STEPS
    else:
        remaining = max(deadline - time.time(), STEP_DELAY)
        steps = max(2, round(remaining / STEP_DELAY))
        path = [(cx, cy)] + interpolate((cx, cy), target, steps)
    pen_down = [False] + [True] * (len(path) - 1)
    return path, pen_down


def build_name_path(text):
    """Render `text` with the Hershey cursive font into a single continuous
    pen path (in normalized font units). Falls back to a random doodle path
    if hershey-fonts isn't installed."""
    if not HERSHEY_AVAILABLE:
        return build_doodle_path()
    h = HersheyFonts()
    h.load_default_font("cursive")
    h.normalize_rendering(1.0)
    strokes = list(h.strokes_for_text(text))
    if not strokes:
        raise ValueError(f"No renderable characters in: {text!r}")
    return build_continuous_path(strokes)


def build_mark_strokes(col, row, symbol):
    """Strokes for an X or O inside the given grid cell (0-indexed)."""
    if symbol == "X":
        return [
            [(col + 0.15, row + 0.15), (col + 0.85, row + 0.85)],
            [(col + 0.85, row + 0.15), (col + 0.15, row + 0.85)],
        ]
    return [build_circle_stroke(col + 0.5, row + 0.5, 0.35)]


def build_tic_tac_toe_path():
    """Pick a random scripted game and render the grid, the moves in order,
    and (if there's a winner) the strike-through line, as one continuous
    pen path (in normalized grid units). The grid is drawn as its own base
    path, at base-line speed."""
    game = random.choice(TTT_GAMES)
    grid_strokes = [
        [(1, 0), (1, 3)],
        [(2, 0), (2, 3)],
        [(0, 1), (3, 1)],
        [(0, 2), (3, 2)],
    ]
    move_strokes = []
    for col, row, symbol in game["moves"]:
        move_strokes.extend(build_mark_strokes(col, row, symbol))
    if game["win"]:
        (c0, r0), (c1, r1) = game["win"][0], game["win"][-1]
        move_strokes.append([(c0 + 0.5, r0 + 0.5), (c1 + 0.5, r1 + 0.5)])
    grid_path = build_continuous_path(grid_strokes, step_length=BASE_LINE_STEP_LENGTH)
    moves_path = build_continuous_path(move_strokes)
    return join_paths(grid_path, moves_path)


def build_connect_four_path():
    """Pick a random scripted Connect Four game and render the 7x6 grid,
    the dropped pieces in order, and the winning line, as one continuous
    pen path (in normalized grid units). The grid is drawn as its own base
    path, at base-line speed."""
    game = random.choice(CONNECT_FOUR_GAMES)
    grid_strokes = [[(col, 0), (col, 6)] for col in range(1, 7)]
    grid_strokes += [[(0, row), (7, row)] for row in range(1, 6)]
    move_strokes = []
    for col, row, player in game["moves"]:
        move_strokes.append(build_circle_stroke(col + 0.5, row + 0.5, 0.35))
    (c0, r0), (c1, r1) = game["win"][0], game["win"][-1]
    move_strokes.append([(c0 + 0.5, r0 + 0.5), (c1 + 0.5, r1 + 0.5)])
    grid_path = build_continuous_path(grid_strokes, step_length=BASE_LINE_STEP_LENGTH)
    moves_path = build_continuous_path(move_strokes)
    return join_paths(grid_path, moves_path)


def build_pong_path():
    """Pick a random scripted Pong rally and render the court, both
    paddles, and the ball's bouncing path, as one continuous pen path (in
    normalized court units). The court and paddles are drawn as their own
    base path, at base-line speed."""
    rally = random.choice(PONG_RALLIES)
    base_strokes = [
        [(0, 0), (4, 0), (4, 2.5), (0, 2.5), (0, 0)],  # court outline
        [(0.15, 0.85), (0.15, 1.65)],                  # left paddle
        [(3.85, 0.85), (3.85, 1.65)],                  # right paddle
    ]
    base_path = build_continuous_path(base_strokes, step_length=BASE_LINE_STEP_LENGTH)
    ball_path = build_continuous_path([rally], step_length=PONG_BALL_STEP_LENGTH)
    return join_paths(base_path, ball_path)


def build_snowman_path():
    """Pick a random scripted snowman round and render the ground, plus
    either the revealed word (win) or the snowman and word blanks (lose),
    as one continuous pen path (in normalized units). The ground is drawn
    as its own base path, at base-line speed."""
    round_ = random.choice(SNOWMAN_ROUNDS)
    base_path = build_continuous_path(list(SNOWMAN_BASE), step_length=BASE_LINE_STEP_LENGTH)
    if round_["outcome"] == "lose":
        action_strokes = [
            build_circle_stroke(1.0, 0.6, 0.6),    # bottom
            build_circle_stroke(1.0, 1.5, 0.45),   # middle
            build_circle_stroke(1.0, 2.15, 0.3),   # head
            [(0.55, 1.5), (0.1, 1.7)],              # left arm
            [(1.45, 1.5), (1.9, 1.7)],               # right arm
        ]
        action_strokes += [[(i * 0.5, -0.6), (i * 0.5 + 0.3, -0.6)] for i in range(len(round_["word"]))]
        action_path = build_continuous_path(action_strokes)
        return join_paths(base_path, action_path)
    box = (0, -1.0, len(round_["word"]) * 0.5, -0.3)
    word_path, word_pen = build_name_path(round_["word"])
    word_path = rescale_path(word_path, box)
    return join_paths(base_path, (word_path, word_pen))


def build_maze_path():
    """Pick a random scripted maze and render its walls, then trace the
    solution path through them, as one continuous pen path (in normalized
    grid units). The walls are drawn as their own base path, at base-line
    speed."""
    maze = random.choice(MAZES)
    walls_path = build_continuous_path(maze["walls"], step_length=BASE_LINE_STEP_LENGTH)
    solution_path = build_continuous_path([maze["path"]], step_length=MAZE_PATH_STEP_LENGTH)
    return join_paths(walls_path, solution_path)


GAME_BUILDERS = [
    build_tic_tac_toe_path, build_connect_four_path, build_pong_path,
    build_snowman_path, build_maze_path,
]


def build_screen_path(name_path):
    """Rescale and place a (path, pen_down) pair onto the screen, appending
    a glide back to the start. Returns (screen_path, pen_down) -- the
    return glide is marked pen-up, same as any other connector."""
    screen_width, screen_height = get_screen_size()
    path, pen_down = name_path

    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
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
        for x, y in path
    ]

    # glide back to the start instead of jumping, so looping stays smooth
    return_path = interpolate(screen_path[-1], screen_path[0], RETURN_STEPS)

    return screen_path + return_path, pen_down + [False] * len(return_path)


def move_along_path(path, pause_seconds=PAUSE_SECONDS, deadline=None, stop_event=None, abort_on_resume=False):
    """Move the cursor through path, one point at a time. If the user moves
    the mouse or presses a key, pause until they're done, then glide back
    onto the path at the same point (rather than jumping or fighting for
    control) -- unless `abort_on_resume` is set, in which case it just
    returns once they've settled, so the caller can build a fresh path
    instead of resuming a now-stale one (--drift mode: it should re-aim
    from wherever the user actually moved to, not glide back to wherever
    it was before they moved the mouse). Returns early once
    `deadline` (a time.time() value) has passed or `stop_event` is set."""
    last_commanded = None
    for index, target in enumerate(path):
        if deadline is not None and time.time() >= deadline:
            return
        if stop_event is not None and stop_event.is_set():
            return
        key_pressed = key_was_pressed()  # must be called exactly once per iteration -- see key_was_pressed()
        mouse_moved = last_commanded is not None and not positions_match(get_cursor_pos(), last_commanded)
        if mouse_moved or key_pressed:
            if mouse_moved and key_pressed:
                log("Manual mouse and keyboard activity detected -- pausing...")
            elif mouse_moved:
                log("Manual mouse movement detected -- pausing...")
            else:
                log("Keyboard activity detected -- pausing...")
            resumed_at = wait_for_user_to_settle(pause_seconds=pause_seconds, stop_event=stop_event)
            log("Resuming.")
            if abort_on_resume:
                return
            for gx, gy in interpolate(resumed_at, target, RESUME_STEPS):
                move_cursor(gx, gy)
                time.sleep(STEP_DELAY)
        else:
            move_cursor(*target)
        if _draw_queue is not None:
            _draw_queue.put(("pt", index))
        last_commanded = target
        time.sleep(STEP_DELAY)


# ---------------------------------------------------------------------------
# --show overlay window
# ---------------------------------------------------------------------------

OVERLAY_TRACE_COLOR = (0, 229, 255)  # bright cyan
OVERLAY_BG_COLOR = (13, 13, 13)      # near-black
OVERLAY_LINE_WIDTH = 3
OVERLAY_PADDING = 32                 # pixels of margin around the drawing
OVERLAY_MAX_SIZE = (960, 540)        # largest canvas, shrunk to fit small screens
OVERLAY_ACTIVE_FPS = 60              # redraw rate right after drawing something new
OVERLAY_IDLE_FPS = 10                # redraw rate while there's nothing new to draw
                                      # (e.g. during a pause/resume wait) -- still
                                      # responsive enough to notice the window closing


def fit_path_to_canvas(path, canvas_width, canvas_height, padding=OVERLAY_PADDING):
    """Scale a screen-space path (already y-down) to fit centered inside the
    canvas, preserving aspect ratio."""
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    scale = min(
        (canvas_width - 2 * padding) / max(x_max - x_min, 1e-9),
        (canvas_height - 2 * padding) / max(y_max - y_min, 1e-9),
    )
    offset_x = (canvas_width - (x_max - x_min) * scale) / 2
    offset_y = (canvas_height - (y_max - y_min) * scale) / 2
    return [(offset_x + (x - x_min) * scale, offset_y + (y - y_min) * scale) for x, y in path]


def run_overlay(title, stop_event):
    """Show a window that draws each pass's trace as the cursor follows it.
    Runs on the main thread (required by macOS for any GUI window) and
    returns once the window is closed, the tracing loop finishes, or
    `stop_event` is set."""
    import pygame

    screen_width, screen_height = get_screen_size()
    canvas_width = min(OVERLAY_MAX_SIZE[0], screen_width - 80)
    canvas_height = min(OVERLAY_MAX_SIZE[1], screen_height - 160)

    os.environ.setdefault("SDL_VIDEO_WINDOW_POS", "40,60")
    pygame.init()
    window = pygame.display.set_mode((canvas_width, canvas_height))
    pygame.display.set_caption(title)
    clock = pygame.time.Clock()
    window.fill(OVERLAY_BG_COLOR)

    canvas_path = []
    canvas_pen = []  # canvas_pen[i]: whether to draw the line into canvas_path[i]
    last_index = 0
    try:
        while not stop_event.is_set():
            for event in pygame.event.get():
                if event.type == pygame.QUIT:  # window closed, or Ctrl+C under SDL
                    stop_event.set()
            changed = False
            try:
                while True:
                    item = _draw_queue.get_nowait()
                    if item is None:  # tracing loop finished
                        stop_event.set()
                        break
                    if item[0] == "path":
                        screen_path, canvas_pen = item[1], item[2]
                        canvas_path = fit_path_to_canvas(screen_path, canvas_width, canvas_height)
                        last_index = 0
                        window.fill(OVERLAY_BG_COLOR)
                        changed = True
                    elif item[0] == "pt":
                        end = item[1]
                        for i in range(last_index + 1, end + 1):
                            if canvas_pen[i]:
                                pygame.draw.line(window, OVERLAY_TRACE_COLOR, canvas_path[i - 1], canvas_path[i], OVERLAY_LINE_WIDTH)
                        last_index = max(last_index, end)
                        changed = True
            except queue.Empty:
                pass
            # Only flip (and poll events quickly) when something new was drawn --
            # otherwise (e.g. during a pause/resume wait) there's nothing to show,
            # so idle at a much lower rate instead of spinning at 60fps for no reason.
            if changed:
                pygame.display.flip()
                clock.tick(OVERLAY_ACTIVE_FPS)
            else:
                clock.tick(OVERLAY_IDLE_FPS)
    except KeyboardInterrupt:
        print("\nStopped.")
        stop_event.set()
    finally:
        pygame.quit()


# ---------------------------------------------------------------------------
# Main loop and CLI
# ---------------------------------------------------------------------------


def trace_until_done(args, name_path, deadline, stop_event):
    """Keep tracing (cursive text, the games in turn, or --drift's crawl
    to a corner) until the deadline passes or `stop_event` is set."""
    game_index = 0
    abort_on_resume = args.drift
    while time.time() < deadline and not stop_event.is_set():
        prevent_sleep()  # re-affirmed each pass through the name in case anything clears it
        if args.drift:
            path, pen_down = build_drift_path(deadline)
        # re-randomizes placement each pass; cycles through the games in order otherwise
        elif name_path is not None:
            path, pen_down = build_screen_path(name_path)
        else:
            path, pen_down = build_screen_path(GAME_BUILDERS[game_index % len(GAME_BUILDERS)]())
            game_index += 1
        if _draw_queue is not None:
            _draw_queue.put(("path", path, pen_down))
        move_along_path(
            path, pause_seconds=args.resume_delay, deadline=deadline, stop_event=stop_event,
            abort_on_resume=abort_on_resume,
        )
    if not stop_event.is_set():
        print(f"\nTimeout of {args.timeout} hour(s) reached. Stopping.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Keep the machine awake by playing tic-tac-toe (or tracing text in cursive) with the mouse."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-s", "--spell", type=str, default=None,
        help="text to trace in cursive instead of playing tic-tac-toe (default: play tic-tac-toe)",
    )
    parser.add_argument(
        "-r", "--resume-delay", type=float, default=PAUSE_SECONDS,
        help=f"seconds of no mouse movement or keystrokes before resuming (default: {PAUSE_SECONDS})",
    )
    parser.add_argument(
        "-t", "--timeout", type=float, default=TIMEOUT_HOURS,
        help=f"stop automatically after this many hours (default: {TIMEOUT_HOURS})",
    )
    parser.add_argument(
        "--show", action=argparse.BooleanOptionalAction, default=True,
        help="open a window that draws the trace as the cursor moves (requires pygame-ce); use --no-show to disable",
    )
    parser.add_argument(
        "--drift", action="store_true",
        help="slowly crawl the cursor to the screen's bottom-left corner and hold there, "
             "instead of playing games or tracing cursive text; implies --no-show and "
             "ignores -s/--spell",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    show = args.show and not args.drift
    show_fallback = False
    if show:
        try:
            import pygame  # noqa: F401 -- only checking it's installed
        except ImportError:
            show = False
            show_fallback = True
    cursive_mode = args.spell is not None and not args.drift
    doodle_fallback = cursive_mode and not HERSHEY_AVAILABLE
    name_path = build_name_path(args.spell) if cursive_mode else None
    deadline = time.time() + args.timeout * 3600

    print(f"Hancock {__version__}")
    if args.drift:
        print("Slowly crawling the mouse to the bottom-left corner to keep the machine awake.")
    elif show_fallback:
        print(
            "pygame-ce isn't installed, so running without the --show window. "
            "Install it with:\n    pip install pygame-ce"
        )
    if doodle_fallback:
        print(
            f"hershey-fonts isn't installed, so this will doodle randomly instead of "
            f"tracing {args.spell!r} to keep the machine awake. Install it with:\n"
            "    pip install hershey-fonts"
        )
    elif cursive_mode:
        print(f"Moving the mouse in a slow cursive trace of {args.spell!r} to keep the machine awake.")
    elif not args.drift:
        print("Playing tic-tac-toe, Connect Four, Pong, Snowman, and a maze solve to keep the machine awake.")
    print(f"Moving the mouse or typing will pause tracing until you've stopped for {args.resume_delay}s.")
    print(f"Will stop automatically after {args.timeout} hour(s), or press Ctrl+C to stop sooner.")

    stop_event = threading.Event()
    if not show:
        try:
            trace_until_done(args, name_path, deadline, stop_event)
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            allow_sleep()
        return

    # --show: the window must own the main thread (a macOS requirement), so
    # the cursor tracing moves to a background thread.
    global _draw_queue
    _draw_queue = queue.Queue()

    def worker():
        try:
            trace_until_done(args, name_path, deadline, stop_event)
        finally:
            allow_sleep()
            _draw_queue.put(None)  # tells the overlay to close

    tracer = threading.Thread(target=worker, daemon=True)
    tracer.start()
    try:
        run_overlay(f"Hancock \u00b7 {args.spell}" if cursive_mode else "Hancock", stop_event)
    finally:
        stop_event.set()
        tracer.join(timeout=2)
        allow_sleep()


if __name__ == "__main__":
    main()
