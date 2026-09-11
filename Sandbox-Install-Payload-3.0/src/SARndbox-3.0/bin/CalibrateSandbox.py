#!/usr/bin/env python3
"""
Calibrate Sandbox - a single-window wizard around the UC Davis AR Sandbox
calibration tools.

Phase 1  Base plane   (RawKinectViewer, "Extract Planes" tool on key 1)
Phase 2  Box corners  (RawKinectViewer, "Measure 3D Positions" tool on key 2)
Phase 3  Projector    (CalibrateProjector, "Capture" tool on keys 1 and 2)

The UC Davis programs are not modified. The wizard launches them, sends
instructions into their window through the Vrui command interface on stdin
("showMessage ..." / "quit"), parses what they print, validates the numbers,
backs up the old files and writes BoxLayout.txt.  CalibrateProjector writes
ProjectorMatrix.dat itself; the wizard only checks that it did.

Phases 1 and 2 share one RawKinectViewer session when both are selected.

Runs on Python 3.6 and later (Linux Mint 19.3 ships 3.6), standard library only.

Paths can be overridden with environment variables (used for testing):
  SANDBOX_CALIB_SARNDBOX_DIR        default ~/src/SARndbox-2.8
  SANDBOX_CALIB_ETC_DIR             default <sarndbox>/etc/SARndbox-2.8
  SANDBOX_CALIB_RAWKINECTVIEWER     default ~/src/Kinect-3.10/bin/RawKinectViewer
  SANDBOX_CALIB_CALIBRATEPROJECTOR  default <sarndbox>/bin/CalibrateProjector
  SANDBOX_CALIB_XBACKGROUND         default XBackground (on PATH)
  SANDBOX_CALIB_RUN_SANDBOX         default <sarndbox>/run-sandbox.sh
  SANDBOX_CALIB_SANDBOX_PROCESS     default SARndbox (process name to detect)
  SANDBOX_CALIB_CONTROL_FIFO        default <sarndbox>/share/SARndbox-2.8/Control.fifo
"""

import argparse
import datetime
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

APP_TITLE = "Calibrate Sandbox"
HOME = os.path.expanduser("~")
NUM_TIE_POINTS = 12  # CalibrateProjector default grid 4 x 3
HELPER_TIMEOUT = 10  # seconds to wait for the SandboxHelper plugin to report before falling back


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

class Paths:
    def __init__(self):
        env = os.environ.get
        self.sarndbox_dir = env("SANDBOX_CALIB_SARNDBOX_DIR", os.path.join(HOME, "src", "SARndbox-2.8"))
        self.etc_dir = env("SANDBOX_CALIB_ETC_DIR", os.path.join(self.sarndbox_dir, "etc", "SARndbox-2.8"))
        self.raw_kinect_viewer = env("SANDBOX_CALIB_RAWKINECTVIEWER",
                                     os.path.join(HOME, "src", "Kinect-3.10", "bin", "RawKinectViewer"))
        if not os.access(self.raw_kinect_viewer, os.X_OK) and shutil.which("RawKinectViewer"):
            self.raw_kinect_viewer = shutil.which("RawKinectViewer")  # installed copy in /usr/local/bin
        self.calibrate_projector = env("SANDBOX_CALIB_CALIBRATEPROJECTOR",
                                       os.path.join(self.sarndbox_dir, "bin", "CalibrateProjector"))
        self.xbackground = env("SANDBOX_CALIB_XBACKGROUND", "XBackground")
        self.run_sandbox = env("SANDBOX_CALIB_RUN_SANDBOX", os.path.join(self.sarndbox_dir, "run-sandbox.sh"))
        self.sandbox_process = env("SANDBOX_CALIB_SANDBOX_PROCESS", "SARndbox")
        self.control_fifo = env("SANDBOX_CALIB_CONTROL_FIFO",
                                os.path.join(self.sarndbox_dir, "share", "SARndbox-2.8", "Control.fifo"))
        # SandboxHelper: our Vrui plugin (../SandboxHelper/) that lets the wizard press "Average Frames"
        # and replace popups inside the tools. Empty when it is not installed.
        self.vislet = env("SANDBOX_CALIB_VISLET", "")
        if self.vislet.lower() in ("none", "off"):
            self.vislet = ""
        elif not self.vislet:
            for pattern in ("/usr/local/lib/*/Vrui-8.0/VRVislets/libSandboxHelper.so",
                            "/usr/local/lib/Vrui-8.0/VRVislets/libSandboxHelper.so",
                            "/usr/local/lib64/Vrui-8.0/VRVislets/libSandboxHelper.so"):
                hits = glob.glob(pattern)
                if hits:
                    self.vislet = hits[0]
                    break
        self.box_layout = os.path.join(self.etc_dir, "BoxLayout.txt")
        self.sandbox_cfg = os.path.join(self.etc_dir, "SARndbox.cfg")
        self.box_layout_orig = self.box_layout + ".orig"
        self.projector_matrix = os.path.join(self.etc_dir, "ProjectorMatrix.dat")
        self.projector_matrix_orig = self.projector_matrix + ".orig"
        self.state_file = os.path.join(self.etc_dir, "calibration-state.json")
        self.log_file = os.path.join(self.etc_dir, "calibration.log")
        self.backup_dir = os.path.join(self.etc_dir, "backups")

    def check(self):
        """Return a list of (label, path, ok) tuples for the --check option."""
        items = [
            ("SARndbox directory", self.sarndbox_dir, os.path.isdir(self.sarndbox_dir)),
            ("etc directory", self.etc_dir, os.path.isdir(self.etc_dir)),
            ("RawKinectViewer", self.raw_kinect_viewer, os.access(self.raw_kinect_viewer, os.X_OK)),
            ("CalibrateProjector", self.calibrate_projector, os.access(self.calibrate_projector, os.X_OK)),
            ("XBackground", self.xbackground, shutil.which(self.xbackground) is not None),
            ("run-sandbox.sh", self.run_sandbox, os.access(self.run_sandbox, os.X_OK)),
            ("BoxLayout.txt", self.box_layout, os.path.isfile(self.box_layout)),
            ("BoxLayout.txt.orig", self.box_layout_orig, os.path.isfile(self.box_layout_orig)),
            ("SARndbox.cfg", self.sandbox_cfg, os.path.isfile(self.sandbox_cfg)),
            ("ProjectorMatrix.dat", self.projector_matrix, os.path.isfile(self.projector_matrix)),
            ("Control.fifo", self.control_fifo, os.path.exists(self.control_fifo)),
            ("SandboxHelper plugin", self.vislet or "not installed: Average Frames is picked by hand", True),
        ]
        output, rotation = detect_display()
        items.append(("Display (xrandr)", "%s, rotation %s" % (output, rotation), output is not None))
        return items


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

class Log:
    def __init__(self, path):
        self.path = path
        self.listeners = []
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            pass

    def write(self, text, echo=True):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s" % (stamp, text)
        try:
            with open(self.path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if echo:
            for cb in self.listeners:
                cb(line)


# --------------------------------------------------------------------------
# Parsing of tool output and of BoxLayout.txt
# --------------------------------------------------------------------------

NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
PLANE_RE = re.compile(r"Camera-space plane equation:\s*x\s*\*\s*\(\s*(%s)\s*,\s*(%s)\s*,\s*(%s)\s*\)\s*=\s*(%s)"
                      % (NUM, NUM, NUM, NUM))
PLANE_RMS_RE = re.compile(r"Camera-space approximation RMS:\s*(%s)" % NUM)
POINT_RE = re.compile(r"^\s*\(\s*(%s)\s*,\s*(%s)\s*,\s*(%s)\s*\)\s*$" % (NUM, NUM, NUM))
LAYOUT_PLANE_RE = re.compile(r"^\s*\(\s*(%s)\s*,\s*(%s)\s*,\s*(%s)\s*\)\s*,\s*(%s)\s*$" % (NUM, NUM, NUM, NUM))
RMS_RE = re.compile(r"RMS calibration residual:\s*(%s)" % NUM)
TIEPOINT_DONE_RE = re.compile(r"Capturing\s+\d+\s+tie point frames\.\.\.\s*done")
BACKGROUND_RE = re.compile(r"Capturing\s+\d+\s+background frames")
CALIB_ERROR_RE = re.compile(r"Calibration error:\s*(.*)")
CONNECTED_RE = re.compile(r"Connected to 3D camera with serial number\s*(\S+)")
EXCEPTION_RE = re.compile(r"Terminated \w+ due to exception:\s*(.*)")

HELPER_LOADED_RE = re.compile(r"^SandboxHelper: loaded")
HELPER_IGNORED_RE = re.compile(r"Ignoring vislet of type SandboxHelper")
HELPER_AVERAGE_RE = re.compile(r"^SandboxHelper: (Average Frames on, capturing|average frame ready|Average Frames off|error: .*)")
CORNER_NAMES = ("lower-left", "lower-right", "upper-left", "upper-right")


def parse_plane_line(line):
    m = PLANE_RE.search(line)
    if not m:
        return None
    return tuple(float(m.group(i)) for i in range(1, 5))


def parse_point_line(line):
    m = POINT_RE.match(line)
    if not m:
        return None
    return tuple(float(m.group(i)) for i in range(1, 4))


def normalize_plane(plane):
    """Return (plane, flipped). The AR Sandbox needs a negative offset; second
    generation Kinects sometimes report the plane inverted (see UC Davis docs)."""
    nx, ny, nz, off = plane
    if off > 0:
        return ((-nx, -ny, -nz, -off), True)
    return (plane, False)


def plane_warnings(plane):
    nx, ny, nz, off = plane
    warnings = []
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if abs(length - 1.0) > 0.05:
        warnings.append("The plane normal is not a unit vector (length %.3f)." % length)
    if abs(nz) < 0.9:
        warnings.append("The plane normal points far from straight down the camera axis "
                        "(z component %.3f). Is the camera looking at the sand from above?" % nz)
    if not (30.0 <= abs(off) <= 400.0):
        warnings.append("The plane offset %.1f cm is outside the usual 30 to 400 cm camera height." % off)
    return warnings


def corner_warnings(corners, plane=None):
    """corners: list of 4 (x, y, z) in the order LL, LR, UL, UR."""
    warnings = []
    if len(corners) != 4:
        warnings.append("Expected 4 corners, got %d." % len(corners))
        return warnings
    ll, lr, ul, ur = corners
    for i in range(4):
        for j in range(i + 1, 4):
            if dist(corners[i], corners[j]) < 5.0:
                warnings.append("Corners %s and %s are less than 5 cm apart."
                                % (CORNER_NAMES[i], CORNER_NAMES[j]))
    if not (ll[0] < lr[0] and ul[0] < ur[0] and ll[1] < ul[1] and lr[1] < ur[1]):
        warnings.append("The corners do not look like they were clicked in the order "
                        "lower-left, lower-right, upper-left, upper-right. "
                        "Use 'Auto-order' or redo the phase.")
    if plane is not None:
        nx, ny, nz, off = plane
        for name, c in zip(CORNER_NAMES, corners):
            height = c[0] * nx + c[1] * ny + c[2] * nz - off
            if abs(height) > 30.0:
                warnings.append("Corner %s is %.1f cm away from the base plane. "
                                "Corners should be clicked on the sand surface." % (name, height))
    return warnings


def dist(a, b):
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def corner_issue(corners, plane=None):
    """One short sentence about the most important problem with 4 corners, or None.
    Kept short because Vrui wraps popup text at about 40 characters."""
    if len(corners) != 4:
        return None
    for i in range(4):
        for j in range(i + 1, 4):
            if dist(corners[i], corners[j]) < 5.0:
                return "%s and %s are almost the same point." % (CORNER_NAMES[i], CORNER_NAMES[j])
    ll, lr, ul, ur = corners
    if not (ll[0] < lr[0] and ul[0] < ur[0] and ll[1] < ul[1] and lr[1] < ur[1]):
        return "the order does not look like lower-left, lower-right, upper-left, upper-right."
    if plane is not None:
        nx, ny, nz, off = plane
        for name, c in zip(CORNER_NAMES, corners):
            height = c[0] * nx + c[1] * ny + c[2] * nz - off
            if abs(height) > 30.0:
                return "%s is %.0f cm off the base plane." % (name, abs(height))
    return None


def auto_order_corners(corners):
    """Sort 4 points into LL, LR, UL, UR using camera-space x (right) and y (up)."""
    pts = sorted(corners, key=lambda p: p[1])
    lower = sorted(pts[:2], key=lambda p: p[0])
    upper = sorted(pts[2:], key=lambda p: p[0])
    return [lower[0], lower[1], upper[0], upper[1]]


def format_plane(plane):
    nx, ny, nz, off = plane
    return "(%.6g, %.6g, %.6g), %.3f" % (nx, ny, nz, off)


def format_point(p):
    return "(%.4f, %.4f, %.4f)" % tuple(p)


def short_plane(plane):
    nx, ny, nz, off = plane
    return "(%.4f, %.4f, %.4f), %.1f" % (nx, ny, nz, off)


def short_point(p):
    return "(%.1f, %.1f, %.1f)" % tuple(p)


def read_box_layout(path):
    """Return (plane, corners) or raise ValueError."""
    with open(path) as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    if len(lines) < 5:
        raise ValueError("%s has %d non-empty lines, expected 5" % (path, len(lines)))
    m = LAYOUT_PLANE_RE.match(lines[0])
    if not m:
        raise ValueError("Cannot parse plane equation in %s: %r" % (path, lines[0]))
    plane = tuple(float(m.group(i)) for i in range(1, 5))
    corners = []
    for l in lines[1:5]:
        p = parse_point_line(l)
        if p is None:
            raise ValueError("Cannot parse corner in %s: %r" % (path, l))
        corners.append(p)
    return plane, corners


def write_box_layout(path, plane, corners):
    text = format_plane(plane) + "\n" + "\n".join(format_point(c) for c in corners) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# heightMapPlane in SARndbox.cfg: the plane the colour map is measured from.
# SARndbox reads it at startup from "section SARndbox" and also accepts it live
# on the control pipe. Keeping it a few cm above or below the base plane moves
# the colour bands (sea level) without touching the camera calibration.
# --------------------------------------------------------------------------

HMP_TAG_RE = re.compile(r"^(\s*)heightMapPlane\b\s*(.*?)\s*$")
SECTION_RE = re.compile(r"^\s*section\s+(\S+)\s*$")
ENDSECTION_RE = re.compile(r"^\s*endsection\s*$")
HMP_COMMENT = "# Colour map height (sea level), written by Calibrate Sandbox"
DEFAULT_SANDBOX_CFG = """# Configuration file for SARndbox application
section SARndbox
\tsection Camera
\t\t# Configuration parameters for Kinect v1
\t\tcompressDepthFrames true
\t\tsmoothDepthFrames false
\tendsection
endsection
"""


def _cfg_root_section(lines):
    """Locate 'section SARndbox' at depth 0. Returns (start, end, tag_lines, comment_lines)."""
    depth = 0
    start = end = None
    tags, comments = [], []
    for i, line in enumerate(lines):
        code = line.split("#", 1)[0]
        if SECTION_RE.match(code):
            if depth == 0 and SECTION_RE.match(code).group(1) == "SARndbox" and start is None:
                start = i
            depth += 1
        elif ENDSECTION_RE.match(code):
            depth -= 1
            if depth == 0 and start is not None and end is None:
                end = i
        elif start is not None and end is None and depth == 1:
            if HMP_TAG_RE.match(code):
                tags.append(i)
            elif line.strip() == HMP_COMMENT:
                comments.append(i)
    return start, end, tags, comments


def read_height_map_plane(path):
    """Return the heightMapPlane from SARndbox.cfg as (nx, ny, nz, off), or None."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    start, end, tags, comments = _cfg_root_section(lines)
    if not tags:
        return None
    value = HMP_TAG_RE.match(lines[tags[-1]].split("#", 1)[0]).group(2)
    m = LAYOUT_PLANE_RE.match(value)
    if not m:
        return None
    return tuple(float(m.group(i)) for i in range(1, 5))


def write_height_map_plane(path, plane):
    """Set (plane given) or remove (plane None) the heightMapPlane tag in SARndbox.cfg.
    Other settings and comments in the file are left untouched."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = DEFAULT_SANDBOX_CFG.splitlines()
    start, end, tags, comments = _cfg_root_section(lines)
    if start is None or end is None:
        lines += ["section SARndbox", "endsection"]
        start, end, tags, comments = _cfg_root_section(lines)
    remove = sorted(tags + comments)
    insert_at = remove[0] if remove else end
    for i in reversed(remove):
        del lines[i]
    if plane is not None:
        lines[insert_at:insert_at] = ["\t" + HMP_COMMENT, "\theightMapPlane " + format_plane(plane)]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def height_map_plane_for(base_plane, delta):
    """The colour plane for a sea-level offset of `delta` cm above the base plane."""
    nx, ny, nz, off = base_plane
    return (nx, ny, nz, off + delta)


def backup_file(path, backup_dir):
    """Copy path into backup_dir with a timestamp. Returns the backup path or None."""
    if not os.path.isfile(path):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir, "%s.%s" % (os.path.basename(path), stamp))
    shutil.copy2(path, dest)
    return dest


