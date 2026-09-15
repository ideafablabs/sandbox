#!/usr/bin/env python3
"""
Calibrate Sandbox - a single-window wizard around the UC Davis AR Sandbox
calibration tools.

Base plane   (RawKinectViewer, "Extract Planes" tool on key 1)
Box corners  (RawKinectViewer, "Measure 3D Positions" tool on key 2)
Projector    (CalibrateProjector, "Capture" tool on keys 1 and 2)
Depth lens   (RawKinectViewer, "Calibrate Depth Lens" tool on keys 1 and 2), hidden
             unless SANDBOX_CALIB_DEPTH=1; it then comes first and renumbers the rest.

The UC Davis programs are not modified. The wizard launches them, sends
instructions into their window through the Vrui command interface on stdin
("showMessage ..." / "quit"), parses what they print, validates the numbers,
backs up the old files and writes BoxLayout.txt.  CalibrateProjector writes
ProjectorMatrix.dat itself; the wizard only checks that it did.

The base plane and box corners share one RawKinectViewer session when both are
selected. Phases are numbered on screen by their position in ALL_PHASES, so the
numbers move when the depth lens phase is switched on.

The depth lens phase is the per-pixel depth correction of the camera (optional,
once per camera). RawKinectViewer's "Calibrate Depth Lens" tool writes
DepthCorrection-<serial>.dat into the Kinect configuration directory itself
(a compiled-in path, /usr/local/etc/Vrui-8.0/Kinect-3.10, owned by root after
the install; the wizard offers a pkexec fix). The wizard binds that tool to
keys 1 and 2 for this phase only with a Vrui -mergeConfig file, counts the
captures through the SandboxHelper plugin, checks that the file appeared and
marks the other phases as needing a redo, since every depth reading changes.

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
  SANDBOX_CALIB_KINECT_ETC_DIR      default /usr/local/etc/Vrui-8.0/Kinect-3.10
  SANDBOX_CALIB_DEPTH               1 shows the depth lens phase (hidden by default)
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
DEFAULT_SCALE = 0.66  # the wizard draws inside a centred panel this fraction of the screen (see --scale)

# Internal phase ids. What the user sees is the position in ALL_PHASES, so hiding a phase
# renumbers the rest; never print these.
PH_DEPTH, PH_PLANE, PH_CORNERS, PH_PROJECTOR = 0, 1, 2, 3
PHASE_NAMES = {PH_DEPTH: "Depth lens", PH_PLANE: "Base plane", PH_CORNERS: "Box corners", PH_PROJECTOR: "Projector"}
CORE_PHASES = (PH_PLANE, PH_CORNERS, PH_PROJECTOR)
PHASE_ORDER = (PH_DEPTH,) + CORE_PHASES
ALL_PHASES = CORE_PHASES  # set_depth_phase() rewrites this


def set_depth_phase(enabled):
    """Show or hide the camera depth lens phase. It is off by default: it needs a flat board at
    several distances, is only worth doing once per camera, and the wizard is normally used for
    the three phases that follow. SANDBOX_CALIB_DEPTH=1 brings it back as the first phase."""
    global ALL_PHASES
    ALL_PHASES = PHASE_ORDER if enabled else CORE_PHASES


def depth_phase_enabled():
    return PH_DEPTH in ALL_PHASES


def phase_number(phase):
    """The number the user sees for a phase: its position among the visible ones."""
    phases = ALL_PHASES if phase in ALL_PHASES else PHASE_ORDER
    return phases.index(phase) + 1


def phase_range(phases):
    """e.g. "phases 1 to 3", for warnings about what has to be recalibrated."""
    numbers = sorted(phase_number(p) for p in phases)
    return "phases %d to %d" % (numbers[0], numbers[-1])


set_depth_phase(os.environ.get("SANDBOX_CALIB_DEPTH", "").strip().lower() in ("1", "on", "yes", "true"))
DEPTH_GOOD_CAPTURES = 4  # distances the wizard asks for before suggesting key 2 (the tool needs 2)
DEPTH_FILE_PREFIX = "DepthCorrection-"  # <prefix><camera serial>.dat, written by RawKinectViewer
KINECT_ETC_CANDIDATES = ("/usr/local/etc/Vrui-8.0/Kinect-3.10", "/usr/local/etc/Kinect-3.10",
                         "/etc/Vrui-8.0/Kinect-3.10")
# Vrui configuration merged into RawKinectViewer for phase 1 only (-mergeConfig <file>). It
# unbinds the plane and corner tools (an empty bindings list disables a tool section) and puts
# "Calibrate Depth Lens" on keys 1 (Save Plane) and 2 (Calibrate). The section names mirror
# .config/Vrui-8.0/Applications/RawKinectViewer.cfg from the payload.
DEPTH_TOOLS_CFG = """\
section Vrui
    section Desktop
        section Tools
            section DefaultTools
                section RawKinectViewerTool0
                    bindings ()
                endsection
                section RawKinectViewerTool1
                    bindings ()
                endsection
                section SandboxDepthLensTool
                    toolClass DepthCorrectionTool
                    bindings ((Mouse, 1, 2))
                endsection
            endsection
        endsection
    endsection