def files_equal(a, b):
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


# --------------------------------------------------------------------------
# Persistent state (what the wizard did and when)
# --------------------------------------------------------------------------

class State:
    def __init__(self, path):
        self.path = path
        self.data = {}
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def mark(self, key, **info):
        info["done"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self.data[key] = info
        self.save()

    def get(self, key):
        return self.data.get(key)

    def clear(self):
        self.data = {}
        self.save()


# --------------------------------------------------------------------------
# System helpers
# --------------------------------------------------------------------------

def detect_resolution(default=(1024, 768)):
    """Read the current screen mode from xrandr. Returns (w, h)."""
    try:
        out = subprocess.run(["xrandr", "--current"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return default
    for line in out.splitlines():
        m = re.match(r"\s+(\d+)x(\d+)\s+[\d.]+\*", line)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"current (\d+) x (\d+)", out)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return default


XRANDR_OUTPUT_RE = re.compile(r"^(\S+) connected(?: primary)? \d+x\d+\+\d+\+\d+(?: (normal|left|inverted|right))? \(")
FLIPPED_ROTATION = {"normal": "inverted", "inverted": "normal", "left": "right", "right": "left"}


def detect_display():
    """Return (output name, rotation) of the first connected xrandr output, or (None, 'normal')."""
    try:
        out = subprocess.run(["xrandr", "--current"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None, "normal"
    for line in out.splitlines():
        m = XRANDR_OUTPUT_RE.match(line)
        if m:
            return m.group(1), (m.group(2) or "normal")
    return None, "normal"


def set_display_rotation(output, rotation, log):
    """Rotate the given xrandr output. Used to flip the whole screen upside down."""
    argv = ["xrandr", "--output", output, "--rotate", rotation]
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                           timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.write("Could not run xrandr: %s" % e)
        return False
    if r.returncode != 0:
        log.write("xrandr failed (%s): %s" % (" ".join(argv), r.stdout.strip()))
        return False
    log.write("Display %s rotated to %s" % (output, rotation))
    return True


def sandbox_pids(process_name):
    try:
        out = subprocess.run(["pgrep", "-x", process_name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in out.split() if p.isdigit()]


def stop_sandbox(process_name, log, timeout=6.0):
    """Terminate the running sandbox. Returns True when no process is left."""
    pids = sandbox_pids(process_name)
    if not pids:
        return True
    log.write("Stopping %s (pid %s)" % (process_name, ", ".join(map(str, pids))))
    subprocess.run(["pkill", "-TERM", "-x", process_name])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not sandbox_pids(process_name):
            return True
        time.sleep(0.25)
    subprocess.run(["pkill", "-KILL", "-x", process_name])
    time.sleep(0.5)
    return not sandbox_pids(process_name)


def launch_sandbox(paths, log):
    log.write("Launching sandbox: %s" % paths.run_sandbox)
    try:
        logf = open(paths.log_file, "a")
    except OSError:
        logf = subprocess.DEVNULL
    subprocess.Popen([paths.run_sandbox], cwd=paths.sarndbox_dir, stdin=subprocess.DEVNULL,
                     stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)


def send_control_command(fifo, command, log):
    """Write one command into the running sandbox's control pipe (non-blocking)."""
    if not os.path.exists(fifo):
        return False
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        log.write("Control pipe not writable (%s); is the sandbox running?" % e)
        return False
    try:
        os.write(fd, (command.strip() + "\n").encode())
        log.write("Sent to control pipe: %s" % command.strip())
        return True
    except OSError as e:
        log.write("Control pipe write failed: %s" % e)
        return False
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Running a Vrui tool with a stdin command channel
# --------------------------------------------------------------------------

class ToolRunner:
    def __init__(self, argv, cwd, log):
        self.argv = argv
        self.log = log
        self.lines = queue.Queue()
        self.eof = False
        self.helper = False  # set once the SandboxHelper plugin reports itself loaded
        log.write("Running: %s" % " ".join(argv))
        self.proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1,
                                     start_new_session=True)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.lines.put(line.rstrip("\r\n"))
        finally:
            self.lines.put(None)

    def send(self, command):
        """Send one Vrui console command (single line)."""
        command = " ".join(command.split())
        try:
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.flush()
            self.log.write("Sent to %s: %s" % (os.path.basename(self.argv[0]), command), echo=False)
        except (BrokenPipeError, OSError, ValueError):
            pass

    def show_message(self, text):
        """Pop a message up in the tool window. With the SandboxHelper plugin loaded the new
        popup replaces the previous one instead of stacking on top of it."""
        self.send(("sandboxMessage " if self.helper else "showMessage ") + text)

    def running(self):
        return self.proc.poll() is None

    def stop(self, grace=3.0):
        if not self.running():
            return
        self.send("quit")
        deadline = time.time() + grace
        while time.time() < deadline and self.running():
            time.sleep(0.1)
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def returncode(self):
        return self.proc.poll()


# --------------------------------------------------------------------------
# Sessions: what to tell the user and how to interpret the tool's output
# --------------------------------------------------------------------------

class Session:
    """Base class. Subclasses define argv(), intro text, messages, on_line()."""
    tool_name = ""
    phases = ()

    def __init__(self, helper_expected=False):
        self.error = None
        self.status = "Starting..."
        self.helper_expected = helper_expected  # the SandboxHelper plugin is installed and gets loaded
        self.helper = False                     # ...and has reported itself loaded
        self.helper_failed = False
        self.awaiting_helper_since = None       # set when the first popup waits for the plugin's report
        self.commands = []                      # console commands for the tool, sent by the App

    def vislet_args(self, paths):
        return ["-vislet", "SandboxHelper", ";"] if getattr(paths, "vislet", "") else []

    def take_commands(self):
        commands, self.commands = self.commands, []
        return commands

    def helper_line(self, line):
        """Handle the plugin's load/failure lines. Returns (handled, message)."""
        if HELPER_LOADED_RE.search(line):
            self.helper = True
            return True, self.on_helper_loaded()
        if HELPER_IGNORED_RE.search(line):
            self.helper_failed = True
            return True, self.on_helper_failed()
        return False, None

    def on_helper_loaded(self):
        """The plugin is in: show the first instructions through it."""
        return self.initial_message()

    def on_helper_failed(self):
        """No plugin after all: show the first instructions the plain Vrui way."""
        return self.initial_message()

    def reminder(self):
        """Instructions for where the user is right now (the 'Send instructions again' button)."""
        return self.initial_message()

    def explain_error(self, text):
        t = text.lower()
        if "fewer than" in t or "not found" in t or "no such device" in t:
            return ("No 3D camera was found. Check that the Kinect is plugged in and "
                    "powered, then try again.")
        if "busy" in t or "access" in t or "permission" in t:
            return ("The 3D camera is in use or not accessible. If the sandbox is still "
                    "running, stop it first. Otherwise unplug and re-plug the camera.")
        return None


class KinectSession(Session):
    tool_name = "RawKinectViewer"

    def __init__(self, phases, helper_expected=False):
        Session.__init__(self, helper_expected)
        self.phases = tuple(sorted(set(phases)))
        self.connected = False
        self.averaging = False      # the plugin is capturing the average depth frame
        self.average_ready = False
        self.captures = 0
        self.plane = None
        self.plane_rms = None
        self.plane_flipped = False
        self.points = []
        self.serial = None
        self._phase2_prompted = False

    def title(self):
        if self.phases == (1, 2):
            return "Phases 1 and 2: base plane and box corners"
        if self.phases == (1,):
            return "Phase 1: base plane"
        return "Phase 2: box corners"

    def argv(self, paths):
        return [paths.raw_kinect_viewer, "-compress", "0"] + self.vislet_args(paths)

    def intro_sections(self):
        """List of (heading, text). The last entry is shown as the closing note."""
        sections = [("Get ready",
                     "\u2022 Flatten and smooth the sand as evenly as you can.\n"
                     "\u2022 Nothing but sand inside the box: no hands, tools or toys.\n"
                     "\u2022 RawKinectViewer opens full screen. Work in the LEFT half (the depth image) "
                     "and ignore the RIGHT half (the color camera).\n"
                     "\u2022 Instructions pop up inside that window as you go. Click 'Jolly Good!' to dismiss them.")]
        if 1 in self.phases and self.helper_expected:
            sections.append(("Phase 1 \u00b7 Base plane",
                             "1. The wizard captures the flat sand by itself right after the window opens "
                             "('Capturing average depth frame...' shows for about 5 seconds). Keep hands out "
                             "until the popup says the sand is captured.\n"
                             "2. Hold down the 1 key and drag a rectangle over a large, flat area of sand in the "
                             "LEFT image, then release the key. Stay inside the sand.\n"
                             "Not happy? Drag again: the last rectangle counts. Touched the sand? Click "
                             "'Capture the sand again' in this window first."))
        elif 1 in self.phases:
            sections.append(("Phase 1 \u00b7 Base plane",
                             "1. Press and hold the RIGHT mouse button, move onto 'Average Frames' in the menu "
                             "that pops up, and release. Wait until 'Capturing average depth frame...' disappears.\n"
                             "2. Hold down the 1 key and drag a rectangle over a large, flat area of sand in the "
                             "LEFT image, then release the key. Stay inside the sand.\n"
                             "Not happy? Drag again: the last rectangle counts."))
        if 2 in self.phases:
            sections.append(("Phase 2 \u00b7 Box corners",
                             "Point at each corner of the sand surface in the LEFT image and press the 2 key, "
                             "in this order:\n"
                             "      lower-left \u2192 lower-right \u2192 upper-left \u2192 upper-right\n"
                             "A popup confirms each corner and names the next one. No popup? The camera has no "
                             "depth reading at that pixel (it shows black): move a little further onto the sand "
                             "and press 2 again.\n"
                             "Misclicked? Keep going: the last 4 clicks count."))
        sections.append(("When you are done", "Press Esc to close RawKinectViewer and come back here."))
        return sections

    def intro_text(self, ctx=None):
        return "\n\n".join("%s\n%s" % sec for sec in self.intro_sections())

    DRAG_PROMPT = ("Hold down key 1 and drag a box over a large, flat area of sand in the LEFT image, "
                   "then release the key.")

    def initial_message(self):
        if self.helper_expected:
            phase = "PHASE 1 - BASE PLANE" if 1 in self.phases else "PHASE 2 - CORNERS"
            return ("%s: starting up. The flat sand is captured automatically in a moment: keep "
                    "hands and tools out of the box." % phase)
        return self.manual_message()

    def manual_message(self):
        """Instructions for the case without the SandboxHelper plugin."""
        if 1 in self.phases:
            return ("PHASE 1 - BASE PLANE: hold the right mouse button, pick Average Frames, release. "
                    "Wait for the capture. Then hold key 1 and drag a box over flat sand in the LEFT image.")
        return self._corner_prompt("PHASE 2 - CORNERS: ")

    def request_average(self):
        """Ask the plugin to (re)capture the average depth frame. Returns the popup text."""
        self.commands.append("sandboxAverage on")
        self.averaging = True
        self.average_ready = False
        self.captures += 1
        self.status = "Capturing the flat sand (about 5 seconds)..."
        again = "again " if self.captures > 1 else ""
        return ("Capturing the flat sand %sfor about 5 seconds. Keep hands and tools out of the box."
                % again)

    def on_helper_loaded(self):
        if self.connected:
            return self.request_average()
        return self.initial_message()

    def on_helper_failed(self):
        self.status = "The SandboxHelper plugin did not load: pick Average Frames by hand."
        return self.manual_message()

    def after_average(self):
        """Popup text once the plugin reports the average frame."""
        self.averaging = False
        self.average_ready = True
        self.status = "Sand captured. Waiting for you..."
        if 1 in self.phases and self.plane is None:
            return "Sand captured. " + self.DRAG_PROMPT
        if 1 in self.phases:
            return ("Sand captured again. Drag again with key 1 to redo the base plane (the last one "
                    "counts), or carry on.")
        return self._corner_prompt("Sand captured. PHASE 2 - CORNERS: ")

    NO_POPUP_HINT = ("No popup? The camera has no depth at that pixel (it shows black): move a bit "
                     "further onto the sand and press 2 again.")
    ALL_DONE = ("Press Esc to finish, or start again from the lower-left corner: the last 4 count.")

    def _corner_prompt(self, prefix):
        return (prefix + "press key 2 on each corner of the sand in the LEFT image, starting "
                "LOWER-LEFT, then lower-right, upper-left, upper-right. A popup confirms each "
                "corner and names the next one. " + self.NO_POPUP_HINT)

    def corner_message(self):
        """Popup text after the corner press that was just parsed."""
        n = len(self.points)
        i = (n - 1) % 4
        got = "Corner %d of 4 (%s) captured at %s." % (i + 1, CORNER_NAMES[i], short_point(self.points[-1]))
        if n > 4 and i == 0:
            got = "Starting over. " + got
        if i < 3:
            return "%s NEXT: press 2 on the %s corner." % (got, CORNER_NAMES[i + 1].upper())
        issue = corner_issue(self.corners(), self.plane)
        if issue:
            return "%s All 4 corners done, but check this: %s %s" % (got, issue, self.ALL_DONE)
        return "%s All 4 corners done. %s" % (got, self.ALL_DONE)

    def _corner_status(self):
        n = len(self.points)
        i = (n - 1) % 4
        if i < 3:
            return "Corner %d of 4 (%s) captured. Next: %s" % (i + 1, CORNER_NAMES[i], CORNER_NAMES[i + 1])
        issue = corner_issue(self.corners(), self.plane)
        text = "All 4 corners captured. Press Esc in RawKinectViewer to finish."
        if issue:
            text += " Check: " + issue
        return text

    def reminder(self):
        if self.helper and self.averaging:
            return "Capturing the flat sand: keep hands and tools out of the box for a few seconds."
        if 1 in self.phases and self.plane is None:
            if self.helper and self.average_ready:
                return "Sand captured. " + self.DRAG_PROMPT
            return self.manual_message() if (self.helper_failed or not self.helper_expected) else self.initial_message()
        if 2 in self.phases:
            n = len(self.points)
            if n == 0:
                return self._corner_prompt("PHASE 2 - CORNERS: ")
            if n % 4:
                return ("%d of 4 corners so far. NEXT: press 2 on the %s corner. %s"
                        % (n % 4, CORNER_NAMES[n % 4].upper(), self.NO_POPUP_HINT))
            return "All 4 corners captured. " + self.ALL_DONE
        return ("Base plane captured (offset %.1f cm). Press Esc to finish, or hold key 1 and drag "
                "again to redo: the last one counts." % self.plane[3])

    def on_line(self, line):
        """Return a message to show inside the tool window, or None."""
        handled, message = self.helper_line(line)
        if handled:
            return message
        m = HELPER_AVERAGE_RE.search(line)
        if m:
            what = m.group(1)
            if what.startswith("average frame ready"):
                return self.after_average()
            if what.startswith("error"):
                self.averaging = False
                self.status = "The plugin could not press Average Frames: pick it by hand."
                return self.manual_message()
            if what == "Average Frames off":
                self.averaging = False
                self.average_ready = False
            return None
        m = CONNECTED_RE.search(line)
        if m:
            self.serial = m.group(1)
            self.connected = True
            self.status = "Camera %s connected. Waiting for you..." % self.serial
            if self.helper:
                return self.request_average()
            return None
        m = EXCEPTION_RE.search(line)
        if m:
            self.error = m.group(1)
            self.status = "RawKinectViewer stopped: " + self.error
            return None
        m = PLANE_RMS_RE.search(line)
        if m:
            self.plane_rms = float(m.group(1))
            return None
        plane = parse_plane_line(line)
        if plane is not None:
            self.plane, self.plane_flipped = normalize_plane(plane)
            self.status = "Base plane captured: " + format_plane(self.plane)
            if 1 not in self.phases:
                return None
            if 2 in self.phases and not self._phase2_prompted:
                self._phase2_prompted = True
                return self._corner_prompt("Plane captured (offset %.1f cm). PHASE 2 - CORNERS: "
                                           % self.plane[3])
            if 2 not in self.phases:
                return ("Plane captured (offset %.1f cm). Press Esc to finish, or drag again "
                        "to redo. The last one counts." % self.plane[3])
            return None
        point = parse_point_line(line)
        if point is not None:
            self.points.append(point)
            if 2 not in self.phases:
                n = len(self.points)
                self.status = "%d corner click%s so far (not part of this phase)" % (n, "" if n == 1 else "s")
                return None
            self.status = self._corner_status()
            return self.corner_message()
        return None

    def corners(self):
        return self.points[-4:] if len(self.points) >= 4 else list(self.points)

    def missing(self):
        out = []
        if 1 in self.phases and self.plane is None:
            out.append("no base plane was captured")
        if 2 in self.phases and len(self.points) < 4:
            out.append("only %d of 4 corners were clicked" % len(self.points))
        return out


class ProjectorSession(Session):
    tool_name = "CalibrateProjector"
    phases = (3,)

    def __init__(self, width, height, helper_expected=False):
        Session.__init__(self, helper_expected)
        self.width = width
        self.height = height
        self.tie_points = 0
        self.rms = None
        self.calib_error = None
        self.started_at = time.time()

    def title(self):
        return "Phase 3: projector calibration"

    def argv(self, paths):
        return ([paths.calibrate_projector, "-s", str(self.width), str(self.height),
                 "-slf", paths.box_layout, "-pmf", paths.projector_matrix] + self.vislet_args(paths))

    def intro_sections(self):
        return [("Get ready",
                 "\u2022 The projector image should fill the sandbox. Use 'Show alignment grid' to check.\n"
                 "\u2022 Have the calibration disk ready: a CD or DVD covered with white paper and a cross "
                 "drawn through the exact center.\n"
                 "\u2022 Smooth the sand. Keep hands out of the box while a point is captured."),
                ("What you will see",
                 "CalibrateProjector opens full screen and first flashes RED while it captures the empty "
                 "sand (the background). Then it shows a white cross: the next target."),
                ("For each of the %d points" % NUM_TIE_POINTS,
                 "1. Hold the disk flat where the white cross is projected, or lay it on the sand there. "
                 "A green circle means the camera sees the disk.\n"
                 "2. Press the 1 key and hold still until the cross moves on.\n"
                 "Vary the height: hold the disk higher for some points and dig a hole to put it lower for "
                 "others. That gives the calibration its depth information. After changing the sand, take "
                 "your hands out and press the 2 key to re-capture the background (the screen flashes red). "
                 "A red screen right after pressing 1 means you pressed the wrong key."),
                ("After the last point",
                 "The calibration is computed and saved automatically. Move the disk around: a red crosshair (two lines "
                 "across the whole screen) should meet at its center. Press Esc to finish.")]

    def intro_text(self, ctx=None):
        return "\n\n".join("%s\n%s" % sec for sec in self.intro_sections())

    def initial_message(self):
        return ("PHASE 3 - PROJECTOR: hold the disk where the white cross is and press key 1. "
                "Repeat for all %d points. Press key 2 after changing the sand." % NUM_TIE_POINTS)

    def reminder(self):
        if self.rms is not None:
            return ("Calibration done. Move the disk around: the red crosshair should follow it. "
                    "Press Esc to finish.")
        if self.tie_points == 0:
            return self.initial_message()
        return ("%d of %d points captured. Hold the disk where the white cross is and press key 1 "
                "for the next one. Press key 2 after changing the sand." % (self.tie_points, NUM_TIE_POINTS))

    def on_line(self, line):
        handled, message = self.helper_line(line)
        if handled:
            return message
        m = EXCEPTION_RE.search(line)
        if m:
            self.error = m.group(1)
            self.status = "CalibrateProjector stopped: " + self.error
            return None
        if BACKGROUND_RE.search(line):
            self.status = "Capturing the background sand surface..."
            return None
        if TIEPOINT_DONE_RE.search(line):
            self.tie_points += 1
            self.status = "Tie point %d of %d captured" % (self.tie_points, NUM_TIE_POINTS)
            if self.tie_points == NUM_TIE_POINTS // 2:
                return "Halfway there: %d of %d points captured." % (self.tie_points, NUM_TIE_POINTS)
            return None
        m = RMS_RE.search(line)
        if m:
            self.rms = float(m.group(1))
            self.status = "Calibration computed. RMS residual %.2f pixels." % self.rms
            return ("Calibration done. RMS residual %.2f pixels. Move the disk around: the red "
                    "crosshair should follow it. Press Esc to finish." % self.rms)
        m = CALIB_ERROR_RE.search(line)
        if m:
            self.calib_error = m.group(1).strip()
            self.status = "Calibration failed: " + self.calib_error
            return ("Calibration FAILED: some points were bad. Press Esc, then run Phase 3 "
                    "again from scratch.")
        return None


# --------------------------------------------------------------------------
# Tk user interface
# --------------------------------------------------------------------------

PALETTE = {
    "bg": "#f3f4f6", "card": "#ffffff", "border": "#e5e7eb", "border_strong": "#d1d5db",
    "text": "#111827", "muted": "#6b7280", "faint": "#9ca3af",
    "accent": "#2563eb", "accent_hover": "#1d4ed8", "accent_soft": "#93c5fd", "accent_text": "#ffffff",
    "good": "#16a34a", "good_bg": "#dcfce7", "good_text": "#166534",
    "warn": "#d97706", "warn_bg": "#fef3c7", "warn_text": "#92400e",
    "bad": "#dc2626", "bad_bg": "#fee2e2", "bad_text": "#991b1b",
    "info_bg": "#dbeafe", "info_text": "#1e40af",
    "muted_bg": "#e5e7eb", "muted_text": "#374151",
    "log_bg": "#111827", "log_fg": "#d1d5db",
}

PHASE_NAMES = {1: "Base plane", 2: "Box corners", 3: "Projector"}


def build_app(paths, log=None, state=None):
    """Build and return the Tk application class bound to the given paths."""
    import tkinter as tk
    from tkinter import ttk, messagebox, font as tkfont

    log = log or Log(paths.log_file)
    state = state or State(paths.state_file)
    C = PALETTE
    TINTS = {
        "good": (C["good_bg"], C["good_text"]),
        "warn": (C["warn_bg"], C["warn_text"]),
        "bad": (C["bad_bg"], C["bad_text"]),
        "info": (C["info_bg"], C["info_text"]),
        "muted": (C["muted_bg"], C["muted_text"]),
    }

    class App(tk.Tk):
        def __init__(self):
            tk.Tk.__init__(self)
            self.title(APP_TITLE)
            self.minsize(900, 640)
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                self.geometry("1000x720")
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.configure(bg=C["bg"])
            self._setup_fonts(tkfont)
            self._setup_styles(ttk)

            self.queue = []          # phases still to run
            self.session = None
            self.runner = None
            self.body = tk.Frame(self, bg=C["bg"], padx=28, pady=22)
            self.body.pack(fill="both", expand=True)
            self.log_lines = []
            try:
                with open(paths.log_file) as f:
                    self.log_lines = [l.rstrip("\n") for l in f.readlines()[-30:]]
            except OSError:
                pass
            log.listeners.append(self._on_log_line)
            self.log_widget = None
            self.pulse_canvas = None
            self._sandbox_bar_key = None
            self._last_bar_refresh = 0.0
            self.resolution = detect_resolution()
            self.display_output, self.display_rotation_orig = detect_display()
            self.flip_wanted = False
            self.xbg_proc = None
            self.show_hub()
            self.after(200, self._poll)

        # ---------------- look and feel ----------------
        def _setup_fonts(self, tkfont):
            families = set(tkfont.families())
            default_family = tkfont.nametofont("TkDefaultFont").cget("family")
            ui = next((f for f in ("Inter", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans",
                                   "Liberation Sans") if f in families), default_family)
            mono = next((f for f in ("JetBrains Mono", "Ubuntu Mono", "DejaVu Sans Mono", "Noto Sans Mono",
                                     "Liberation Mono") if f in families),
                        tkfont.nametofont("TkFixedFont").cget("family"))

            def F(size, weight="normal"):
                return tkfont.Font(family=ui, size=size, weight=weight)

            self.f_title = F(22, "bold")
            self.f_display = F(34, "bold")
            self.f_h2 = F(15, "bold")
            self.f_body = F(12)
            self.f_body_b = F(12, "bold")
            self.f_small = F(10)
            self.f_small_b = F(10, "bold")
            self.f_btn = F(12, "bold")
            self.f_mono = tkfont.Font(family=mono, size=12)
            self.f_mono_small = tkfont.Font(family=mono, size=10)
            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
                tkfont.nametofont(name).configure(family=ui, size=12)

        def _setup_styles(self, ttk):
            st = ttk.Style(self)
            try:
                st.theme_use("clam")
            except Exception:
                pass
            st.configure(".", background=C["bg"], foreground=C["text"], font=self.f_body)
            st.configure("TFrame", background=C["bg"])
            st.configure("TLabel", background=C["bg"], foreground=C["text"])
            # Buttons: flat, no focus ring, hover colours
            st.configure("Primary.TButton", background=C["accent"], foreground=C["accent_text"],
                         bordercolor=C["accent"], lightcolor=C["accent"], darkcolor=C["accent"],
                         borderwidth=0, focusthickness=0, focuscolor=C["accent"], padding=(20, 11),
                         font=self.f_btn)
            st.map("Primary.TButton",
                   background=[("disabled", C["accent_soft"]), ("pressed", C["accent_hover"]),
                               ("active", C["accent_hover"])],
                   bordercolor=[("active", C["accent_hover"])],
                   lightcolor=[("active", C["accent_hover"])], darkcolor=[("active", C["accent_hover"])])
            st.configure("Secondary.TButton", background=C["card"], foreground=C["text"],
                         bordercolor=C["border_strong"], lightcolor=C["card"], darkcolor=C["card"],
                         borderwidth=1, focusthickness=0, focuscolor=C["card"], padding=(16, 10),
                         font=self.f_body_b)
            st.map("Secondary.TButton",
                   background=[("pressed", C["border"]), ("active", "#f9fafb")],
                   bordercolor=[("active", C["faint"])])
            st.configure("Small.Secondary.TButton", padding=(12, 6), font=self.f_small_b)
            st.configure("Small.Primary.TButton", padding=(12, 6), font=self.f_small_b)
            st.configure("Danger.TButton", background=C["card"], foreground=C["bad"],
                         bordercolor="#fecaca", lightcolor=C["card"], darkcolor=C["card"],
                         borderwidth=1, focusthickness=0, focuscolor=C["card"], padding=(16, 10),
                         font=self.f_body_b)
            st.map("Danger.TButton", background=[("active", C["bad_bg"])], bordercolor=[("active", C["bad"])])
            # Check buttons and entries on white cards
            st.configure("Card.TCheckbutton", background=C["card"], foreground=C["text"],
                         focuscolor=C["card"], indicatorbackground=C["card"], indicatorforeground=C["accent"],
                         indicatorcolor=C["card"], indicatorsize=16, indicatormargin=(2, 2, 6, 2), padding=4)
            st.map("Card.TCheckbutton", background=[("active", C["card"])],
                   indicatorbackground=[("selected", C["accent"]), ("active", C["card"])],
                   indicatorforeground=[("selected", C["accent_text"])],
                   indicatorcolor=[("selected", C["accent"])])
            st.configure("TEntry", fieldbackground=C["card"], background=C["card"], foreground=C["text"],
                         bordercolor=C["border_strong"], lightcolor=C["card"], darkcolor=C["card"],
                         insertcolor=C["text"], padding=(8, 6))
            st.map("TEntry", bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["accent"])],
                   darkcolor=[("focus", C["accent"])])
            st.configure("Horizontal.TScale", background=C["accent"], troughcolor=C["border"],
                         bordercolor=C["accent"], lightcolor=C["accent"], darkcolor=C["accent"],
                         sliderlength=30, sliderthickness=22, gripcount=0)
            st.map("Horizontal.TScale", background=[("active", C["accent_hover"])],
                   bordercolor=[("active", C["accent_hover"])], lightcolor=[("active", C["accent_hover"])],
                   darkcolor=[("active", C["accent_hover"])])
            st.configure("Log.Vertical.TScrollbar", background="#374151", troughcolor=C["log_bg"],
                         bordercolor=C["log_bg"], arrowcolor=C["faint"], lightcolor="#374151",
                         darkcolor="#374151", gripcount=0)
            st.map("Log.Vertical.TScrollbar", background=[("active", "#4b5563")])

        # ---------------- building blocks ----------------
        def clear(self):
            for w in self.body.winfo_children():
                w.destroy()
            self.log_widget = None
            self.pulse_canvas = None
            self._sandbox_bar_key = None

        def card(self, parent, padx=20, pady=16, fill="x", expand=False, gap=(0, 12), side=None):
            outer = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"],
                             highlightcolor=C["border"], bd=0)
            if side:
                outer.pack(side=side, fill=fill, expand=expand, pady=gap, padx=(0, 12))
            else:
                outer.pack(fill=fill, expand=expand, pady=gap)
            inner = tk.Frame(outer, bg=C["card"], padx=padx, pady=pady)
            inner.pack(fill="both", expand=True)
            return inner

        def label(self, parent, text, font=None, fg=None, bg=None, **kw):
            bg = bg or parent.cget("background")
            return tk.Label(parent, text=text, font=font or self.f_body, fg=fg or C["text"], bg=bg,
                            justify="left", **kw)

        def pill(self, parent, text, kind="muted"):
            bg, fg = TINTS[kind]
            return tk.Label(parent, text=text, bg=bg, fg=fg, font=self.f_small_b, padx=10, pady=3)

        def notice(self, parent, text, kind, prefix="", pack=True):
            bg, fg = TINTS[kind]
            f = tk.Frame(parent, bg=bg)
            if pack:
                f.pack(fill="x", pady=(0, 8))
            tk.Label(f, text=prefix + text, bg=bg, fg=fg, font=self.f_body, wraplength=880, justify="left",
                     padx=14, pady=10).pack(anchor="w")
            return f

        def header(self, title, subtitle=None):
            row = tk.Frame(self.body, bg=C["bg"])
            row.pack(fill="x")
            right = tk.Frame(row, bg=C["bg"])
            right.pack(side="right", anchor="ne", padx=(16, 0))
            if self.display_output:
                self.flip_button = ttk.Button(right, command=self.flip_toggle)
                self.flip_button.pack(side="right", padx=(10, 0))
                self.refresh_flip_button()
            status = tk.Frame(right, bg=C["bg"])
            status.pack(side="left")
            left = tk.Frame(row, bg=C["bg"])
            left.pack(side="left", fill="x", expand=True)
            labels = [self.label(left, title, font=self.f_title, wraplength=760)]
            labels[0].pack(anchor="w")
            if subtitle:
                labels.append(self.label(left, subtitle, fg=C["muted"], wraplength=760))
                labels[1].pack(anchor="w", pady=(2, 0))

            def rewrap(event, labels=labels):
                for l in labels:
                    if l.winfo_exists():
                        l.configure(wraplength=max(200, event.width - 4))
            left.bind("<Configure>", rewrap)
            tk.Frame(self.body, bg=C["bg"], height=14).pack()
            return status

        # ---------------- flip view (rotate the projector output 180 degrees) ----------------
        def refresh_flip_button(self):
            b = getattr(self, "flip_button", None)
            if b is None or not b.winfo_exists():
                return
            if self.flip_wanted:
                b.configure(text="\u21c5  View flipped", style="Small.Primary.TButton")
            else:
                b.configure(text="\u21c5  Flip view", style="Small.Secondary.TButton")

        def flip_toggle(self):
            self.flip_wanted = not self.flip_wanted
            if not self.apply_flip():
                self.flip_wanted = False
                messagebox.showerror(APP_TITLE, "The display could not be rotated. See the log for the xrandr error.")
            self.refresh_flip_button()

        def apply_flip(self, tool_starting=False, sandbox_starting=False):
            """Flip the screen when wanted, but never while a Vrui tool, XBackground or the
            sandbox is running: their output must stay in the calibrated orientation."""
            if not self.display_output:
                return False
            busy = (tool_starting or sandbox_starting
                    or (self.runner is not None and self.runner.running())
                    or (self.xbg_proc is not None and self.xbg_proc.poll() is None)
                    or bool(sandbox_pids(paths.sandbox_process)))
            if self.flip_wanted and not busy:
                desired = FLIPPED_ROTATION.get(self.display_rotation_orig, "inverted")
            else:
                desired = self.display_rotation_orig
            output, current = detect_display()
            if current == desired:
                return True
            return set_display_rotation(self.display_output, desired, log)

        def phase_kind(self, phase):
            """'good' when calibrated, 'warn' when factory defaults, 'bad' on error."""
            return self.phase_status(phase)[1]

        def steps(self, parent, current=None):
            """Draw the 1-2-3 step indicator."""
            canvas = tk.Canvas(parent, height=44, bg=C["bg"], highlightthickness=0)
            canvas.pack(fill="x", pady=(0, 10))
            x = 20
            r = 15
            for phase in (1, 2, 3):
                kind = self.phase_kind(phase)
                active = current is not None and phase in (current if isinstance(current, tuple) else (current,))
                if active:
                    fill, outline, fg, txt = C["accent"], C["accent"], C["accent_text"], str(phase)
                elif kind == "good":
                    fill, outline, fg, txt = C["good"], C["good"], "#ffffff", "\u2713"
                else:
                    fill, outline, fg, txt = C["card"], C["border_strong"], C["muted"], str(phase)
                canvas.create_oval(x - r, 22 - r, x + r, 22 + r, fill=fill, outline=outline, width=2)
                canvas.create_text(x, 22, text=txt, fill=fg, font=self.f_small_b)
                name = PHASE_NAMES[phase]
                canvas.create_text(x + r + 10, 22, text=name, anchor="w", fill=C["text"] if active else C["muted"],
                                   font=self.f_body_b if active else self.f_body)
                text_w = self.f_body_b.measure(name)
                x_end = x + r + 10 + text_w + 18
                if phase < 3:
                    canvas.create_line(x_end, 22, x_end + 56, 22, fill=C["border_strong"], width=2)
                    x = x_end + 56 + r + 4
            return canvas

        def badge(self, parent, phase, kind, bg, text=None):
            size = 36
            cv = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0)
            if kind == "good":
                fill, fg, txt = C["good"], "#ffffff", "\u2713"
            else:
                fill, fg, txt = C["accent"], "#ffffff", str(phase)
            if text is not None:
                txt = text
            cv.create_oval(2, 2, size - 2, size - 2, fill=fill, outline=fill)
            cv.create_text(size / 2, size / 2, text=txt, fill=fg, font=self.f_body_b)
            return cv

        def _on_log_line(self, line):
            self.log_lines.append(line)
            if len(self.log_lines) > 2000:
                del self.log_lines[:500]
            w = self.log_widget
            if w is not None and w.winfo_exists():
                w.configure(state="normal")
                w.insert("end", line + "\n")
                w.see("end")
                w.configure(state="disabled")

        def make_log_widget(self, parent, height=8, title="Activity log"):
            inner = self.card(parent, padx=0, pady=0, fill="both", expand=True, gap=(0, 0))
            head = tk.Frame(inner, bg=C["card"], padx=16, pady=8)
            head.pack(fill="x")
            self.label(head, title, font=self.f_body_b).pack(side="left")
            parts = paths.log_file.split(os.sep)
            short = os.sep.join(parts[-3:]) if len(parts) > 3 else paths.log_file
            self.label(head, "\u2026/" + short if len(parts) > 3 else short, font=self.f_small,
                       fg=C["faint"]).pack(side="right")
            frame = tk.Frame(inner, bg=C["log_bg"])
            frame.pack(fill="both", expand=True)
            text = tk.Text(frame, height=height, wrap="word", font=self.f_mono_small, state="disabled",
                           background=C["log_bg"], foreground=C["log_fg"], relief="flat", bd=0,
                           highlightthickness=0, padx=14, pady=10, insertbackground=C["log_fg"])
            sb = ttk.Scrollbar(frame, command=text.yview, style="Log.Vertical.TScrollbar")
            text.configure(yscrollcommand=sb.set)
            text.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            text.configure(state="normal")
            for line in self.log_lines[-200:]:
                text.insert("end", line + "\n")
            text.see("end")
            text.configure(state="disabled")
            self.log_widget = text

        def button_row(self, parent=None):
            row = tk.Frame(parent or self.body, bg=C["bg"])
            row.pack(fill="x", pady=(14, 0))
            return row

        # ---------------- data helpers ----------------
        def layout_values(self):
            """Return (plane, corners, error_text)."""
            try:
                plane, corners = read_box_layout(paths.box_layout)
                return plane, corners, None
            except (OSError, ValueError) as e:
                return None, None, str(e)

        def phase_status(self, phase):
            """Return (text, kind) describing the status of a phase; kind is good/warn/bad."""
            if phase in (1, 2):
                plane, corners, err = self.layout_values()
                if err:
                    return ("BoxLayout.txt unreadable: " + err, "bad")
                key = "plane" if phase == 1 else "corners"
                info = state.get(key)
                current = format_plane(plane) if phase == 1 else [format_point(c) for c in corners]
                factory = None
                if os.path.isfile(paths.box_layout_orig):
                    try:
                        oplane, ocorners = read_box_layout(paths.box_layout_orig)
                        factory = format_plane(oplane) if phase == 1 else [format_point(c) for c in ocorners]
                    except (OSError, ValueError):
                        factory = None
                if info and info.get("value") == current:
                    extra = ""
                    if phase == 1 and info.get("rms") is not None:
                        extra = "  \u00b7  fit RMS %.2f cm" % info["rms"]
                    return ("Done %s%s" % (info["done"], extra), "good")
                if factory is not None and current == factory:
                    return ("Not calibrated yet  \u00b7  factory defaults", "warn")
                if info:
                    return ("Edited by hand after %s" % info["done"], "good")
                return ("Values present  \u00b7  calibrated outside this wizard", "good")
            # phase 3
            if not os.path.isfile(paths.projector_matrix):
                return ("Not calibrated yet  \u00b7  no ProjectorMatrix.dat", "warn")
            info = state.get("projector")
            mtime = os.path.getmtime(paths.projector_matrix)
            if info and abs(info.get("mtime", -1) - mtime) < 1.0:
                text = "Done %s" % info["done"]
                if info.get("rms") is not None:
                    text += "  \u00b7  RMS %.2f px" % info["rms"]
                if info.get("resolution"):
                    text += "  \u00b7  %sx%s" % tuple(info["resolution"])
                return (text, "good")
            if os.path.isfile(paths.projector_matrix_orig) and files_equal(paths.projector_matrix,
                                                                           paths.projector_matrix_orig):
                return ("Not calibrated yet  \u00b7  factory defaults", "warn")
            stamp = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            return ("Matrix file dated %s  \u00b7  calibrated outside this wizard" % stamp, "good")

        # ---------------- colour height (sea level) ----------------
        def color_height_state(self):
            """Return (base_plane, delta_cm, in_use, error). delta is how far above the base
            plane the colour map's zero sits; in_use says whether SARndbox.cfg carries it."""
            plane, corners, err = self.layout_values()
            if plane is None:
                return None, 0.0, False, err
            hmp = read_height_map_plane(paths.sandbox_cfg)
            info = state.get("color_height")
            if info and "delta" in info:
                delta = float(info["delta"])
            elif hmp is not None:
                delta = hmp[3] - plane[3]
            else:
                delta = 0.0
            return plane, delta, hmp is not None or info is not None, None

        def write_color_height(self, base_plane, delta, live=True):
            """Persist the colour plane for `delta` in SARndbox.cfg and push it to a running sandbox."""
            hmp = height_map_plane_for(base_plane, delta)
            backup = backup_file(paths.sandbox_cfg, paths.backup_dir)
            write_height_map_plane(paths.sandbox_cfg, hmp)
            state.mark("color_height", delta=delta)
            log.write("Wrote heightMapPlane %s to %s (backup: %s)" % (format_plane(hmp), paths.sandbox_cfg, backup))
            if live:
                self.send_color_height_live(base_plane, delta)

        def send_color_height_live(self, base_plane, delta):
            if not sandbox_pids(paths.sandbox_process):
                return False
            nx, ny, nz, off = height_map_plane_for(base_plane, delta)
            return send_control_command(paths.control_fifo, "heightMapPlane %.6g %.6g %.6g %.6g" % (nx, ny, nz, off), log)

        def after_layout_write(self, new_plane, delta, in_use):
            """Keep the colour height in step after BoxLayout.txt changed."""
            if in_use:
                self.write_color_height(new_plane, delta)
            elif sandbox_pids(paths.sandbox_process):
                self.send_color_height_live(new_plane, 0.0)

        @staticmethod
        def format_delta(delta):
            if abs(delta) < 0.05:
                return "0 cm"
            return "%+.1f cm" % delta

        def show_color_height(self):
            self.clear()
            plane, delta, in_use, err = self.color_height_state()
            self.sandbox_bar = self.header(
                "Color height",
                "Moves the color bands up or down on the sand without changing the camera calibration. "
                "Positive raises the sea level (more blue), negative lowers it (more land). A running "
                "sandbox shows every change immediately; Save keeps it for the next start.")
            self.refresh_sandbox_bar()
            if err or plane is None:
                self.notice(self.body, "BoxLayout.txt is needed first: " + str(err), "bad")
                ttk.Button(self.button_row(), text="Back to overview", style="Secondary.TButton",
                           command=self.show_hub).pack(side="right")
                return
            self.ch_plane = plane
            self.ch_saved = delta if in_use else None
            self.ch_value = delta
            self._ch_pending = None

            card = self.card(self.body, padx=24, pady=20)
            top = tk.Frame(card, bg=C["card"])
            top.pack(fill="x")
            self.label(top, "Sea level offset from the calibrated base plane", fg=C["muted"]).pack(anchor="w")
            self.ch_display = self.label(top, self.format_delta(delta), font=self.f_display, fg=C["accent"])
            self.ch_display.pack(anchor="w", pady=(2, 8))

            self.ch_scale_var = tk.DoubleVar(value=delta)
            scale = ttk.Scale(card, from_=-20.0, to=20.0, orient="horizontal", variable=self.ch_scale_var,
                              command=self.ch_scale_moved, style="Horizontal.TScale")
            scale.pack(fill="x", pady=(0, 2))
            ticks = tk.Frame(card, bg=C["card"])
            ticks.pack(fill="x")
            self.label(ticks, "-20 cm  lower sea level", font=self.f_small, fg=C["muted"]).pack(side="left")
            self.label(ticks, "raise sea level  +20 cm", font=self.f_small, fg=C["muted"]).pack(side="right")

            steps = tk.Frame(card, bg=C["card"])
            steps.pack(fill="x", pady=(16, 0))
            for text, step in (("-5", -5.0), ("-1", -1.0), ("-0.5", -0.5)):
                ttk.Button(steps, text=text, style="Secondary.TButton", width=6,
                           command=lambda d=step: self.ch_set(self.ch_value + d)).pack(side="left", padx=(0, 6))
            ttk.Button(steps, text="Reset to 0", style="Secondary.TButton",
                       command=lambda: self.ch_set(0.0)).pack(side="left", padx=(12, 12))
            for text, step in (("+0.5", 0.5), ("+1", 1.0), ("+5", 5.0)):
                ttk.Button(steps, text=text, style="Secondary.TButton", width=6,
                           command=lambda d=step: self.ch_set(self.ch_value + d)).pack(side="left", padx=(0, 6))

            self.ch_hint = tk.Frame(self.body, bg=C["bg"])
            self.ch_hint.pack(fill="x")
            self.ch_refresh_hint()

            buttons = self.button_row()
            ttk.Button(buttons, text="Save", style="Primary.TButton", command=self.ch_save).pack(side="left")
            ttk.Button(buttons, text="Back to overview", style="Secondary.TButton",
                       command=self.ch_back).pack(side="right")

        def ch_refresh_hint(self):
            f = getattr(self, "ch_hint", None)
            if f is None or not f.winfo_exists():
                return
            for w in f.winfo_children():
                w.destroy()
            running = bool(sandbox_pids(paths.sandbox_process))
            unsaved = self.ch_saved is None or abs(self.ch_value - self.ch_saved) > 1e-6
            if running:
                text = "The running sandbox is showing this value. "
            else:
                text = "The sandbox is not running, so you cannot see the effect yet; launch it to preview. "
            if unsaved:
                text += "Not saved yet: press Save to keep it for the next start."
                kind = "warn"
            else:
                text += "Saved in SARndbox.cfg."
                kind = "good"
            self.notice(f, text, kind)

        def ch_scale_moved(self, value):
            self.ch_set(round(float(value) * 2.0) / 2.0, from_scale=True)

        def ch_set(self, delta, from_scale=False):
            delta = max(-20.0, min(20.0, round(delta * 2.0) / 2.0))
            if abs(delta - self.ch_value) < 1e-9 and from_scale:
                return
            self.ch_value = delta
            self.ch_display.configure(text=self.format_delta(delta))
            if not from_scale:
                self.ch_scale_var.set(delta)
            # push to the sandbox after the slider settles
            if self._ch_pending is not None:
                self.after_cancel(self._ch_pending)
            self._ch_pending = self.after(120, self.ch_push_live)
            self.ch_refresh_hint()

        def ch_push_live(self):
            self._ch_pending = None
            self.send_color_height_live(self.ch_plane, self.ch_value)

        def ch_save(self):
            self.write_color_height(self.ch_plane, self.ch_value)
            self.ch_saved = self.ch_value
            self.ch_refresh_hint()

        def ch_back(self):
            unsaved = self.ch_saved is None or abs(self.ch_value - self.ch_saved) > 1e-6
            if unsaved and (self.ch_saved is not None or abs(self.ch_value) > 1e-6):
                answer = messagebox.askyesnocancel(APP_TITLE, "Keep the new color height (%s)?\n\nYes saves it, "
                                                              "No puts the previous value back." % self.format_delta(self.ch_value))
                if answer is None:
                    return
                if answer:
                    self.ch_save()
                else:
                    self.send_color_height_live(self.ch_plane, self.ch_saved or 0.0)
            self.show_hub()

        # ---------------- hub ----------------
        def show_hub(self):
            self.clear()
            self.session = None
            self.sandbox_bar = self.header("Sandbox calibration",
                                           "Tick the phases to run and press Run. Phases 1 and 2 share one "
                                           "RawKinectViewer window. Values can be adjusted under Edit values.")
            self.refresh_sandbox_bar()
            self.steps(self.body)

            plane, corners, err = self.layout_values()
            self.phase_vars = {}
            rows = [
                (1, "Flat sand surface as seen by the camera", short_plane(plane) if plane else "-"),
                (2, "The four corners of the sand surface",
                 "\n".join("%-12s %s" % (n + ":", short_point(c)) for n, c in zip(CORNER_NAMES, corners))
                 if corners else "-"),
                (3, "Aligns the projected image with the camera", "Resolution %dx%d" % self.resolution),
            ]
            for phase, desc, values in rows:
                status, kind = self.phase_status(phase)
                var = tk.BooleanVar(value=kind != "good")
                self.phase_vars[phase] = var
                cardf = self.card(self.body, padx=16, pady=8, gap=(0, 8))
                cardf.columnconfigure(3, weight=1)
                ttk.Checkbutton(cardf, variable=var, style="Card.TCheckbutton").grid(
                    row=0, column=0, sticky="n", padx=(0, 6), pady=(4, 0))
                self.badge(cardf, phase, kind, C["card"]).grid(row=0, column=1, sticky="n", padx=(0, 14))
                info = tk.Frame(cardf, bg=C["card"])
                info.grid(row=0, column=2, sticky="nw")
                self.label(info, "Phase %d  \u00b7  %s" % (phase, PHASE_NAMES[phase]), font=self.f_h2).pack(anchor="w")
                self.label(info, desc, fg=C["muted"]).pack(anchor="w", pady=(0, 4))
                self.pill(info, status, kind).pack(anchor="w")
                self.label(cardf, values, font=self.f_mono, fg=C["muted_text"]).grid(
                    row=0, column=3, sticky="nw", padx=(24, 12))
                ttk.Button(cardf, text="Run", style="Secondary.TButton",
                           command=lambda p=phase: self.start_phases([p])).grid(row=0, column=4, sticky="ne")
            if err:
                self.notice(self.body, err, "bad")

            ch_plane, ch_delta, ch_in_use, ch_err = self.color_height_state()
            cardf = self.card(self.body, padx=16, pady=8, gap=(0, 8))
            cardf.columnconfigure(3, weight=1)
            tk.Frame(cardf, bg=C["card"], width=28).grid(row=0, column=0, padx=(0, 6))
            self.badge(cardf, 0, "accent", C["card"], text="\u2248").grid(row=0, column=1, sticky="n", padx=(0, 14))
            info = tk.Frame(cardf, bg=C["card"])
            info.grid(row=0, column=2, sticky="nw")
            self.label(info, "Color height  \u00b7  Sea level", font=self.f_h2).pack(anchor="w")
            self.label(info, "Where the water color starts, relative to the calibrated base plane",
                       fg=C["muted"]).pack(anchor="w", pady=(0, 4))
            if ch_in_use:
                self.pill(info, "Set to %s  \u00b7  saved in SARndbox.cfg" % self.format_delta(ch_delta), "good").pack(anchor="w")
            else:
                self.pill(info, "Not adjusted  \u00b7  colors follow the base plane", "muted").pack(anchor="w")
            self.label(cardf, self.format_delta(ch_delta), font=self.f_mono, fg=C["muted_text"]).grid(
                row=0, column=3, sticky="nw", padx=(24, 12))
            ttk.Button(cardf, text="Adjust", style="Secondary.TButton",
                       command=self.show_color_height).grid(row=0, column=4, sticky="ne")

            buttons = self.button_row()
            ttk.Button(buttons, text="Run ticked phases", style="Primary.TButton",
                       command=self.run_ticked).pack(side="left")
            ttk.Button(buttons, text="Run all 3 phases", style="Secondary.TButton",
                       command=lambda: self.start_phases([1, 2, 3])).pack(side="left", padx=(10, 0))
            ttk.Button(buttons, text="Edit values", style="Secondary.TButton",
                       command=self.show_editor).pack(side="left", padx=(10, 0))
            ttk.Button(buttons, text="Restore factory defaults", style="Danger.TButton",
                       command=self.restore_defaults).pack(side="left", padx=(10, 0))
            ttk.Button(buttons, text="Quit", style="Secondary.TButton", command=self.on_close).pack(side="right")
            tk.Frame(self.body, bg=C["bg"], height=8).pack()
            self.make_log_widget(self.body, height=3)

        def refresh_sandbox_bar(self, force=False):
            """Rebuild the running/stopped indicator only when the sandbox state changed."""
            bar = getattr(self, "sandbox_bar", None)
            if bar is None or not bar.winfo_exists():
                return
            pids = sandbox_pids(paths.sandbox_process)
            key = pids[0] if pids else 0
            self._last_bar_refresh = time.time()
            if not force and key == self._sandbox_bar_key and bar.winfo_children():
                return
            state_changed = self._sandbox_bar_key is not None and key != self._sandbox_bar_key
            self._sandbox_bar_key = key
            if state_changed:
                self.apply_flip()
            for w in bar.winfo_children():
                w.destroy()
            if pids:
                self.pill(bar, "\u25cf  Sandbox running  \u00b7  pid %d" % pids[0], "good").pack(side="left", padx=(0, 10))
                ttk.Button(bar, text="Stop sandbox", style="Small.Secondary.TButton",
                           command=self.stop_sandbox_clicked).pack(side="left")
            else:
                self.pill(bar, "\u25cb  Sandbox not running", "muted").pack(side="left", padx=(0, 10))
                ttk.Button(bar, text="Launch sandbox", style="Small.Secondary.TButton",
                           command=self.launch_sandbox_clicked).pack(side="left")

        def stop_sandbox_clicked(self):
            stop_sandbox(paths.sandbox_process, log)
            self.apply_flip()
            self.refresh_sandbox_bar(force=True)

        def launch_sandbox_clicked(self):
            if self.runner is not None and self.runner.running():
                messagebox.showinfo(APP_TITLE, "Finish or stop the running calibration tool first.")
                return
            self.apply_flip(sandbox_starting=True)
            launch_sandbox(paths, log)
            self.after(1500, self.refresh_sandbox_bar)

        def run_ticked(self):
            phases = [p for p in (1, 2, 3) if self.phase_vars[p].get()]
            if not phases:
                messagebox.showinfo(APP_TITLE, "Tick at least one phase first.")
                return
            self.start_phases(phases)

        def restore_defaults(self):
            if not messagebox.askyesno(APP_TITLE, "Replace BoxLayout.txt and ProjectorMatrix.dat with the "
                                                  "factory default files?\n\nThe current files are backed up first."):
                return
            for src, dst in ((paths.box_layout_orig, paths.box_layout),
                             (paths.projector_matrix_orig, paths.projector_matrix)):
                if os.path.isfile(src):
                    backup_file(dst, paths.backup_dir)
                    shutil.copy2(src, dst)
                    log.write("Restored %s from %s" % (dst, src))
                else:
                    log.write("No factory file %s; skipped" % src)
            state.clear()
            self.show_hub()

        # ---------------- phase flow ----------------
        def start_phases(self, phases):
            self.queue = list(phases)
            self.next_session()

        def next_session(self):
            if not self.queue:
                self.show_hub()
                return
            if self.queue[:2] == [1, 2]:
                self.queue = self.queue[2:]
                session = KinectSession((1, 2), bool(paths.vislet))
            elif self.queue[0] in (1, 2):
                session = KinectSession((self.queue.pop(0),), bool(paths.vislet))
            else:
                self.queue.pop(0)
                session = ProjectorSession(self.resolution[0], self.resolution[1], bool(paths.vislet))
            self.show_intro(session)

        def show_intro(self, session):
            self.clear()
            self.session = session
            self.header(session.title(), "Read this first, then press Start. The tool opens full screen.")
            self.steps(self.body, current=session.phases)

            buttons = tk.Frame(self.body, bg=C["bg"])
            buttons.pack(side="bottom", fill="x", pady=(14, 0))
            ttk.Button(buttons, text="Start " + session.tool_name, style="Primary.TButton",
                       command=self.start_tool).pack(side="left")
            if isinstance(session, ProjectorSession):
                ttk.Button(buttons, text="Show alignment grid", style="Secondary.TButton",
                           command=self.show_alignment_grid).pack(side="left", padx=(10, 0))
            ttk.Button(buttons, text="Back to overview", style="Secondary.TButton",
                       command=self.abort_to_hub).pack(side="right")

            if isinstance(session, ProjectorSession):
                bottom = tk.Frame(self.body, bg=C["bg"])
                bottom.pack(side="bottom", fill="x")
                res = self.card(bottom, padx=16, pady=10, gap=(0, 0))
                self.label(res, "Projector resolution", font=self.f_body_b).pack(side="left")
                self.label(res, "must match the projector's input signal", fg=C["muted"]).pack(side="left", padx=(8, 14))
                self.res_w = tk.StringVar(value=str(session.width))
                self.res_h = tk.StringVar(value=str(session.height))
                ttk.Entry(res, textvariable=self.res_w, width=6, font=self.f_mono, justify="center").pack(side="left")
                self.label(res, "\u00d7", fg=C["muted"]).pack(side="left", padx=6)
                ttk.Entry(res, textvariable=self.res_h, width=6, font=self.f_mono, justify="center").pack(side="left")
                self.pill(res, "detected %dx%d" % detect_resolution(), "info").pack(side="left", padx=(14, 0))

            sections = session.intro_sections()
            closing = sections[-1]
            grid = tk.Frame(self.body, bg=C["bg"])
            grid.pack(fill="both", expand=True)
            grid.columnconfigure(0, weight=1, uniform="col")
            grid.columnconfigure(1, weight=1, uniform="col")
            r = c = 0
            for heading, text in sections[:-1]:
                span = 2 if len(text) > 330 else 1
                if span == 2 and c == 1:
                    r, c = r + 1, 0
                outer = tk.Frame(grid, bg=C["card"], highlightthickness=1, highlightbackground=C["border"])
                outer.grid(row=r, column=c, columnspan=span, sticky="nsew",
                           padx=(0, 12) if (c == 0 and span == 1) else 0, pady=(0, 12))
                inner = tk.Frame(outer, bg=C["card"], padx=18, pady=14)
                inner.pack(fill="both", expand=True)
                self.label(inner, heading, font=self.f_h2).pack(anchor="w", pady=(0, 6))
                body = self.label(inner, text, wraplength=400)
                body.pack(anchor="w", fill="x")
                inner.bind("<Configure>", lambda e, l=body: l.configure(wraplength=max(200, e.width - 40)))
                if span == 2 or c == 1:
                    r, c = r + 1, 0
                else:
                    c = 1
            self.notice(grid, closing[1], "info", prefix=closing[0] + ":  ", pack=False).grid(
                row=r + 1, column=0, columnspan=2, sticky="ew")

        def show_alignment_grid(self):
            w, h = self.read_resolution()
            if w is None:
                return
            argv = [paths.xbackground, "-f", "-geometry", "%dx%d" % (w, h)]
            log.write("Running: %s" % " ".join(argv))
            self.apply_flip(tool_starting=True)
            try:
                self.xbg_proc = subprocess.Popen(argv, start_new_session=True)
            except OSError as e:
                messagebox.showerror(APP_TITLE, "Could not start XBackground: %s" % e)
            else:
                messagebox.showinfo(APP_TITLE, "XBackground shows a grid on the projector. Align the projector so "
                                               "the grid fills the sandbox, then press Esc in that window.")

        def read_resolution(self):
            try:
                w, h = int(self.res_w.get()), int(self.res_h.get())
                if w < 100 or h < 100:
                    raise ValueError
            except (ValueError, AttributeError):
                messagebox.showerror(APP_TITLE, "Enter the projector resolution as two whole numbers, e.g. 1024 and 768.")
                return None, None
            return w, h

        def start_tool(self):
            session = self.session
            if isinstance(session, ProjectorSession):
                w, h = self.read_resolution()
                if w is None:
                    return
                session.width, session.height = w, h
                self.resolution = (w, h)
                backup = backup_file(paths.projector_matrix, paths.backup_dir)
                if backup:
                    log.write("Backed up projector matrix to %s" % backup)
                session.matrix_mtime_before = (os.path.getmtime(paths.projector_matrix)
                                               if os.path.isfile(paths.projector_matrix) else None)
                session.started_at = time.time()
            if sandbox_pids(paths.sandbox_process):
                if not messagebox.askokcancel(APP_TITLE, "The sandbox is running and holds the 3D camera.\n\n"
                                                         "Stop the sandbox now and start %s?" % session.tool_name):
                    return
                if not stop_sandbox(paths.sandbox_process, log):
                    messagebox.showerror(APP_TITLE, "The sandbox process could not be stopped.")
                    return
            argv = session.argv(paths)
            log.write("SandboxHelper plugin: %s" % (paths.vislet or "not installed"))
            self.apply_flip(tool_starting=True)
            try:
                self.runner = ToolRunner(argv, paths.sarndbox_dir, log)
            except OSError as e:
                messagebox.showerror(APP_TITLE, "Could not start %s:\n%s\n\n%s" % (session.tool_name, argv[0], e))
                return
            if paths.vislet:
                # The plugin reports "SandboxHelper: loaded" (or Vrui reports it missing) within a
                # second; the first popup is sent then, through the plugin, so no Vrui-native popup
                # is ever created. _poll falls back to plain Vrui messages if neither line shows up.
                session.awaiting_helper_since = time.time()
            else:
                self.runner.show_message(session.initial_message())
            self.show_running()

        def show_running(self):
            self.clear()
            session = self.session
            self.header(session.title(),
                        "%s is running. Work in its window; this screen follows along. "
                        "Press Esc in the tool window when you are done." % session.tool_name)
            self.steps(self.body, current=session.phases)
            buttons = tk.Frame(self.body, bg=C["bg"])
            buttons.pack(side="bottom", fill="x", pady=(14, 0))
            ttk.Button(buttons, text="Stop the tool now", style="Danger.TButton",
                       command=self.stop_tool_clicked).pack(side="left")
            ttk.Button(buttons, text="Send instructions again", style="Secondary.TButton",
                       command=lambda: self.runner and self.runner.show_message(session.reminder())).pack(
                side="left", padx=(10, 0))
            self.recapture_btn = None
            if isinstance(session, KinectSession):
                self.recapture_btn = ttk.Button(buttons, text="Capture the sand again", style="Secondary.TButton",
                                                command=self.recapture_average)
                self.recapture_btn.pack(side="left", padx=(10, 0))
                self.recapture_btn.state(["disabled"])
            inner = self.card(self.body, padx=18, pady=14)
            self.pulse_canvas = tk.Canvas(inner, width=22, height=22, bg=C["card"], highlightthickness=0)
            self.pulse_canvas.pack(side="left", padx=(0, 12))
            self._pulse_step = 0
            self._pulse()
            self.status_label = self.label(inner, session.status, font=self.f_h2, wraplength=840, anchor="w")
            self.status_label.pack(side="left", fill="x", expand=True)
            self.make_log_widget(self.body, height=12, title="Tool output")

        def _pulse(self):
            cv = self.pulse_canvas
            if cv is None or not cv.winfo_exists():
                return
            self._pulse_step = (self._pulse_step + 1) % 3
            r = (6, 8, 10)[self._pulse_step]
            cv.delete("all")
            cv.create_oval(11 - 10, 11 - 10, 11 + 10, 11 + 10, fill=C["info_bg"], outline="")
            cv.create_oval(11 - r, 11 - r, 11 + r, 11 + r, fill=C["accent"], outline="")
            self.after(380, self._pulse)

        def stop_tool_clicked(self):
            if self.runner is not None:
                self.runner.stop()

        def flush_commands(self):
            """Send the console commands a session queued (e.g. sandboxAverage on)."""
            runner, session = self.runner, self.session
            if runner is None or session is None:
                return
            if session.helper:
                runner.helper = True
            for command in session.take_commands():
                runner.send(command)

        def recapture_average(self):
            session, runner = self.session, self.runner
            if runner is None or not isinstance(session, KinectSession) or not session.helper:
                return
            message = session.request_average()
            self.flush_commands()
            runner.show_message(message)
            self.refresh_running_widgets()

        def refresh_running_widgets(self):
            session = self.session
            lbl = getattr(self, "status_label", None)
            if lbl is not None and lbl.winfo_exists() and session is not None:
                lbl.configure(text=session.status)
            btn = getattr(self, "recapture_btn", None)
            if btn is not None and btn.winfo_exists() and session is not None:
                ready = getattr(session, "helper", False) and not getattr(session, "averaging", False)
                btn.state(["!disabled"] if ready else ["disabled"])

        def _poll(self):
            runner = self.runner
            if runner is not None:
                finished = False
                while True:
                    try:
                        line = runner.lines.get_nowait()
                    except queue.Empty:
                        break
                    if line is None:
                        finished = True
                        break
                    log.write("%s: %s" % (self.session.tool_name, line))
                    message = self.session.on_line(line)
                    self.flush_commands()
                    if message:
                        runner.show_message(message)
                    self.refresh_running_widgets()
                s = self.session
                if (s.awaiting_helper_since and not s.helper and not s.helper_failed
                        and time.time() - s.awaiting_helper_since > HELPER_TIMEOUT):
                    s.helper_failed = True
                    log.write("The SandboxHelper plugin did not report within %d s; using plain Vrui messages"
                              % HELPER_TIMEOUT)
                    message = s.on_helper_failed()
                    if message:
                        runner.show_message(message)
                    self.refresh_running_widgets()
                if finished:
                    rc = runner.proc.wait()
                    log.write("%s exited with code %s" % (self.session.tool_name, rc))
                    self.runner = None
                    self.apply_flip()
                    self.tool_finished(rc)
            else:
                if self.xbg_proc is not None and self.xbg_proc.poll() is not None:
                    self.xbg_proc = None
                    self.apply_flip()
                if time.time() - self._last_bar_refresh > 2.0:
                    self.refresh_sandbox_bar()
            self.after(150, self._poll)

        def tool_finished(self, rc):
            try:
                self.deiconify()
                self.lift()
                self.focus_force()
            except tk.TclError:
                pass
            if isinstance(self.session, KinectSession):
                self.show_kinect_result(rc)
            else:
                self.show_projector_result(rc)

        # ---------------- results ----------------
        def show_kinect_result(self, rc):
            s = self.session
            self.clear()
            self.header(s.title() + "  \u00b7  result", "Check the numbers, then save or redo.")
            self.steps(self.body, current=s.phases)
            problems = []
            if s.error:
                problems.append("RawKinectViewer stopped with an error: " + s.error)
                hint = s.explain_error(s.error)
                if hint:
                    problems.append(hint)
            problems += s.missing()

            current_plane, current_corners, err = self.layout_values()
            new_plane = s.plane if (1 in s.phases and s.plane) else current_plane
            new_corners = s.corners() if (2 in s.phases and len(s.corners()) == 4) else current_corners
            self.pending_plane, self.pending_corners = new_plane, new_corners

            warnings = []
            if 1 in s.phases and s.plane:
                if s.plane_flipped:
                    warnings.append("The camera reported the plane inverted; the signs were flipped "
                                    "so the offset is negative (as the sandbox expects).")
                warnings += plane_warnings(s.plane)
            if 2 in s.phases and len(s.corners()) == 4:
                warnings += corner_warnings(s.corners(), new_plane)

            cards = tk.Frame(self.body, bg=C["bg"])
            cards.pack(fill="x")
            if 1 in s.phases:
                c1 = self.card(cards, side="left", fill="both", expand=True)
                self.label(c1, "Base plane", font=self.f_h2).pack(anchor="w")
                self.label(c1, format_plane(s.plane) if s.plane else "not captured", font=self.f_mono).pack(
                    anchor="w", pady=(6, 4))
                if s.plane and s.plane_rms is not None:
                    self.pill(c1, "fit RMS %.2f cm  \u00b7  lower is flatter" % s.plane_rms,
                              "good" if s.plane_rms < 1.0 else "warn").pack(anchor="w")
            if 2 in s.phases:
                c2 = self.card(cards, side="left", fill="both", expand=True)
                self.label(c2, "Box corners", font=self.f_h2).pack(anchor="w")
                cs = s.corners()
                lines = ["%-12s %s" % (n + ":", format_point(c)) for n, c in zip(CORNER_NAMES, cs)] or ["none"]
                self.corner_label = self.label(c2, "\n".join(lines), font=self.f_mono)
                self.corner_label.pack(anchor="w", pady=(6, 4))
                self.pill(c2, "last 4 of %d clicks" % len(s.points), "info").pack(anchor="w")
            for p in problems:
                self.notice(self.body, p, "bad", prefix="Problem:  ")
            for w in warnings:
                self.notice(self.body, w, "warn", prefix="Check:  ")
            if not problems and not warnings:
                self.notice(self.body, "Looks good.", "good", prefix="\u2713  ")

            buttons = self.button_row()
            if not s.missing():
                ttk.Button(buttons, text="Save and continue", style="Primary.TButton",
                           command=self.save_kinect_result).pack(side="left")
            if 2 in s.phases and len(s.corners()) == 4 and any("order" in w for w in warnings):
                ttk.Button(buttons, text="Auto-order corners", style="Secondary.TButton",
                           command=self.auto_order_clicked).pack(side="left", padx=(10, 0))
            ttk.Button(buttons, text="Redo this step", style="Secondary.TButton",
                       command=lambda: self.show_intro(KinectSession(s.phases, bool(paths.vislet)))).pack(side="left", padx=(10, 0))
            ttk.Button(buttons, text="Back to overview", style="Secondary.TButton",
                       command=self.abort_to_hub).pack(side="right")
            tk.Frame(self.body, bg=C["bg"], height=14).pack()
            self.make_log_widget(self.body, height=5, title="Tool output")

        def auto_order_clicked(self):
            s = self.session
            ordered = auto_order_corners(s.corners())
            s.points = ordered
            self.pending_corners = ordered
            log.write("Corners auto-ordered")
            self.show_kinect_result(0)

        def save_kinect_result(self):
            s = self.session
            plane, corners = self.pending_plane, self.pending_corners
            if plane is None or corners is None or len(corners) != 4:
                messagebox.showerror(APP_TITLE, "Nothing complete to save.")
                return
            _, ch_delta, ch_in_use, _ = self.color_height_state()
            backup = backup_file(paths.box_layout, paths.backup_dir)
            write_box_layout(paths.box_layout, plane, corners)
            log.write("Wrote %s (backup: %s)" % (paths.box_layout, backup))
            self.after_layout_write(plane, ch_delta, ch_in_use)
            if 1 in s.phases and s.plane:
                state.mark("plane", value=format_plane(plane), rms=s.plane_rms, serial=s.serial)
            if 2 in s.phases and len(s.corners()) == 4:
                state.mark("corners", value=[format_point(c) for c in corners], serial=s.serial)
            self.next_session()

        def show_projector_result(self, rc):
            s = self.session
            self.clear()
            self.header(s.title() + "  \u00b7  result", "Check the result, then continue or redo.")
            self.steps(self.body, current=s.phases)
            problems, notes = [], []
            if s.error:
                problems.append("CalibrateProjector stopped with an error: " + s.error)
                hint = s.explain_error(s.error)
                if hint:
                    problems.append(hint)
            if s.calib_error:
                problems.append("The calibration computation failed: " + s.calib_error +
                                ". Run the phase again and capture all points from scratch.")
            mtime = os.path.getmtime(paths.projector_matrix) if os.path.isfile(paths.projector_matrix) else None
            written = mtime is not None and mtime >= s.started_at - 1
            if not written and not problems:
                problems.append("No new ProjectorMatrix.dat was written. %d of %d points were captured. "
                                "The tool was probably closed before the last point."
                                % (s.tie_points, NUM_TIE_POINTS))
            if written:
                notes.append("ProjectorMatrix.dat written at %s." %
                             datetime.datetime.fromtimestamp(mtime).strftime("%H:%M:%S"))
                if s.rms is not None:
                    notes.append("RMS residual %.2f pixels (below about 2 px is good; above 5 px, "
                                 "consider redoing)." % s.rms)
                state.mark("projector", rms=s.rms, resolution=[s.width, s.height], mtime=mtime)
            c = self.card(self.body)
            self.label(c, "Projector calibration", font=self.f_h2).pack(anchor="w")
            row = tk.Frame(c, bg=C["card"])
            row.pack(anchor="w", pady=(8, 0))
            self.pill(row, "%d of %d tie points" % (s.tie_points, NUM_TIE_POINTS),
                      "good" if s.tie_points >= NUM_TIE_POINTS else "warn").pack(side="left", padx=(0, 8))
            if s.rms is not None:
                self.pill(row, "RMS %.2f px" % s.rms, "good" if s.rms < 2.0 else "warn").pack(side="left", padx=(0, 8))
            self.pill(row, "%dx%d" % (s.width, s.height), "info").pack(side="left")
            for n in notes:
                self.notice(self.body, n, "good", prefix="\u2713  ")
            for p in problems:
                self.notice(self.body, p, "bad", prefix="Problem:  ")
            buttons = self.button_row()
            if written and not problems:
                ttk.Button(buttons, text="Continue", style="Primary.TButton", command=self.next_session).pack(side="left")
            ttk.Button(buttons, text="Redo this step", style="Secondary.TButton",
                       command=lambda: self.show_intro(ProjectorSession(s.width, s.height, bool(paths.vislet)))).pack(side="left", padx=(10, 0))
            ttk.Button(buttons, text="Back to overview", style="Secondary.TButton",
                       command=self.abort_to_hub).pack(side="right")
            tk.Frame(self.body, bg=C["bg"], height=14).pack()
            self.make_log_widget(self.body, height=6, title="Tool output")

        def abort_to_hub(self):
            self.queue = []
            self.show_hub()

        # ---------------- editor ----------------
        def show_editor(self):
            self.clear()
            self.header("Edit values",
                        "Adjust the numbers by hand. Saving writes BoxLayout.txt and backs up the old file. "
                        "A running sandbox recolors heights from the new plane at once; corner changes and "
                        "the full plane apply when the sandbox restarts.")
            plane, corners, err = self.layout_values()
            if err:
                self.notice(self.body, err, "bad")
                plane, corners = (0.0, 0.0, 1.0, -100.0), [(0, 0, 0)] * 4
            inner = self.card(self.body)
            grid = tk.Frame(inner, bg=C["card"])
            grid.pack(anchor="w")
            self.edit_vars = {}

            def head(r, c, text):
                self.label(grid, text, font=self.f_small_b, fg=C["muted"]).grid(row=r, column=c, pady=(0, 2))

            def row(r, label, values, keys):
                self.label(grid, label, font=self.f_body_b).grid(row=r, column=0, sticky="w", padx=(0, 16), pady=5)
                for c, (v, k) in enumerate(zip(values, keys)):
                    var = tk.StringVar(value="%.6g" % v)
                    self.edit_vars[k] = var
                    ttk.Entry(grid, textvariable=var, width=12, font=self.f_mono).grid(row=r, column=1 + c, padx=4, pady=5)

            head(0, 1, "normal x"); head(0, 2, "normal y"); head(0, 3, "normal z"); head(0, 4, "offset (cm)")
            row(1, "Base plane", plane, ("nx", "ny", "nz", "off"))
            tk.Frame(grid, bg=C["card"], height=10).grid(row=2, column=0)
            head(3, 1, "x (cm)"); head(3, 2, "y (cm)"); head(3, 3, "z (cm)")
            for i, name in enumerate(CORNER_NAMES):
                row(4 + i, "Corner " + name, corners[i], ("c%d%s" % (i, a) for a in "xyz"))

            self.editor_msg_frame = tk.Frame(self.body, bg=C["bg"])
            self.editor_msg_frame.pack(fill="x")
            self.editor_msg = tk.Label(self.editor_msg_frame, text="", bg=C["bg"], fg=C["text"], font=self.f_body,
                                       wraplength=880, justify="left", padx=14, pady=10)
            buttons = self.button_row()
            ttk.Button(buttons, text="Save", style="Primary.TButton", command=self.save_editor).pack(side="left")
            ttk.Button(buttons, text="Auto-order corners", style="Secondary.TButton",
                       command=self.editor_auto_order).pack(side="left", padx=(10, 0))
            ttk.Button(buttons, text="Restart sandbox with these values", style="Secondary.TButton",
                       command=self.editor_restart_sandbox).pack(side="left", padx=(10, 0))
            ttk.Button(buttons, text="Back to overview", style="Secondary.TButton",
                       command=self.show_hub).pack(side="right")

        def set_editor_msg(self, text, kind):
            bg, fg = TINTS[kind]
            self.editor_msg_frame.configure(bg=bg)
            self.editor_msg.configure(text=text, bg=bg, fg=fg)
            self.editor_msg.pack(anchor="w", fill="x")

        def read_editor(self):
            try:
                v = {k: float(var.get()) for k, var in self.edit_vars.items()}
            except ValueError:
                self.set_editor_msg("All fields must be numbers.", "bad")
                return None, None
            plane = (v["nx"], v["ny"], v["nz"], v["off"])
            corners = [(v["c%dx" % i], v["c%dy" % i], v["c%dz" % i]) for i in range(4)]
            return plane, corners

        def editor_auto_order(self):
            plane, corners = self.read_editor()
            if plane is None:
                return
            ordered = auto_order_corners(corners)
            for i, c in enumerate(ordered):
                for a, val in zip("xyz", c):
                    self.edit_vars["c%d%s" % (i, a)].set("%.6g" % val)
            self.set_editor_msg("Corners re-ordered as lower-left, lower-right, upper-left, upper-right.", "good")

        def save_editor(self):
            plane, corners = self.read_editor()
            if plane is None:
                return False
            plane, flipped = normalize_plane(plane)
            warnings = plane_warnings(plane) + corner_warnings(corners, plane)
            _, ch_delta, ch_in_use, _ = self.color_height_state()
            backup = backup_file(paths.box_layout, paths.backup_dir)
            write_box_layout(paths.box_layout, plane, corners)
            log.write("Wrote %s from the editor (backup: %s)" % (paths.box_layout, backup))
            state.mark("plane", value=format_plane(plane), rms=None, edited=True)
            state.mark("corners", value=[format_point(c) for c in corners], edited=True)
            self.after_layout_write(plane, ch_delta, ch_in_use)
            msg = "Saved."
            if flipped:
                msg += " The plane signs were flipped so the offset is negative."
            if ch_in_use:
                msg += " The color height of %s was kept relative to the new plane." % self.format_delta(ch_delta)
            if sandbox_pids(paths.sandbox_process):
                msg += " The running sandbox now colors heights from the new plane; restart it to apply the corners."
            if warnings:
                msg += "\nCheck: " + "\nCheck: ".join(warnings)
            self.set_editor_msg(msg, "warn" if warnings else "good")
            return True

        def editor_restart_sandbox(self):
            if not self.save_editor():
                return
            stop_sandbox(paths.sandbox_process, log)
            self.apply_flip(sandbox_starting=True)
            launch_sandbox(paths, log)
            self.set_editor_msg(self.editor_msg.cget("text") + "\nSandbox restarted.", "good")

        # ---------------- close ----------------
        def on_close(self):
            if self.runner is not None and self.runner.running():
                if not messagebox.askyesno(APP_TITLE, "%s is still running. Stop it and quit?" % self.session.tool_name):
                    return
                self.runner.stop()
            if self.flip_wanted:
                self.flip_wanted = False
                self.apply_flip()
            self.destroy()

    return App


def run_gui(paths):
    app = build_app(paths)()
    app.mainloop()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="AR Sandbox calibration wizard")
    parser.add_argument("--check", action="store_true", help="print the paths the wizard will use and exit")
    args = parser.parse_args(argv)
    paths = Paths()
    if args.check:
        ok_all = True
        for label, path, ok in paths.check():
            print("%-22s %-4s %s" % (label, "ok" if ok else "MISSING", path))
            ok_all = ok_all and ok
        return 0 if ok_all else 1
    try:
        import tkinter  # noqa: F401
    except ImportError:
        sys.stderr.write("python3-tk is not installed. Run: sudo apt-get install python3-tk\n")
        return 2
    run_gui(paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())