endsection
"""


def content_scale(value=None):
    """The panel scale from --scale or SANDBOX_CALIB_SCALE, clamped to 0.4..1.0."""
    if value is None:
        value = os.environ.get("SANDBOX_CALIB_SCALE", "")
    try:
        scale = float(value)
    except (TypeError, ValueError):
        scale = DEFAULT_SCALE
    return max(0.4, min(1.0, scale))


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
        # Kinect configuration directory: RawKinectViewer's "Calibrate Depth Lens" tool writes
        # DepthCorrection-<serial>.dat there (compiled-in path, root-owned after the install).
        self.kinect_etc_dir = env("SANDBOX_CALIB_KINECT_ETC_DIR", "")
        if not self.kinect_etc_dir:
            self.kinect_etc_dir = next((d for d in KINECT_ETC_CANDIDATES if os.path.isdir(d)),
                                       KINECT_ETC_CANDIDATES[0])
        self.box_layout = os.path.join(self.etc_dir, "BoxLayout.txt")
        self.sandbox_cfg = os.path.join(self.etc_dir, "SARndbox.cfg")
        self.box_layout_orig = self.box_layout + ".orig"
        self.projector_matrix = os.path.join(self.etc_dir, "ProjectorMatrix.dat")
        self.projector_matrix_orig = self.projector_matrix + ".orig"
        self.state_file = os.path.join(self.etc_dir, "calibration-state.json")
        self.log_file = os.path.join(self.etc_dir, "calibration.log")
        self.backup_dir = os.path.join(self.etc_dir, "backups")
        # "<output> <rotation>" written by the Flip projector button; bin/apply-display-rotation.sh
        # re-applies it at login and before the sandbox starts.
        self.rotation_file = os.path.join(self.etc_dir, "display-rotation")
        # Vrui tool bindings for phase 1 (DEPTH_TOOLS_CFG), written before RawKinectViewer starts
        self.depth_tools_cfg = os.path.join(self.etc_dir, "DepthLensTools.cfg")

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
            ("Kinect config dir", self.kinect_etc_dir, os.path.isdir(self.kinect_etc_dir)),
            ("  writable", "yes" if kinect_etc_writable(self.kinect_etc_dir)
             else "no: the depth lens phase offers to fix it (pkexec)", True),
        ]
        intrinsics = sorted(glob.glob(os.path.join(self.kinect_etc_dir, "IntrinsicParameters-*.dat")))
        items.append(("Camera intrinsics", ", ".join(os.path.basename(p) for p in intrinsics)
                      or "none: run  sudo KinectUtil getCalib 0", bool(intrinsics)))
        depth = depth_file_for(self.kinect_etc_dir)
        items.append(("Depth correction", "%s (%s)" % (os.path.basename(depth[0]),
                      datetime.datetime.fromtimestamp(depth[2]).strftime("%Y-%m-%d %H:%M")) if depth
                      else "none (not calibrated)", True))
        output, rotation = detect_display()
        items.append(("Display (xrandr)", "%s, rotation %s" % (output, rotation), output is not None))
        saved = read_rotation_file(self.rotation_file)
        items.append(("Saved projector flip", "%s %s (%s)" % (saved[0], saved[1], self.rotation_file) if saved
                      else "none (normal orientation)", True))
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
# "sandboxWatch on": the plugin reports RawKinectViewer's capture dialog and Vrui error popups
HELPER_WATCH_RE = re.compile(r"^SandboxHelper: (capture started|capture done|watching|not watching|popup (.*))$")
DEPTH_WRITTEN_RE = re.compile(r"Writing depth correction file\s+(\S+)")
MERGE_MISSING_RE = re.compile(r"Requested configuration file (\S+) not found")
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
        info["ts"] = time.time()  # exact time, for before/after comparisons
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


def read_rotation_file(path):
    """Return (output, rotation) saved by the Flip projector button, or None."""
    try:
        with open(path) as f:
            parts = f.read().split()
    except OSError:
        return None
    if len(parts) >= 2 and parts[1] in FLIPPED_ROTATION:
        return parts[0], parts[1]
    return None


def write_rotation_file(path, output, rotation):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("%s %s\n" % (output, rotation))
        return True
    except OSError:
        return False


def depth_file_info(path):
    """(path, serial, mtime) for a DepthCorrection-<serial>.dat path, or None if it is not there."""
    name = os.path.basename(path)
    if not (name.startswith(DEPTH_FILE_PREFIX) and name.endswith(".dat")):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return path, name[len(DEPTH_FILE_PREFIX):-4], mtime


def depth_files(kinect_etc_dir):
    """All depth correction files in the Kinect config directory, newest first."""
    items = [depth_file_info(p) for p in glob.glob(os.path.join(kinect_etc_dir, DEPTH_FILE_PREFIX + "*.dat"))]
    return sorted([i for i in items if i], key=lambda i: -i[2])


def depth_file_for(kinect_etc_dir, serial=None):
    """(path, serial, mtime) of the camera's depth correction file (any camera when serial is
    None), or None."""
    for item in depth_files(kinect_etc_dir):
        if serial is None or item[1] == serial:
            return item
    return None


def remove_depth_file(item, backup_dir, log):
    """Throw away one depth correction file, keeping a copy in the backups folder.
    Returns (ok, backup path or error text)."""
    path = item[0]
    backup = backup_file(path, backup_dir)
    try:
        os.remove(path)
    except OSError as e:
        return False, "%s could not be deleted: %s" % (os.path.basename(path), e.strerror or e)
    log.write("Deleted %s (backup: %s)" % (path, backup))
    return True, backup


def kinect_etc_writable(kinect_etc_dir):
    """True when RawKinectViewer, running as this user, can write the depth correction file."""
    if not os.access(kinect_etc_dir, os.W_OK | os.X_OK):
        return False
    return all(os.access(path, os.W_OK) for path, _, _ in depth_files(kinect_etc_dir))


def fix_kinect_etc_permissions(kinect_etc_dir, log):
    """Give the Kinect config directory to this user through pkexec (a password prompt).
    Returns (ok, message)."""
    owner = "%d:%d" % (os.getuid(), os.getgid())
    # pkexec needs the program as an absolute path and runs it as root with a clean environment
    argv = ["pkexec", "/bin/sh", "-c", 'chown -R "$1" "$2" && chmod -R u+rwX "$2"', "fix-permissions",
            owner, kinect_etc_dir]
    log.write("Running: %s" % " ".join(argv))
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return False, "pkexec could not be run: %s" % e
    output = proc.stdout.strip()
    if proc.returncode == 0 and kinect_etc_writable(kinect_etc_dir):
        log.write("%s now belongs to %s" % (kinect_etc_dir, owner))
        return True, "Done: %s now belongs to you." % kinect_etc_dir
    if proc.returncode in (126, 127):
        return False, "Cancelled, or the password was not accepted."
    return False, "pkexec failed (exit code %s): %s" % (proc.returncode, output or "no output")


def write_depth_tools_cfg(path):
    with open(path, "w") as f:
        f.write(DEPTH_TOOLS_CFG)


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


class DepthSession(Session):
    """Phase 1: per-pixel depth correction with the "Calibrate Depth Lens" tool (keys 1 and 2).
    Key 1 captures an averaged depth frame (RawKinectViewer shows its 'Capturing average depth
    frame...' dialog meanwhile), key 2 computes the correction and writes
    DepthCorrection-<serial>.dat, printing 'Writing depth correction file <path>'. Failures only
    show up as a Vrui error popup; the SandboxHelper plugin relays both the dialog and such
    popups ('SandboxHelper: capture done', 'SandboxHelper: popup <title>: <text>') after
    'sandboxWatch on'."""
    tool_name = "RawKinectViewer"
    phases = (PH_DEPTH,)

    def __init__(self, helper_expected=False):
        Session.__init__(self, helper_expected)
        self.connected = False
        self.serial = None
        self.captures = 0
        self.capturing = False
        self.written = None        # path from the tool's "Writing depth correction file" line
        self.failure = None        # text of the tool's error popup, relayed by the plugin
        self.merge_missing = None  # Vrui did not find the key bindings file
        self.started_at = time.time()

    def title(self):
        return "Phase %d: camera depth lens" % phase_number(PH_DEPTH)

    def argv(self, paths):
        return ([paths.raw_kinect_viewer, "-compress", "0", "-mergeConfig", paths.depth_tools_cfg]
                + self.vislet_args(paths))

    def intro_sections(self):
        return [("What this does",
                 "The camera sees a flat surface slightly bowl-shaped (lens distortion). This phase "
                 "measures that on a flat surface at several distances and saves a per-pixel "
                 "correction for this camera. Do it once per camera. The other %s must be redone "
                 "afterwards because every depth reading changes." % phase_range(CORE_PHASES)),
                ("Get ready",
                 "\u2022 Flatten the sand: it is the last capture.\n"
                 "\u2022 Find a large flat board (foam board, plywood, a table top) and boxes to prop it "
                 "at two or three heights between camera and sand.\n"
                 "\u2022 Or take the camera off its mount and point it at a blank wall from the same "
                 "distances.\n"
                 "\u2022 Work in the LEFT half of the window (the depth image)."),
                ("For each distance (at least %d, more is better)" % DEPTH_GOOD_CAPTURES,
                 "1. Start close: the surface about 50 cm from the camera and square to it, filling the "
                 "LEFT image with no black holes and nothing else in view.\n"
                 "2. Press key 1 and keep everything still for about 5 seconds, until the capture "
                 "dialog disappears. A popup counts the capture.\n"
                 "3. Move the surface further away until the colors change, check that it still fills "
                 "the image, press 1 again."),
                ("After the last capture",
                 "Press key 2: the correction is computed and saved by itself, and a popup confirms it. "
                 "An error popup means too few captures or too many black pixels, so capture more "
                 "distances with key 1 and press 2 again. Then press Esc to finish.")]

    def intro_text(self, ctx=None):
        return "\n\n".join("%s\n%s" % sec for sec in self.intro_sections())

    def initial_message(self):
        return ("PHASE %d - DEPTH LENS: capture 1 of at least %d. Fill the LEFT image with a flat "
                "surface about 50 cm from the camera, no black holes. Press key 1 and keep "
                "everything still for 5 seconds." % (phase_number(PH_DEPTH), DEPTH_GOOD_CAPTURES))

    def on_helper_loaded(self):
        self.commands.append("sandboxWatch on")
        return self.initial_message()

    def on_helper_failed(self):
        self.status = "The SandboxHelper plugin did not load: captures are not counted here."
        return self.initial_message()

    def capture_message(self):
        n = self.captures
        if n < DEPTH_GOOD_CAPTURES:
            return ("Capture %d done. Move the surface further from the camera (the colors should "
                    "change), keep it flat, square and filling the LEFT image, then press 1 again. "
                    "After %d captures press 2 to compute." % (n, DEPTH_GOOD_CAPTURES))
        return ("Capture %d done. Press 1 for more distances (the flattened sand last), or press "
                "2 to compute and save the correction." % n)

    def saved_message(self):
        return ("Depth correction computed and saved (%d capture%s). Press Esc to finish."
                % (self.captures, "" if self.captures == 1 else "s"))

    def permission_problem(self):
        t = (self.failure or "").lower()
        return "permission" in t or "open file" in t or "could not open" in t or "cannot open" in t

    def failure_message(self):
        if self.permission_problem():
            return ("The correction could NOT be saved: RawKinectViewer may not write into the "
                    "Kinect config folder. Press Esc; the wizard offers to fix the permissions.")
        return ("The computation FAILED (too few captures, or too many black pixels). Capture "
                "more distances with key 1, then press 2 again.")

    def reminder(self):
        if self.written and not self.failure:
            return self.saved_message()
        if self.capturing:
            return "Capturing: keep everything still until the capture dialog disappears."
        if self.captures == 0:
            return self.initial_message()
        return self.capture_message()

    def on_line(self, line):
        handled, message = self.helper_line(line)
        if handled:
            return message
        m = HELPER_WATCH_RE.search(line)
        if m:
            what = m.group(1)
            if what == "capture started":
                self.capturing = True
                self.status = "Capturing: keep everything still..."
            elif what == "capture done":
                self.capturing = False
                self.captures += 1
                self.status = "%d capture%s so far" % (self.captures, "" if self.captures == 1 else "s")
                if self.captures >= DEPTH_GOOD_CAPTURES:
                    self.status += ". Press 2 in RawKinectViewer to compute, or 1 for more."
                return self.capture_message()
            elif what.startswith("popup") and "Calibrate Depth Lens" in m.group(2):
                self.failure = m.group(2)
                self.status = "The tool reported an error: " + self.failure
                return self.failure_message()
            return None
        m = CONNECTED_RE.search(line)
        if m:
            self.serial = m.group(1)
            self.connected = True
            self.status = "Camera %s connected. Waiting for the first capture (key 1)..." % self.serial
            return None
        m = EXCEPTION_RE.search(line)
        if m:
            self.error = m.group(1)
            self.status = "RawKinectViewer stopped: " + self.error
            return None
        m = DEPTH_WRITTEN_RE.search(line)
        if m:
            self.written = m.group(1)
            self.failure = None
            self.status = "Depth correction written to " + self.written
            return self.saved_message()
        m = MERGE_MISSING_RE.search(line)
        if m:
            self.merge_missing = m.group(1)
            self.status = ("Key bindings file %s not found: keys 1 and 2 still drive the plane and "
                           "corner tools" % self.merge_missing)
            return None
        return None

    def missing(self):
        if self.written is None and self.failure is None:
            return ["no depth correction was computed (press 2 in RawKinectViewer after the captures)"]
        return []


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
        if self.phases == (PH_PLANE, PH_CORNERS):
            return ("Phases %d and %d: base plane and box corners"
                    % (phase_number(PH_PLANE), phase_number(PH_CORNERS)))
        if self.phases == (PH_PLANE,):
            return "Phase %d: base plane" % phase_number(PH_PLANE)
        return "Phase %d: box corners" % phase_number(PH_CORNERS)

    def argv(self, paths):
        return [paths.raw_kinect_viewer, "-compress", "0"] + self.vislet_args(paths)

    def intro_sections(self):
        """List of (heading, text). The last entry is shown as the closing note."""
        sections = [("Get ready",
                     "\u2022 Flatten and smooth the sand as evenly as you can.\n"
                     "\u2022 Nothing but sand inside the box: no hands, tools or toys.\n"
                     "\u2022 RawKinectViewer opens full screen. Work in the LEFT half (the depth image) "
                     "and ignore the RIGHT half (the color camera).\n"
                     "\u2022 Instructions pop up inside that window as you go. Click OK (or 'Jolly Good!') to "
                     "dismiss them.")]
        if PH_PLANE in self.phases and self.helper_expected:
            sections.append(("Phase %d \u00b7 Base plane" % phase_number(PH_PLANE),
                             "1. The wizard captures the flat sand by itself right after the window opens "
                             "('Capturing average depth frame...' shows for about 5 seconds). Keep hands out "
                             "until the popup says the sand is captured.\n"
                             "2. Hold down the 1 key and drag a rectangle over a large, flat area of sand in the "
                             "LEFT image, then release the key. Stay inside the sand.\n"
                             "Not happy? Drag again: the last rectangle counts. Touched the sand? Click "
                             "'Capture the sand again' in this window first."))
        elif PH_PLANE in self.phases:
            sections.append(("Phase %d \u00b7 Base plane" % phase_number(PH_PLANE),
                             "1. Press and hold the RIGHT mouse button, move onto 'Average Frames' in the menu "
                             "that pops up, and release. Wait until 'Capturing average depth frame...' disappears.\n"
                             "2. Hold down the 1 key and drag a rectangle over a large, flat area of sand in the "
                             "LEFT image, then release the key. Stay inside the sand.\n"
                             "Not happy? Drag again: the last rectangle counts."))
        if PH_CORNERS in self.phases:
            sections.append(("Phase %d \u00b7 Box corners" % phase_number(PH_CORNERS),
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

    @property
    def PLANE_TAG(self):
        return "PHASE %d - BASE PLANE" % phase_number(PH_PLANE)

    @property
    def CORNER_TAG(self):
        return "PHASE %d - CORNERS" % phase_number(PH_CORNERS)

    DRAG_PROMPT = ("Hold down key 1 and drag a box over a large, flat area of sand in the LEFT image, "
                   "then release the key.")

    def initial_message(self):
        if self.helper_expected:
            phase = (self.PLANE_TAG if PH_PLANE in self.phases else self.CORNER_TAG)
            return ("%s: starting up. The flat sand is captured automatically in a moment: keep "
                    "hands and tools out of the box." % phase)
        return self.manual_message()

    def manual_message(self):
        """Instructions for the case without the SandboxHelper plugin."""
        if PH_PLANE in self.phases:
            return ("%s: hold the right mouse button, pick Average Frames, release. "
                    "Wait for the capture. Then hold key 1 and drag a box over flat sand in the LEFT image."
                    % self.PLANE_TAG)
        return self._corner_prompt(self.CORNER_TAG + ": ")

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
        if PH_PLANE in self.phases and self.plane is None:
            return "Sand captured. " + self.DRAG_PROMPT
        if PH_PLANE in self.phases:
            return ("Sand captured again. Drag again with key 1 to redo the base plane (the last one "
                    "counts), or carry on.")
        return self._corner_prompt("Sand captured. " + self.CORNER_TAG + ": ")

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
        if PH_PLANE in self.phases and self.plane is None:
            if self.helper and self.average_ready:
                return "Sand captured. " + self.DRAG_PROMPT
            return self.manual_message() if (self.helper_failed or not self.helper_expected) else self.initial_message()
        if PH_CORNERS in self.phases:
            n = len(self.points)
            if n == 0:
                return self._corner_prompt(self.CORNER_TAG + ": ")
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
            if PH_PLANE not in self.phases:
                return None
            if PH_CORNERS in self.phases and not self._phase2_prompted:
                self._phase2_prompted = True
                return self._corner_prompt("Plane captured (offset %.1f cm). %s: "
                                           % (self.plane[3], self.CORNER_TAG))
            if PH_CORNERS not in self.phases:
                return ("Plane captured (offset %.1f cm). Press Esc to finish, or drag again "
                        "to redo. The last one counts." % self.plane[3])
            return None
        point = parse_point_line(line)
        if point is not None:
            self.points.append(point)
            if PH_CORNERS not in self.phases:
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
        if PH_PLANE in self.phases and self.plane is None:
            out.append("no base plane was captured")
        if PH_CORNERS in self.phases and len(self.points) < 4:
            out.append("only %d of 4 corners were clicked" % len(self.points))
        return out


class ProjectorSession(Session):
    tool_name = "CalibrateProjector"
    phases = (PH_PROJECTOR,)

    def __init__(self, width, height, helper_expected=False):
        Session.__init__(self, helper_expected)
        self.width = width
        self.height = height
        self.tie_points = 0
        self.rms = None
        self.calib_error = None
        self.started_at = time.time()

    def title(self):
        return "Phase %d: projector calibration" % phase_number(PH_PROJECTOR)

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
        return ("PHASE %d - PROJECTOR: hold the disk where the white cross is and press key 1. "
                "Repeat for all %d points. Press key 2 after changing the sand."
                % (phase_number(PH_PROJECTOR), NUM_TIE_POINTS))

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
            return ("Calibration FAILED: some points were bad. Press Esc, then run Phase %d "
                    "again from scratch." % phase_number(PH_PROJECTOR))
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
    "surround": "#0b0f19",
}

def build_app(paths, log=None, state=None, scale=None):
    """Build and return the Tk application class bound to the given paths."""
    import tkinter as tk
    from tkinter import ttk, messagebox, font as tkfont

    log = log or Log(paths.log_file)
    state = state or State(paths.state_file)
    SCALE = content_scale(scale)
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
            # Layout designed for a 1024x768 screen; everything is scaled by SCALE and drawn inside a
            # centred panel so the content lands on the sand, not on the box edges.
            self.scale = SCALE
            self.minsize(900, 640)
            # Full screen when scaled: the dark surround should cover the whole projector image and a
            # maximised window would lose the title bar and panel height. F11 toggles it.
            self.fullscreen = SCALE < 0.999
            try:
                if self.fullscreen:
                    self.attributes("-fullscreen", True)
                else:
                    self.attributes("-zoomed", True)
            except tk.TclError:
                self.geometry("1000x720")
            self.bind("<F11>", self.toggle_fullscreen)
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.configure(bg=C["surround"] if SCALE < 0.999 else C["bg"])
            self._setup_fonts(tkfont)
            self._setup_styles(ttk)
            self.panel = tk.Frame(self, bg=C["bg"])
            self.panel.place(relx=0.5, rely=0.5, anchor="center", relwidth=SCALE, relheight=SCALE)

            self.queue = []          # phases still to run
            self.session = None
            self.runner = None
            self.body = tk.Frame(self.panel, bg=C["bg"], padx=self.px(28), pady=self.px(22))
            self.body.pack(fill="both", expand=True)
            self.log_lines = []
            try:
                with open(paths.log_file) as f:
                    self.log_lines = [l.rstrip("\n") for l in f.readlines()[-30:]]
            except OSError:
                pass
            log.listeners.append(self._on_log_line)
            self.log_widget = None
            self.log_frame = None
            self.pulse_canvas = None
            self._sandbox_bar_key = None
            self._last_bar_refresh = 0.0
            self.resolution = detect_resolution()
            self.display_output, self.display_rotation = detect_display()
            self.xbg_proc = None
            self.show_hub()
            self.after(200, self._poll)

        def toggle_fullscreen(self, event=None):
            self.fullscreen = not self.fullscreen
            try:
                self.attributes("-fullscreen", self.fullscreen)
                if not self.fullscreen:
                    self.attributes("-zoomed", True)
            except tk.TclError:
                pass

        # ---------------- look and feel ----------------
        def px(self, n):
            """Scale a design pixel value."""
            return int(round(n * self.scale))

        def fs(self, n):
            """Scale a design font size (points)."""
            return max(6, int(round(n * self.scale)))

        def _setup_fonts(self, tkfont):
            families = set(tkfont.families())
            default_family = tkfont.nametofont("TkDefaultFont").cget("family")
            ui = next((f for f in ("Inter", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans",
                                   "Liberation Sans") if f in families), default_family)
            mono = next((f for f in ("JetBrains Mono", "Ubuntu Mono", "DejaVu Sans Mono", "Noto Sans Mono",
                                     "Liberation Mono") if f in families),
                        tkfont.nametofont("TkFixedFont").cget("family"))

            def F(size, weight="normal"):
                return tkfont.Font(family=ui, size=self.fs(size), weight=weight)

            self.f_title = F(22, "bold")
            self.f_display = F(34, "bold")
            self.f_h2 = F(15, "bold")
            self.f_body = F(12)
            self.f_body_b = F(12, "bold")
            self.f_small = F(10)
            self.f_small_b = F(10, "bold")
            self.f_btn = F(12, "bold")
            self.f_mono = tkfont.Font(family=mono, size=self.fs(12))
            self.f_mono_small = tkfont.Font(family=mono, size=self.fs(10))
            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
                tkfont.nametofont(name).configure(family=ui, size=self.fs(12))

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
                         borderwidth=0, focusthickness=0, focuscolor=C["accent"], padding=(self.px(20), self.px(11)),
                         font=self.f_btn)
            st.map("Primary.TButton",
                   background=[("disabled", C["accent_soft"]), ("pressed", C["accent_hover"]),
                               ("active", C["accent_hover"])],
                   bordercolor=[("active", C["accent_hover"])],
                   lightcolor=[("active", C["accent_hover"])], darkcolor=[("active", C["accent_hover"])])
            st.configure("Secondary.TButton", background=C["card"], foreground=C["text"],
                         bordercolor=C["border_strong"], lightcolor=C["card"], darkcolor=C["card"],
                         borderwidth=1, focusthickness=0, focuscolor=C["card"], padding=(self.px(16), self.px(10)),
                         font=self.f_body_b)
            st.map("Secondary.TButton",
                   background=[("pressed", C["border"]), ("active", "#f9fafb")],
                   bordercolor=[("active", C["faint"])])
            st.configure("Small.Secondary.TButton", padding=(self.px(12), self.px(6)), font=self.f_small_b)
            st.configure("Small.Primary.TButton", padding=(self.px(12), self.px(6)), font=self.f_small_b)
            st.configure("Danger.TButton", background=C["card"], foreground=C["bad"],
                         bordercolor="#fecaca", lightcolor=C["card"], darkcolor=C["card"],
                         borderwidth=1, focusthickness=0, focuscolor=C["card"], padding=(self.px(16), self.px(10)),
                         font=self.f_body_b)
            st.map("Danger.TButton", background=[("active", C["bad_bg"])], bordercolor=[("active", C["bad"])])
            # Check buttons and entries on white cards
            st.configure("Card.TCheckbutton", background=C["card"], foreground=C["text"],
                         focuscolor=C["card"], indicatorbackground=C["card"], indicatorforeground=C["accent"],
                         indicatorcolor=C["card"], indicatorsize=self.px(16), indicatormargin=(2, 2, 6, 2), padding=self.px(4))
            st.map("Card.TCheckbutton", background=[("active", C["card"])],
                   indicatorbackground=[("selected", C["accent"]), ("active", C["card"])],
                   indicatorforeground=[("selected", C["accent_text"])],
                   indicatorcolor=[("selected", C["accent"])])
            st.configure("TEntry", fieldbackground=C["card"], background=C["card"], foreground=C["text"],
                         bordercolor=C["border_strong"], lightcolor=C["card"], darkcolor=C["card"],
                         insertcolor=C["text"], padding=(self.px(8), self.px(6)))
            st.map("TEntry", bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["accent"])],
                   darkcolor=[("focus", C["accent"])])
            st.configure("Horizontal.TScale", background=C["accent"], troughcolor=C["border"],
                         bordercolor=C["accent"], lightcolor=C["accent"], darkcolor=C["accent"],
                         sliderlength=self.px(30), sliderthickness=self.px(22), gripcount=0)
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
            self.log_frame = None
            self.pulse_canvas = None
            self._sandbox_bar_key = None

        def card(self, parent, padx=20, pady=16, fill="x", expand=False, gap=(0, 12), side=None):
            outer = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"],
                             highlightcolor=C["border"], bd=0)
            gap = tuple(self.px(g) for g in gap)
            if side:
                outer.pack(side=side, fill=fill, expand=expand, pady=gap, padx=(self.px(0), self.px(12)))
            else:
                outer.pack(fill=fill, expand=expand, pady=gap)
            inner = tk.Frame(outer, bg=C["card"], padx=self.px(padx), pady=self.px(pady))
            inner.pack(fill="both", expand=True)
            return inner

        def label(self, parent, text, font=None, fg=None, bg=None, **kw):
            bg = bg or parent.cget("background")
            return tk.Label(parent, text=text, font=font or self.f_body, fg=fg or C["text"], bg=bg,
                            justify="left", **kw)

        def pill(self, parent, text, kind="muted"):
            bg, fg = TINTS[kind]
            return tk.Label(parent, text=text, bg=bg, fg=fg, font=self.f_small_b, padx=self.px(10), pady=self.px(3))

        def notice(self, parent, text, kind, prefix="", pack=True):
            bg, fg = TINTS[kind]
            f = tk.Frame(parent, bg=bg)
            if pack:
                f.pack(fill="x", pady=(self.px(0), self.px(8)))
            tk.Label(f, text=prefix + text, bg=bg, fg=fg, font=self.f_body, wraplength=self.px(880), justify="left",
                     padx=self.px(14), pady=self.px(10)).pack(anchor="w")
            return f

        def header(self, title, subtitle=None):
            row = tk.Frame(self.body, bg=C["bg"])
            row.pack(fill="x")
            right = tk.Frame(row, bg=C["bg"])
            right.pack(side="right", anchor="ne", padx=(self.px(16), self.px(0)))
            if self.display_output:
                self.flip_button = ttk.Button(right, command=self.flip_toggle)
                self.flip_button.pack(side="right", padx=(self.px(10), self.px(0)))
                self.refresh_flip_button()
            status = tk.Frame(right, bg=C["bg"])
            status.pack(side="left")
            left = tk.Frame(row, bg=C["bg"])
            left.pack(side="left", fill="x", expand=True)
            labels = [self.label(left, title, font=self.f_title, wraplength=self.px(760))]
            labels[0].pack(anchor="w")
            if subtitle:
                labels.append(self.label(left, subtitle, fg=C["muted"], wraplength=self.px(760)))
                labels[1].pack(anchor="w", pady=(self.px(2), self.px(0)))

            def rewrap(event, labels=labels):
                for l in labels:
                    if l.winfo_exists():
                        l.configure(wraplength=max(200, event.width - 4))
            left.bind("<Configure>", rewrap)
            tk.Frame(self.body, bg=C["bg"], height=self.px(10)).pack()
            return status

        # ---------------- flip projector (permanent 180 degree rotation) ----------------
        def refresh_flip_button(self):
            b = getattr(self, "flip_button", None)
            if b is None or not b.winfo_exists():
                return
            if self.display_rotation != "normal":
                b.configure(text="\u21c5  Projector flipped (%s)" % self.display_rotation,
                            style="Small.Primary.TButton")
            else:
                b.configure(text="\u21c5  Flip projector", style="Small.Secondary.TButton")

        def flip_toggle(self):
            """Rotate the projector output 180 degrees for good, like Display Settings would."""
            if not self.display_output:
                messagebox.showerror(APP_TITLE, "No display output was found with xrandr.")
                return
            target = FLIPPED_ROTATION.get(self.display_rotation, "inverted")
            what = ("Turn the projector image back to normal?" if target == "normal"
                    else "Turn the projector image upside down?")
            if not messagebox.askokcancel(APP_TITLE, what + "\n\n"
                                          "This is permanent, like Display Settings: it stays after the wizard "
                                          "closes and after a reboot (the sandbox re-applies it at login).\n\n"
                                          "Everything must be recalibrated afterwards: base plane, box corners "
                                          "and projector (%s)." % phase_range(CORE_PHASES)):
                return
            if not set_display_rotation(self.display_output, target, log):
                messagebox.showerror(APP_TITLE, "The display could not be rotated. See the log for the xrandr error.")
                return
            self.display_rotation = target
            if not write_rotation_file(paths.rotation_file, self.display_output, target):
                log.write("Could not save the rotation to %s" % paths.rotation_file)
            state.mark("display_flip", output=self.display_output, rotation=target)
            log.write("Projector output %s rotated to %s; phases done before now need recalibration"
                      % (self.display_output, target))
            if self.session is None and self.runner is None:
                self.show_hub()
            else:
                self.refresh_flip_button()

        STALE_EVENTS = (("display_flip", "the projector flip"), ("depth", "the depth lens calibration"),
                        ("depth_removed", "the depth lens reset"))

        def stale_after(self, done, ts=None):
            """(suffix, kind) for a phase done at 'done' (text stamp) / 'ts' (epoch seconds): bad
            when that was before the last projector flip or depth lens calibration (phase 1)."""
            latest = None
            for key, what in self.STALE_EVENTS:
                event = state.get(key)
                if not event:
                    continue
                if ts is not None and event.get("ts") is not None:
                    before = ts < event["ts"]
                else:
                    before = bool(done) and done < event.get("done", "")
                if before and (latest is None or event.get("ts", 0) > latest[0].get("ts", 0)):
                    latest = (event, what)
            if latest:
                return ("  \u00b7  BEFORE %s of %s: redo" % (latest[1], latest[0].get("done")), "bad")
            return ("", "good")

        def phase_kind(self, phase):
            """'good' when calibrated, 'warn' when factory defaults, 'bad' on error."""
            return self.phase_status(phase)[1]

        def steps(self, parent, current=None):
            """Draw the 1-2-3 step indicator."""
            canvas = tk.Canvas(parent, height=self.px(38), bg=C["bg"], highlightthickness=0)
            canvas.pack(fill="x", pady=(self.px(0), self.px(6)))
            p = self.px
            x = p(20)
            r = p(14)
            cy = p(19)
            for phase in ALL_PHASES:
                kind = self.phase_kind(phase)
                active = current is not None and phase in (current if isinstance(current, tuple) else (current,))
                number = str(phase_number(phase))
                if active:
                    fill, outline, fg, txt = C["accent"], C["accent"], C["accent_text"], number
                elif kind == "good":
                    fill, outline, fg, txt = C["good"], C["good"], "#ffffff", "\u2713"
                else:
                    fill, outline, fg, txt = C["card"], C["border_strong"], C["muted"], number
                canvas.create_oval(x - r, cy - r, x + r, cy + r, fill=fill, outline=outline, width=2)
                canvas.create_text(x, cy, text=txt, fill=fg, font=self.f_small_b)
                name = PHASE_NAMES[phase]
                canvas.create_text(x + r + p(10), cy, text=name, anchor="w", fill=C["text"] if active else C["muted"],
                                   font=self.f_body_b if active else self.f_body)
                text_w = self.f_body_b.measure(name)
                x_end = x + r + p(10) + text_w + p(14)
                if phase < ALL_PHASES[-1]:
                    canvas.create_line(x_end, cy, x_end + p(40), cy, fill=C["border_strong"], width=2)
                    x = x_end + p(40) + r + p(4)
            return canvas

        def badge(self, parent, phase, kind, bg, text=None):
            size = self.px(36)
            cv = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0)
            if kind == "good":
                fill, fg, txt = C["good"], "#ffffff", "\u2713"
            else:
                fill, fg, txt = C["accent"], "#ffffff", str(phase_number(phase))
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
            inner = self.card(parent, padx=self.px(0), pady=self.px(0), fill="both", expand=True, gap=(0, 0))
            self.log_frame = inner.master  # the block that gives way when a screen is taller than the panel
            head = tk.Frame(inner, bg=C["card"], padx=self.px(16), pady=self.px(8))
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
                           highlightthickness=0, padx=self.px(14), pady=self.px(10), insertbackground=C["log_fg"])
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
            row.pack(fill="x", pady=(self.px(14), self.px(0)))
            return row

        # ---------------- data helpers ----------------
        def layout_values(self):
            """Return (plane, corners, error_text)."""
            try:
                plane, corners = read_box_layout(paths.box_layout)
                return plane, corners, None
            except (OSError, ValueError) as e:
                return None, None, str(e)

        def depth_status(self):
            """(text, kind, (path, serial, mtime) or None) for phase 1, from the Kinect config dir."""
            info = state.get("depth")
            item = depth_file_for(paths.kinect_etc_dir, info.get("serial") if info else None)
            if item is None:
                item = depth_file_for(paths.kinect_etc_dir)
            if item is None:
                return ("Not calibrated yet  \u00b7  optional, once per camera", "warn", None)
            path, serial, mtime = item
            if info and abs(info.get("mtime", -1) - mtime) < 1.0:
                text = "Done %s" % info["done"]
                if info.get("captures"):
                    text += "  \u00b7  %d captures" % info["captures"]
                return (text, "good", item)
            stamp = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            return ("File dated %s  \u00b7  calibrated outside this wizard" % stamp, "good", item)

        def phase_status(self, phase):
            """Return (text, kind) describing the status of a phase; kind is good/warn/bad."""
            if phase == PH_DEPTH:
                return self.depth_status()[:2]
            if phase in (PH_PLANE, PH_CORNERS):
                plane, corners, err = self.layout_values()
                if err:
                    return ("BoxLayout.txt unreadable: " + err, "bad")
                key = "plane" if phase == PH_PLANE else "corners"
                info = state.get(key)
                current = format_plane(plane) if phase == PH_PLANE else [format_point(c) for c in corners]
                factory = None
                if os.path.isfile(paths.box_layout_orig):
                    try:
                        oplane, ocorners = read_box_layout(paths.box_layout_orig)
                        factory = format_plane(oplane) if phase == PH_PLANE else [format_point(c) for c in ocorners]
                    except (OSError, ValueError):
                        factory = None
                if info and info.get("value") == current:
                    extra = ""
                    if phase == PH_PLANE and info.get("rms") is not None:
                        extra = "  \u00b7  fit RMS %.2f cm" % info["rms"]
                    suffix, kind = self.stale_after(info["done"], info.get("ts"))
                    return ("Done %s%s%s" % (info["done"], extra, suffix), kind)
                if factory is not None and current == factory:
                    return ("Not calibrated yet  \u00b7  factory defaults", "warn")
                if info:
                    return ("Edited by hand after %s" % info["done"], "good")
                return ("Values present  \u00b7  calibrated outside this wizard", "good")
            # the projector phase
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
                suffix, kind = self.stale_after(info["done"], info.get("ts"))
                return (text + suffix, kind)
            if os.path.isfile(paths.projector_matrix_orig) and files_equal(paths.projector_matrix,
                                                                           paths.projector_matrix_orig):
                return ("Not calibrated yet  \u00b7  factory defaults", "warn")
            stamp = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            suffix, kind = self.stale_after(stamp, mtime)
            return ("Matrix file dated %s  \u00b7  calibrated outside this wizard%s" % (stamp, suffix), kind)

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

        def write_color_height(self, base_plane, delta, live=True, make_backup=True):
            """Persist the colour plane for `delta` in SARndbox.cfg and push it to a running sandbox."""
            hmp = height_map_plane_for(base_plane, delta)
            backup = backup_file(paths.sandbox_cfg, paths.backup_dir) if make_backup else "not repeated"
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
                "Positive raises the sea level (more blue), negative lowers it (more land). Every change "
                "is saved by itself and a running sandbox shows it immediately.")
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
            self._ch_backed_up = False  # SARndbox.cfg is backed up once per visit, not once per nudge

            card = self.card(self.body, padx=self.px(24), pady=self.px(20))
            top = tk.Frame(card, bg=C["card"])
            top.pack(fill="x")
            self.label(top, "Sea level offset from the calibrated base plane", fg=C["muted"]).pack(anchor="w")
            self.ch_display = self.label(top, self.format_delta(delta), font=self.f_display, fg=C["accent"])
            self.ch_display.pack(anchor="w", pady=(self.px(2), self.px(8)))

            self.ch_scale_var = tk.DoubleVar(value=delta)
            scale = ttk.Scale(card, from_=-20.0, to=20.0, orient="horizontal", variable=self.ch_scale_var,
                              command=self.ch_scale_moved, style="Horizontal.TScale")
            scale.pack(fill="x", pady=(self.px(0), self.px(2)))
            ticks = tk.Frame(card, bg=C["card"])
            ticks.pack(fill="x")
            self.label(ticks, "-20 cm  lower sea level", font=self.f_small, fg=C["muted"]).pack(side="left")
            self.label(ticks, "raise sea level  +20 cm", font=self.f_small, fg=C["muted"]).pack(side="right")

            steps = tk.Frame(card, bg=C["card"])
            steps.pack(fill="x", pady=(self.px(16), self.px(0)))
            for text, step in (("-5", -5.0), ("-1", -1.0), ("-0.5", -0.5)):
                ttk.Button(steps, text=text, style="Secondary.TButton", width=6,
                           command=lambda d=step: self.ch_set(self.ch_value + d)).pack(side="left", padx=(self.px(0), self.px(6)))
            ttk.Button(steps, text="Reset to 0", style="Secondary.TButton",
                       command=lambda: self.ch_set(0.0)).pack(side="left", padx=(self.px(12), self.px(12)))
            for text, step in (("+0.5", 0.5), ("+1", 1.0), ("+5", 5.0)):
                ttk.Button(steps, text=text, style="Secondary.TButton", width=6,
                           command=lambda d=step: self.ch_set(self.ch_value + d)).pack(side="left", padx=(self.px(0), self.px(6)))

            self.ch_hint = tk.Frame(self.body, bg=C["bg"])
            self.ch_hint.pack(fill="x")
            self.ch_refresh_hint()

            buttons = self.button_row()
            ttk.Button(buttons, text="Back to overview", style="Primary.TButton",
                       command=self.ch_back).pack(side="right")

        def ch_refresh_hint(self):
            f = getattr(self, "ch_hint", None)
            if f is None or not f.winfo_exists():
                return
            for w in f.winfo_children():
                w.destroy()
            if bool(sandbox_pids(paths.sandbox_process)):
                text = "The running sandbox is showing this value. "
                kind = "good"
            else:
                text = "The sandbox is not running, so you cannot see the effect yet; launch it to preview. "
                kind = "info"
            self.notice(f, text + "Changes are written to SARndbox.cfg as you make them, so they are "
                        "there at the next start.", kind)

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
            # save and show it once the slider settles, so a drag writes the file once, not per pixel
            if self._ch_pending is not None:
                self.after_cancel(self._ch_pending)
            self._ch_pending = self.after(250, self.ch_commit)
            self.ch_refresh_hint()

        def ch_commit(self):
            """Write the value to SARndbox.cfg and push it to a running sandbox."""
            self._ch_pending = None
            if self.ch_saved is not None and abs(self.ch_value - self.ch_saved) < 1e-9:
                return
            self.write_color_height(self.ch_plane, self.ch_value, make_backup=not self._ch_backed_up)
            self._ch_backed_up = True
            self.ch_saved = self.ch_value
            self.ch_refresh_hint()

        def ch_flush(self):
            """Write a change that is still waiting for the slider to settle."""
            if getattr(self, "_ch_pending", None) is None:
                return
            self.after_cancel(self._ch_pending)
            self.ch_commit()

        def ch_back(self):
            self.ch_flush()
            self.show_hub()

        # ---------------- hub ----------------
        def show_hub(self):
            self.clear()
            self.session = None
            self.sandbox_bar = self.header("Sandbox calibration",
                                           "Tick the phases to run and press Run. Base plane and box corners "
                                           "share one RawKinectViewer window. Values can be adjusted under "
                                           "Edit values.")
            self.refresh_sandbox_bar()
            self.steps(self.body)

            plane, corners, err = self.layout_values()
            depth_item = self.depth_status()[2]
            self.phase_vars = {}
            rows = [
                (PH_DEPTH, "Per-pixel depth correction of the camera lens",
                 "camera %s\n%s" % (depth_item[1], datetime.datetime.fromtimestamp(depth_item[2]).strftime("%Y-%m-%d %H:%M"))
                 if depth_item else "-"),
                (PH_PLANE, "Flat sand surface as seen by the camera", short_plane(plane) if plane else "-"),
                (PH_CORNERS, "The four corners of the sand surface",
                 "\n".join("%-12s %s" % (n + ":", short_point(c)) for n, c in zip(CORNER_NAMES, corners))
                 if corners else "-"),
                (PH_PROJECTOR, "Aligns the projected image with the camera", "Resolution %dx%d" % self.resolution),
            ]
            for phase, desc, values in [r for r in rows if r[0] in ALL_PHASES]:
                status, kind = self.phase_status(phase)
                var = tk.BooleanVar(value=kind != "good")
                self.phase_vars[phase] = var
                cardf = self.card(self.body, padx=self.px(16), pady=self.px(6), gap=(0, 6))
                cardf.columnconfigure(3, weight=1)
                ttk.Checkbutton(cardf, variable=var, style="Card.TCheckbutton").grid(
                    row=0, column=0, sticky="n", padx=(self.px(0), self.px(6)), pady=(self.px(4), self.px(0)))
                self.badge(cardf, phase, kind, C["card"]).grid(row=0, column=1, sticky="n", padx=(self.px(0), self.px(14)))
                info = tk.Frame(cardf, bg=C["card"])
                info.grid(row=0, column=2, sticky="nw")
                self.label(info, "Phase %d  \u00b7  %s" % (phase_number(phase), PHASE_NAMES[phase]),
                           font=self.f_h2).pack(anchor="w")
                self.label(info, desc, fg=C["muted"]).pack(anchor="w", pady=(self.px(0), self.px(3)))
                self.pill(info, status, kind).pack(anchor="w")
                self.label(cardf, values, font=self.f_mono_small, fg=C["muted_text"]).grid(
                    row=0, column=3, sticky="nw", padx=(self.px(18), self.px(12)))
                ttk.Button(cardf, text="Run", style="Secondary.TButton",
                           command=lambda p=phase: self.start_phases([p])).grid(row=0, column=4, sticky="ne")
            if err:
                self.notice(self.body, err, "bad")

            ch_plane, ch_delta, ch_in_use, ch_err = self.color_height_state()
            cardf = self.card(self.body, padx=self.px(16), pady=self.px(6), gap=(0, 6))
            cardf.columnconfigure(3, weight=1)
            tk.Frame(cardf, bg=C["card"], width=self.px(28)).grid(row=0, column=0, padx=(self.px(0), self.px(6)))
            self.badge(cardf, 0, "accent", C["card"], text="\u2248").grid(row=0, column=1, sticky="n", padx=(self.px(0), self.px(14)))
            info = tk.Frame(cardf, bg=C["card"])
            info.grid(row=0, column=2, sticky="nw")
            self.label(info, "Color height  \u00b7  Sea level", font=self.f_h2).pack(anchor="w")
            self.label(info, "Where the water color starts, relative to the calibrated base plane",
                       fg=C["muted"]).pack(anchor="w", pady=(self.px(0), self.px(3)))
            if ch_in_use:
                self.pill(info, "Set to %s  \u00b7  saved in SARndbox.cfg" % self.format_delta(ch_delta), "good").pack(anchor="w")
            else:
                self.pill(info, "Not adjusted  \u00b7  colors follow the base plane", "muted").pack(anchor="w")
            self.label(cardf, self.format_delta(ch_delta), font=self.f_mono, fg=C["muted_text"]).grid(
                row=0, column=3, sticky="nw", padx=(self.px(24), self.px(12)))
            ttk.Button(cardf, text="Adjust", style="Secondary.TButton",
                       command=self.show_color_height).grid(row=0, column=4, sticky="ne")

            buttons = self.button_row()
            ttk.Button(buttons, text="Run ticked phases", style="Primary.TButton",
                       command=self.run_ticked).pack(side="left")
            ttk.Button(buttons, text="Run all %d phases" % len(ALL_PHASES), style="Secondary.TButton",
                       command=lambda: self.start_phases(list(ALL_PHASES))).pack(side="left", padx=(self.px(10), self.px(0)))
            ttk.Button(buttons, text="Edit values", style="Secondary.TButton",
                       command=self.show_editor).pack(side="left", padx=(self.px(10), self.px(0)))
            ttk.Button(buttons, text="Restore factory defaults", style="Danger.TButton",
                       command=self.restore_defaults).pack(side="left", padx=(self.px(10), self.px(0)))
            ttk.Button(buttons, text="Quit", style="Secondary.TButton", command=self.on_close).pack(side="right")
            tk.Frame(self.body, bg=C["bg"], height=self.px(4)).pack()
            self.make_log_widget(self.body, height=1)  # grows into whatever space is left

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
            self._sandbox_bar_key = key
            for w in bar.winfo_children():
                w.destroy()
            if pids:
                self.pill(bar, "\u25cf  Sandbox running  \u00b7  pid %d" % pids[0], "good").pack(side="left", padx=(self.px(0), self.px(10)))
                ttk.Button(bar, text="Stop sandbox", style="Small.Secondary.TButton",
                           command=self.stop_sandbox_clicked).pack(side="left")
            else:
                self.pill(bar, "\u25cb  Sandbox not running", "muted").pack(side="left", padx=(self.px(0), self.px(10)))
                ttk.Button(bar, text="Launch sandbox", style="Small.Secondary.TButton",
                           command=self.launch_sandbox_clicked).pack(side="left")

        def stop_sandbox_clicked(self):
            stop_sandbox(paths.sandbox_process, log)
            self.refresh_sandbox_bar(force=True)

        def launch_sandbox_clicked(self):
            if self.runner is not None and self.runner.running():
                messagebox.showinfo(APP_TITLE, "Finish or stop the running calibration tool first.")
                return
            launch_sandbox(paths, log)
            self.after(1500, self.refresh_sandbox_bar)

        def run_ticked(self):
            phases = [p for p in ALL_PHASES if self.phase_vars[p].get()]
            if not phases:
                messagebox.showinfo(APP_TITLE, "Tick at least one phase first.")
                return
            self.start_phases(phases)

        def restore_defaults(self):
            if not messagebox.askyesno(APP_TITLE, "Replace BoxLayout.txt and ProjectorMatrix.dat with the "
                                                  "factory default files and set the color height back to 0 cm?"
                                                  "\n\nThe current files are backed up first."):
                return
            for src, dst in ((paths.box_layout_orig, paths.box_layout),
                             (paths.projector_matrix_orig, paths.projector_matrix)):
                if os.path.isfile(src):
                    backup_file(dst, paths.backup_dir)
                    shutil.copy2(src, dst)
                    log.write("Restored %s from %s" % (dst, src))
                else:
                    log.write("No factory file %s; skipped" % src)
            self.reset_color_height()
            depth = state.get("depth")  # the camera's depth correction is not a factory file
            state.clear()
            if depth:
                state.data["depth"] = depth
                state.save()
            self.show_hub()

        def reset_color_height(self):
            """Put the sea level back on the base plane. The color height has no factory file: it is
            a heightMapPlane line in SARndbox.cfg, so the line is removed and the rest of the file
            (water speed, camera settings) is left alone."""
            if read_height_map_plane(paths.sandbox_cfg) is not None:
                backup = backup_file(paths.sandbox_cfg, paths.backup_dir)
                write_height_map_plane(paths.sandbox_cfg, None)
                log.write("Removed heightMapPlane from %s (backup: %s)" % (paths.sandbox_cfg, backup))
            plane, _, _ = self.layout_values()
            if plane is not None and self.send_color_height_live(plane, 0.0):
                log.write("Told the running sandbox to color heights from the base plane again")

        # ---------------- phase flow ----------------
        def start_phases(self, phases):
            self.queue = list(phases)
            self.next_session()

        def next_session(self):
            if not self.queue:
                self.show_hub()
                return
            if self.queue[0] == PH_DEPTH:
                self.queue.pop(0)
                session = DepthSession(bool(paths.vislet))
            elif self.queue[:2] == [PH_PLANE, PH_CORNERS]:
                self.queue = self.queue[2:]
                session = KinectSession((PH_PLANE, PH_CORNERS), bool(paths.vislet))
            elif self.queue[0] in (PH_PLANE, PH_CORNERS):
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
            buttons.pack(side="bottom", fill="x", pady=(self.px(14), self.px(0)))
            ttk.Button(buttons, text="Start " + session.tool_name, style="Primary.TButton",
                       command=self.start_tool).pack(side="left")
            if isinstance(session, ProjectorSession):
                ttk.Button(buttons, text="Show alignment grid", style="Secondary.TButton",
                           command=self.show_alignment_grid).pack(side="left", padx=(self.px(10), self.px(0)))
            if isinstance(session, DepthSession):
                ttk.Button(buttons, text="Skip this phase", style="Secondary.TButton",
                           command=self.next_session).pack(side="left", padx=(self.px(10), self.px(0)))
            ttk.Button(buttons, text="Back to overview", style="Secondary.TButton",
                       command=self.abort_to_hub).pack(side="right")

            if isinstance(session, DepthSession):
                bottom = tk.Frame(self.body, bg=C["bg"])
                bottom.pack(side="bottom", fill="x")
                if not kinect_etc_writable(paths.kinect_etc_dir):
                    self.permission_notice(bottom, lambda: self.show_intro(session))
                item = self.depth_status()[2]
                if item is not None:
                    self.existing_depth_notice(bottom, item, lambda: self.show_intro(session))

            if isinstance(session, ProjectorSession):
                bottom = tk.Frame(self.body, bg=C["bg"])
                bottom.pack(side="bottom", fill="x")
                res = self.card(bottom, padx=self.px(16), pady=self.px(10), gap=(0, 0))
                self.label(res, "Projector resolution", font=self.f_body_b).pack(side="left")
                self.label(res, "must match the projector's input signal", fg=C["muted"]).pack(side="left", padx=(self.px(8), self.px(14)))
                self.res_w = tk.StringVar(value=str(session.width))
                self.res_h = tk.StringVar(value=str(session.height))
                ttk.Entry(res, textvariable=self.res_w, width=6, font=self.f_mono, justify="center").pack(side="left")
                self.label(res, "\u00d7", fg=C["muted"]).pack(side="left", padx=self.px(6))
                ttk.Entry(res, textvariable=self.res_h, width=6, font=self.f_mono, justify="center").pack(side="left")
                self.pill(res, "detected %dx%d" % detect_resolution(), "info").pack(side="left", padx=(self.px(14), self.px(0)))

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
                           padx=(self.px(0), self.px(12)) if (c == 0 and span == 1) else 0, pady=(self.px(0), self.px(12)))
                inner = tk.Frame(outer, bg=C["card"], padx=self.px(18), pady=self.px(14))
                inner.pack(fill="both", expand=True)
                self.label(inner, heading, font=self.f_h2).pack(anchor="w", pady=(self.px(0), self.px(6)))
                body = self.label(inner, text, wraplength=self.px(400))
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
            if isinstance(session, DepthSession):
                if not kinect_etc_writable(paths.kinect_etc_dir):
                    if not messagebox.askokcancel(APP_TITLE, "%s is not writable, so RawKinectViewer will not be "
                                                             "able to save the correction at the end.\n\nStart anyway?"
                                                  % paths.kinect_etc_dir):
                        return
                try:
                    write_depth_tools_cfg(paths.depth_tools_cfg)
                except OSError as e:
                    messagebox.showerror(APP_TITLE, "Could not write the key bindings file %s:\n%s"
                                         % (paths.depth_tools_cfg, e))
                    return
                log.write("Wrote %s (Calibrate Depth Lens on keys 1 and 2)" % paths.depth_tools_cfg)
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
            buttons.pack(side="bottom", fill="x", pady=(self.px(14), self.px(0)))
            ttk.Button(buttons, text="Stop the tool now", style="Danger.TButton",
                       command=self.stop_tool_clicked).pack(side="left")
            ttk.Button(buttons, text="Send instructions again", style="Secondary.TButton",
                       command=lambda: self.runner and self.runner.show_message(session.reminder())).pack(
                side="left", padx=(self.px(10), self.px(0)))
            self.recapture_btn = None
            if isinstance(session, KinectSession):
                self.recapture_btn = ttk.Button(buttons, text="Capture the sand again", style="Secondary.TButton",
                                                command=self.recapture_average)
                self.recapture_btn.pack(side="left", padx=(self.px(10), self.px(0)))
                self.recapture_btn.state(["disabled"])
            inner = self.card(self.body, padx=self.px(18), pady=self.px(14))
            self.pulse_canvas = tk.Canvas(inner, width=self.px(22), height=self.px(22), bg=C["card"], highlightthickness=0)
            self.pulse_canvas.pack(side="left", padx=(self.px(0), self.px(12)))
            self._pulse_step = 0
            self._pulse()
            self.status_label = self.label(inner, session.status, font=self.f_h2, wraplength=self.px(840), anchor="w")
            self.status_label.pack(side="left", fill="x", expand=True)
            self.make_log_widget(self.body, height=12, title="Tool output")

        def _pulse(self):
            cv = self.pulse_canvas
            if cv is None or not cv.winfo_exists():
                return
            self._pulse_step = (self._pulse_step + 1) % 3
            r = self.px((6, 8, 10)[self._pulse_step])
            c, R = self.px(11), self.px(10)
            cv.delete("all")
            cv.create_oval(c - R, c - R, c + R, c + R, fill=C["info_bg"], outline="")
            cv.create_oval(c - r, c - r, c + r, c + r, fill=C["accent"], outline="")
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
                    self.tool_finished(rc)
            else:
                if self.xbg_proc is not None and self.xbg_proc.poll() is not None:
                    self.xbg_proc = None
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
            if isinstance(self.session, DepthSession):
                self.show_depth_result(rc)
            elif isinstance(self.session, KinectSession):
                self.show_kinect_result(rc)
            else:
                self.show_projector_result(rc)

        # ---------------- results ----------------
        def permission_notice(self, parent, after):
            """Red notice with a Fix permissions button (pkexec); 'after' redraws the screen."""
            bg, fg = TINTS["bad"]
            f = tk.Frame(parent, bg=bg)
            f.pack(fill="x", pady=(self.px(0), self.px(6)))
            ttk.Button(f, text="Fix permissions", style="Small.Primary.TButton",
                       command=lambda: self.fix_permissions_clicked(after)).pack(
                side="right", padx=self.px(12), pady=self.px(8))
            tk.Label(f, text="Problem:  %s belongs to root, so the correction cannot be saved. "
                     "Fix permissions asks for the password once and gives that folder to you."
                     % paths.kinect_etc_dir, bg=bg, fg=fg, font=self.f_body, justify="left",
                     wraplength=self.px(700), padx=self.px(14), pady=self.px(8)).pack(side="left", anchor="w")
            return f

        def existing_depth_notice(self, parent, item, after):
            """Phase 1 band: this camera already has a correction, with the button that throws it out."""
            path, serial, mtime = item
            when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            bg, fg = TINTS["info"]
            f = tk.Frame(parent, bg=bg)
            f.pack(fill="x", pady=(self.px(0), self.px(6)))
            ttk.Button(f, text="Delete it and start clean", style="Small.Secondary.TButton",
                       command=lambda: self.delete_depth_clicked(after)).pack(
                side="right", padx=self.px(12), pady=self.px(8))
            tk.Label(f, text="Camera %s already has a depth correction from %s. Capturing this phase again "
                     "replaces it, so deleting is only needed to go back to uncorrected readings."
                     % (serial, when), bg=bg, fg=fg, font=self.f_body, justify="left",
                     wraplength=self.px(680), padx=self.px(14), pady=self.px(8)).pack(side="left", anchor="w")
            return f

        def delete_depth_clicked(self, after):
            """Throw the DepthCorrection-<serial>.dat file out and start phase 1 from nothing."""
            item = self.depth_status()[2]
            if item is None:
                return
            path, serial, mtime = item
            when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            if not messagebox.askyesno(APP_TITLE,
                                       "Delete the depth correction of camera %s?\n\n"
                                       "%s, saved %s\n\n"
                                       "The camera goes back to uncorrected (slightly bowl-shaped) depth "
                                       "readings until this phase is run again, and the base plane, box "
                                       "corners and projector have to be redone afterwards.\n\n"
                                       "A copy is kept in the backups folder, and a running sandbox keeps "
                                       "the old correction until it is restarted."
                                       % (serial, os.path.basename(path), when)):
                return
            ok, info = remove_depth_file(item, paths.backup_dir, log)
            if not ok:
                extra = ("\n\nUse Fix permissions first, then try again."
                         if not kinect_etc_writable(paths.kinect_etc_dir) else "")
                messagebox.showerror(APP_TITLE, info + extra)
                return
            state.data.pop("depth", None)
            state.mark("depth_removed", serial=serial, path=path)
            log.write("Depth correction removed; the other %s need a redo" % phase_range(CORE_PHASES))
            messagebox.showinfo(APP_TITLE, "Deleted. A copy is in %s" % (info or paths.backup_dir))
            after()

        def fix_permissions_clicked(self, after):
            ok, message = fix_kinect_etc_permissions(paths.kinect_etc_dir, log)
            log.write("Fix permissions: " + message)
            if ok:
                messagebox.showinfo(APP_TITLE, message)
            else:
                messagebox.showerror(APP_TITLE, message)
            after()

        def show_depth_result(self, rc):
            s = self.session
            self.clear()
            self.header(s.title() + "  \u00b7  result", "Check the result, then continue or redo.")
            self.steps(self.body, current=s.phases)
            problems, warnings, notes = [], [], []
            if s.error:
                problems.append("RawKinectViewer stopped with an error: " + s.error)
                hint = s.explain_error(s.error)
                if hint:
                    problems.append(hint)
            if s.merge_missing:
                warnings.append("Vrui did not find the key bindings file %s, so keys 1 and 2 drove the plane "
                                "and corner tools instead of Calibrate Depth Lens." % s.merge_missing)
            item = depth_file_info(s.written) if s.written else None
            if item is None:
                item = depth_file_for(paths.kinect_etc_dir, s.serial)
            written = item is not None and item[2] >= s.started_at - 1
            if s.failure:
                problems.append("The tool reported: " + s.failure)
                if s.permission_problem():
                    problems.append("RawKinectViewer could not write into %s. Use Fix permissions below, then run "
                                    "this phase again." % paths.kinect_etc_dir)
            if not written and not problems:
                problems.append("No new depth correction file was written (%d capture%s taken). Press 2 in "
                                "RawKinectViewer after the captures next time."
                                % (s.captures, "" if s.captures == 1 else "s"))
            if written:
                path, serial, mtime = item
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                notes.append("%s written at %s (%d bytes)." % (
                    os.path.basename(path), datetime.datetime.fromtimestamp(mtime).strftime("%H:%M:%S"), size))
                if s.captures and s.captures < DEPTH_GOOD_CAPTURES:
                    warnings.append("Only %d capture%s: the correction rests on few distances. Consider redoing "
                                    "it with %d or more." % (s.captures, "" if s.captures == 1 else "s",
                                                             DEPTH_GOOD_CAPTURES))
                info = state.get("depth")
                if not info or abs(info.get("mtime", -1) - mtime) >= 1.0:
                    state.mark("depth", serial=serial or s.serial, captures=s.captures, mtime=mtime, path=path)
                    log.write("Depth correction %s written with %d captures; the other %s need a redo"
                              % (path, s.captures, phase_range(CORE_PHASES)))
                notes.append("The other %s are now marked for a redo: every depth reading has changed."
                             % phase_range(CORE_PHASES))
            c = self.card(self.body)
            self.label(c, "Depth lens correction", font=self.f_h2).pack(anchor="w")
            row = tk.Frame(c, bg=C["card"])
            row.pack(anchor="w", pady=(self.px(8), self.px(0)))
            self.pill(row, "%d capture%s" % (s.captures, "" if s.captures == 1 else "s"),
                      "good" if s.captures >= DEPTH_GOOD_CAPTURES else "warn").pack(side="left", padx=(self.px(0), self.px(8)))
            self.pill(row, "camera %s" % (s.serial or "?"), "info").pack(side="left", padx=(self.px(0), self.px(8)))
            self.pill(row, "file written" if written else "no file", "good" if written else "bad").pack(side="left")
            for n in notes:
                self.notice(self.body, n, "good", prefix="\u2713  ")
            for w in warnings:
                self.notice(self.body, w, "warn", prefix="Check:  ")
            for p in problems:
                self.notice(self.body, p, "bad", prefix="Problem:  ")
            if not kinect_etc_writable(paths.kinect_etc_dir):
                self.permission_notice(self.body, lambda: self.show_depth_result(rc))
            buttons = self.button_row()
            if written and not problems:
                ttk.Button(buttons, text="Continue", style="Primary.TButton", command=self.next_session).pack(side="left")
            ttk.Button(buttons, text="Redo this step", style="Secondary.TButton",
                       command=lambda: self.show_intro(DepthSession(bool(paths.vislet)))).pack(
                side="left", padx=(self.px(10), self.px(0)))
            ttk.Button(buttons, text="Back to overview", style="Secondary.TButton",
                       command=self.abort_to_hub).pack(side="right")
            tk.Frame(self.body, bg=C["bg"], height=self.px(14)).pack()
            self.make_log_widget(self.body, height=6, title="Tool output")

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
            new_plane = s.plane if (PH_PLANE in s.phases and s.plane) else current_plane
            new_corners = s.corners() if (PH_CORNERS in s.phases and len(s.corners()) == 4) else current_corners
            self.pending_plane, self.pending_corners = new_plane, new_corners

            warnings = []
            if PH_PLANE in s.phases and s.plane:
                if s.plane_flipped:
                    warnings.append("The camera reported the plane inverted; the signs were flipped "
                                    "so the offset is negative (as the sandbox expects).")
                warnings += plane_warnings(s.plane)
            if PH_CORNERS in s.phases and len(s.corners()) == 4:
                warnings += corner_warnings(s.corners(), new_plane)

            cards = tk.Frame(self.body, bg=C["bg"])
            cards.pack(fill="x")
            if PH_PLANE in s.phases:
                c1 = self.card(cards, side="left", fill="both", expand=True)
                self.label(c1, "Base plane", font=self.f_h2).pack(anchor="w")
                self.label(c1, format_plane(s.plane) if s.plane else "not captured", font=self.f_mono).pack(
                    anchor="w", pady=(self.px(6), self.px(4)))
                if s.plane and s.plane_rms is not None:
                    self.pill(c1, "fit RMS %.2f cm  \u00b7  lower is flatter" % s.plane_rms,
                              "good" if s.plane_rms < 1.0 else "warn").pack(anchor="w")
            if PH_CORNERS in s.phases:
                c2 = self.card(cards, side="left", fill="both", expand=True)
                self.label(c2, "Box corners", font=self.f_h2).pack(anchor="w")
                cs = s.corners()
                lines = ["%-12s %s" % (n + ":", format_point(c)) for n, c in zip(CORNER_NAMES, cs)] or ["none"]
                self.corner_label = self.label(c2, "\n".join(lines), font=self.f_mono)
                self.corner_label.pack(anchor="w", pady=(self.px(6), self.px(4)))
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
            if PH_CORNERS in s.phases and len(s.corners()) == 4 and any("order" in w for w in warnings):
                ttk.Button(buttons, text="Auto-order corners", style="Secondary.TButton",
                           command=self.auto_order_clicked).pack(side="left", padx=(self.px(10), self.px(0)))
            ttk.Button(buttons, text="Redo this step", style="Secondary.TButton",
                       command=lambda: self.show_intro(KinectSession(s.phases, bool(paths.vislet)))).pack(side="left", padx=(self.px(10), self.px(0)))
            ttk.Button(buttons, text="Back to overview", style="Secondary.TButton",
                       command=self.abort_to_hub).pack(side="right")
            tk.Frame(self.body, bg=C["bg"], height=self.px(14)).pack()
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
            if PH_PLANE in s.phases and s.plane:
                state.mark("plane", value=format_plane(plane), rms=s.plane_rms, serial=s.serial)
            if PH_CORNERS in s.phases and len(s.corners()) == 4:
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
            row.pack(anchor="w", pady=(self.px(8), self.px(0)))
            self.pill(row, "%d of %d tie points" % (s.tie_points, NUM_TIE_POINTS),
                      "good" if s.tie_points >= NUM_TIE_POINTS else "warn").pack(side="left", padx=(self.px(0), self.px(8)))
            if s.rms is not None:
                self.pill(row, "RMS %.2f px" % s.rms, "good" if s.rms < 2.0 else "warn").pack(side="left", padx=(self.px(0), self.px(8)))
            self.pill(row, "%dx%d" % (s.width, s.height), "info").pack(side="left")
            for n in notes:
                self.notice(self.body, n, "good", prefix="\u2713  ")
            for p in problems:
                self.notice(self.body, p, "bad", prefix="Problem:  ")
            buttons = self.button_row()
            if written and not problems:
                ttk.Button(buttons, text="Continue", style="Primary.TButton", command=self.next_session).pack(side="left")
            ttk.Button(buttons, text="Redo this step", style="Secondary.TButton",
                       command=lambda: self.show_intro(ProjectorSession(s.width, s.height, bool(paths.vislet)))).pack(side="left", padx=(self.px(10), self.px(0)))
            ttk.Button(buttons, text="Back to overview", style="Secondary.TButton",
                       command=self.abort_to_hub).pack(side="right")
            tk.Frame(self.body, bg=C["bg"], height=self.px(14)).pack()
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
                self.label(grid, text, font=self.f_small_b, fg=C["muted"]).grid(row=r, column=c, pady=(self.px(0), self.px(2)))

            def row(r, label, values, keys):
                self.label(grid, label, font=self.f_body_b).grid(row=r, column=0, sticky="w", padx=(self.px(0), self.px(16)), pady=self.px(5))
                for c, (v, k) in enumerate(zip(values, keys)):
                    var = tk.StringVar(value="%.6g" % v)
                    self.edit_vars[k] = var
                    ttk.Entry(grid, textvariable=var, width=12, font=self.f_mono).grid(row=r, column=1 + c, padx=self.px(4), pady=self.px(5))

            head(0, 1, "normal x"); head(0, 2, "normal y"); head(0, 3, "normal z"); head(0, 4, "offset (cm)")
            row(1, "Base plane", plane, ("nx", "ny", "nz", "off"))
            tk.Frame(grid, bg=C["card"], height=self.px(10)).grid(row=2, column=0)
            head(3, 1, "x (cm)"); head(3, 2, "y (cm)"); head(3, 3, "z (cm)")
            for i, name in enumerate(CORNER_NAMES):
                row(4 + i, "Corner " + name, corners[i], ("c%d%s" % (i, a) for a in "xyz"))

            self.editor_msg_frame = tk.Frame(self.body, bg=C["bg"])
            self.editor_msg_frame.pack(fill="x")
            self.editor_msg = tk.Label(self.editor_msg_frame, text="", bg=C["bg"], fg=C["text"], font=self.f_body,
                                       wraplength=self.px(880), justify="left", padx=self.px(14), pady=self.px(10))
            buttons = self.button_row()
            ttk.Button(buttons, text="Save", style="Primary.TButton", command=self.save_editor).pack(side="left")
            ttk.Button(buttons, text="Auto-order corners", style="Secondary.TButton",
                       command=self.editor_auto_order).pack(side="left", padx=(self.px(10), self.px(0)))
            ttk.Button(buttons, text="Restart sandbox with these values", style="Secondary.TButton",
                       command=self.editor_restart_sandbox).pack(side="left", padx=(self.px(10), self.px(0)))
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
            launch_sandbox(paths, log)
            self.set_editor_msg(self.editor_msg.cget("text") + "\nSandbox restarted.", "good")

        # ---------------- close ----------------
        def on_close(self):
            self.ch_flush()
            if self.runner is not None and self.runner.running():
                if not messagebox.askyesno(APP_TITLE, "%s is still running. Stop it and quit?" % self.session.tool_name):
                    return
                self.runner.stop()
            self.destroy()

    return App


def run_gui(paths, scale=None):
    app = build_app(paths, scale=scale)()
    app.mainloop()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="AR Sandbox calibration wizard")
    parser.add_argument("--check", action="store_true", help="print the paths the wizard will use and exit")
    parser.add_argument("--scale", type=float, default=None,
                        help="size of the wizard panel as a fraction of the screen, 0.4 to 1.0 "
                             "(default %.2f, or SANDBOX_CALIB_SCALE)" % DEFAULT_SCALE)
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
    run_gui(paths, args.scale)
    return 0


if __name__ == "__main__":
    sys.exit(main())
