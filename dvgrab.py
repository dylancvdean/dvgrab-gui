#!/usr/bin/env python3
# dvgrab_gui.py
# Portable Tk/Ttk GUI for dvgrab on Linux.
# - Exposes common settings (output dir, format, naming, splitting, etc.)
# - "Advanced" panel for the rest (card/channel/guid, v4l2, duration, etc.)
# - Remembers settings across launches in ~/.config/dvgrab-gui/config.json
# - For each capture, creates a new subfolder under the chosen output directory:
#     e.g., ~/vids/tape1, ~/vids/tape2, ...
# - Runs dvgrab in a background thread; live logs shown in the UI.
# - Uses only the Python standard library (tkinter/ttk), no extra deps.

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import queue
from pathlib import Path
from datetime import datetime
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception as e:
    print("tkinter is required.", file=sys.stderr)
    raise

APP_NAME = "dvgrab-gui"
CONFIG_DIR = Path.home() / ".config" / "dvgrab-gui"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_OUTPUT = str(Path.home() / "Videos")
LOG_LINES_MAX = 5000

# --------------------------- Utilities & Config ---------------------------
def system_pick_directory(initial_dir=None):
    """
    Try native folder pickers first:
      - KDE: kdialog --getexistingdirectory
      - GNOME/etc.: zenity --file-selection --directory
    Falls back to Tk's filedialog if none are available (return None and let caller handle).
    """
    initial_dir = str(Path(initial_dir or Path.home()).expanduser())
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP", "") + " " +
               os.environ.get("DESKTOP_SESSION", "")).upper()
    prefer_kde = "KDE" in desktop or "PLASMA" in desktop or os.environ.get("KDE_FULL_SESSION") == "true"

    cmds = []
    if prefer_kde and shutil.which("kdialog"):
        cmds.append(["kdialog", "--getexistingdirectory", initial_dir])
    # zenity works well across GNOME, Cinnamon, MATE, XFCE (if installed)
    if shutil.which("zenity"):
        idir = initial_dir if initial_dir.endswith(os.sep) else initial_dir + os.sep
        cmds.append(["zenity", "--file-selection", "--directory", "--filename", idir])
    # Try both even if desktop guess was wrong
    if not prefer_kde and shutil.which("kdialog"):
        cmds.append(["kdialog", "--getexistingdirectory", initial_dir])

    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc.returncode == 0:
                choice = proc.stdout.strip()
                if choice:
                    return choice
        except Exception:
            pass  # try next option

    return None  # let caller fall back to Tk

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "output_dir": DEFAULT_OUTPUT,
        "subfolder_prefix": "tape",
        "filename_scheme": "timestamp",  # none|timestamp|timecode|timesys
        "format": "dv2",                 # dv2|dv1|raw|qt|mov|avi|mpeg2
        "showstatus": True,
        "autosplit": False,
        "autosplit_seconds": 0,
        "size_mb": 0,                    # 0 = unlimited
        "csize_mb": 0,
        "cmincutsize_mb": 0,
        "rewind": False,
        "noavc": False,
        "recordonly": False,
        "opendml": True,                 # for dv2 > 1GB
        "frames_per_file": 0,
        "every_nth": 1,
        "card": "",
        "channel": "",
        "guid": "",
        "duration": "",                  # SMIL time like "1h", "00:30:00", etc.
        "use_v4l2": False,
        "v4l2_input": "/dev/video0",
        "dvgrab_path": shutil.which("dvgrab") or "dvgrab",
        # Track next index per base output dir to make tapeN folders. We'll
        # still auto-scan folders on each start to avoid collisions.
        "next_index_by_dir": {}
    }

def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    os.replace(tmp, CONFIG_FILE)

def find_next_subfolder(base_dir: Path, prefix: str, cfg) -> Path:
    # Prefer persisted counter, but verify against actual folders to avoid collisions.
    base_dir = base_dir.expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    next_map = cfg.get("next_index_by_dir", {})
    key = str(base_dir)
    start_idx = int(next_map.get(key, 1))

    # Scan existing folders with pattern prefix + digits
    max_found = 0
    pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    try:
        for p in base_dir.iterdir():
            if p.is_dir():
                m = pat.match(p.name)
                if m:
                    n = int(m.group(1))
                    if n > max_found:
                        max_found = n
    except Exception:
        pass

    idx = max(start_idx, max_found + 1)
    # finalize
    candidate = base_dir / f"{prefix}{idx}"
    # Store next for *future* captures
    next_map[key] = idx + 1
    cfg["next_index_by_dir"] = next_map
    return candidate

def which_dvgrab(path_pref: str) -> str:
    # If user typed an absolute/relative path, use it; else search PATH.
    if path_pref and (Path(path_pref).exists() or "/" in path_pref):
        return path_pref
    found = shutil.which(path_pref or "dvgrab")
    return found or path_pref or "dvgrab"

# --------------------------- Command Builder ---------------------------

def build_dvgrab_cmd(values: dict, capture_dir: Path) -> list:
    """
    Compose dvgrab command from GUI state.
    We point dvgrab to write files inside capture_dir with base 'clip-'.
    Let dvgrab decide extensions based on -format (or extension-less base).
    """
    dvgrab_bin = which_dvgrab(values["dvgrab_path"])
    cmd = [dvgrab_bin]

    # Format mapping
    fmt = values["format"]  # dv2|dv1|raw|qt|mov|avi|mpeg2
    if fmt:
        # dvgrab man page accepts -format dv1|dv2|avi|raw|dif|qt|mov|jpeg|jpg|mpeg2|hdv
        # We keep a conservative subset here.
        if fmt == "mpeg2":
            cmd += ["-format", "mpeg2"]
        elif fmt in ("dv2", "dv1", "raw", "qt", "mov", "avi"):
            cmd += ["-format", fmt]

    # Filename scheme
    scheme = values["filename_scheme"]
    if scheme == "timestamp":
        cmd.append("-timestamp")
    elif scheme == "timecode":
        cmd.append("-timecode")
    elif scheme == "timesys":
        cmd.append("-timesys")

    # Splitting
    size_mb = int(values.get("size_mb") or 0)
    if size_mb >= 0:
        cmd += ["-size", str(size_mb)]
    frames_per_file = int(values.get("frames_per_file") or 0)
    if frames_per_file > 0:
        cmd += ["-frames", str(frames_per_file)]
    if values.get("autosplit"):
        secs = int(values.get("autosplit_seconds") or 0)
        # -autosplit or -autosplit=SECONDS
        if secs > 0:
            cmd.append(f"-autosplit={secs}")
        else:
            cmd.append("-autosplit")
    csize_mb = int(values.get("csize_mb") or 0)
    if csize_mb > 0:
        cmd += ["-csize", str(csize_mb)]
    cmincutsize_mb = int(values.get("cmincutsize_mb") or 0)
    if cmincutsize_mb > 0:
        cmd += ["-cmincutsize", str(cmincutsize_mb)]

    # Controls & behaviors
    if values.get("showstatus", True):
        cmd.append("-showstatus")
    if values.get("rewind", False):
        cmd.append("-rewind")
    if values.get("noavc", False):
        cmd.append("-noavc")
    if values.get("recordonly", False):
        cmd.append("-recordonly")
    if values.get("opendml", False) and fmt == "dv2":
        cmd.append("-opendml")

    # Device routing
    card = str(values.get("card") or "").strip()
    if card.isdigit():
        cmd += ["-card", card]
    channel = str(values.get("channel") or "").strip()
    if channel.isdigit():
        cmd += ["-channel", channel]
    guid = str(values.get("guid") or "").strip()
    if guid:
        cmd += ["-guid", guid]

    # V4L2 (USB DV) path
    if values.get("use_v4l2", False):
        cmd.append("-v4l2")
        v4l2_input = str(values.get("v4l2_input") or "").strip()
        if v4l2_input:
            cmd += ["-input", v4l2_input]

    # Time limiting across splits
    dur = str(values.get("duration") or "").strip()
    if dur:
        cmd += ["-duration", dur]

    # Decimation
    every = int(values.get("every_nth") or 1)
    if every and every > 1:
        cmd += ["-every", str(every)]

    # Base name inside capture_dir; trailing dash gives "clip-001.ext" etc.
    base = (capture_dir / "clip-").as_posix()
    cmd.append(base)
    return cmd

# --------------------------- GUI ---------------------------

class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.master.title("dvgrab GUI")
        self.master.minsize(820, 620)
        self.pack(fill="both", expand=True)

        self.cfg = load_config()
        self.proc = None
        self.proc_thread = None
        self.log_q = queue.Queue()
        self.stop_requested = False

        self._build_widgets()
        self._load_from_config()
        self._schedule_log_pump()

        # Warn if dvgrab not found
        dv_path = which_dvgrab(self.cfg.get("dvgrab_path", "dvgrab"))
        if not shutil.which(dv_path) and not Path(dv_path).exists():
            messagebox.showwarning(
                "dvgrab not found",
                "Could not find 'dvgrab' on PATH.\n"
                "Install it (e.g., sudo apt install dvgrab) or set the dvgrab path in Advanced."
            )

        self.master.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_widgets(self):
        # Top-level layout: notebook + bottom button bar + log
        self.nb = ttk.Notebook(self)
        self.nb.pack(side="top", fill="x", padx=8, pady=(8, 0))

        self.basic = ttk.Frame(self.nb)
        self.adv = ttk.Frame(self.nb)
        self.nb.add(self.basic, text="Capture")
        self.nb.add(self.adv, text="Advanced")

        # ------------------ Basic Tab ------------------
        row = 0
        self.output_dir_var = tk.StringVar()
        ttk.Label(self.basic, text="Output directory (base for all tapes):").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        out_frame = ttk.Frame(self.basic)
        out_frame.grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        self.basic.grid_columnconfigure(1, weight=1)
        ttk.Entry(out_frame, textvariable=self.output_dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(out_frame, text="Browse…", command=self.choose_output_dir).pack(side="left", padx=(6,0))
        row += 1

        self.subprefix_var = tk.StringVar()
        ttk.Label(self.basic, text="Subfolder prefix (auto-numbered):").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(self.basic, textvariable=self.subprefix_var, width=16).grid(row=row, column=1, sticky="w", padx=8, pady=4)
        row += 1

        # Format and naming
        self.format_var = tk.StringVar()
        ttk.Label(self.basic, text="Format:").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        fmt_combo = ttk.Combobox(self.basic, textvariable=self.format_var, state="readonly",
                                 values=[
                                     "dv2 (AVI Type 2)", "dv1 (AVI Type 1)", "raw (.dv)", "qt (QuickTime)", "mov (QuickTime)", "avi", "mpeg2 (HDV .m2t)"
                                 ])
        fmt_combo.grid(row=row, column=1, sticky="w", padx=8, pady=4)
        row += 1

        self.scheme_var = tk.StringVar()
        self.scheme_var.set("timestamp")
        scheme_frame = ttk.Frame(self.basic)
        scheme_frame.grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        ttk.Label(scheme_frame, text="Filename scheme:").pack(side="left")
        for label, key in [("Timestamp", "timestamp"), ("Timecode", "timecode"), ("System time", "timesys"), ("None", "none")]:
            ttk.Radiobutton(scheme_frame, text=label, value=key, variable=self.scheme_var).pack(side="left", padx=(10,0))
        row += 1

        # Splitting
        self.size_mb_var = tk.StringVar()
        self.autosplit_var = tk.BooleanVar()
        self.autosplit_secs_var = tk.StringVar()
        split_fr = ttk.LabelFrame(self.basic, text="Splitting")
        split_fr.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        split_fr.columnconfigure(1, weight=1)
        ttk.Label(split_fr, text="Max file size (MB, 0=unlimited):").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(split_fr, textvariable=self.size_mb_var, width=10).grid(row=0, column=1, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(split_fr, text="Autosplit on scene/time gaps", variable=self.autosplit_var).grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Label(split_fr, text="Sensitivity (seconds, 0=default):").grid(row=1, column=1, sticky="w", padx=8, pady=4)
        ttk.Entry(split_fr, textvariable=self.autosplit_secs_var, width=10).grid(row=1, column=2, sticky="w", padx=8, pady=4)
        row += 1

        # Behavior toggles
        self.showstatus_var = tk.BooleanVar()
        self.rewind_var = tk.BooleanVar()
        self.noavc_var = tk.BooleanVar()
        self.recordonly_var = tk.BooleanVar()
        self.opendml_var = tk.BooleanVar()
        toggles = ttk.Frame(self.basic)
        toggles.grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(toggles, text="Show status", variable=self.showstatus_var).pack(side="left", padx=(0,12))
        ttk.Checkbutton(toggles, text="Rewind before capture", variable=self.rewind_var).pack(side="left", padx=(0,12))
        ttk.Checkbutton(toggles, text="Disable AV/C (noavc)", variable=self.noavc_var).pack(side="left", padx=(0,12))
        ttk.Checkbutton(toggles, text="Record-only (requires AV/C)", variable=self.recordonly_var).pack(side="left", padx=(0,12))
        ttk.Checkbutton(toggles, text="OpenDML (Type 2 >1GB)", variable=self.opendml_var).pack(side="left", padx=(0,12))
        row += 1

        # Actions
        btns = ttk.Frame(self.basic)
        btns.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        btns.columnconfigure(3, weight=1)
        self.start_btn = ttk.Button(btns, text="Start Capture", command=self.start_capture)
        self.start_btn.grid(row=0, column=0, padx=4)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop_capture, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=4)
        ttk.Button(btns, text="Open Last Folder", command=self.open_last_folder).grid(row=0, column=2, padx=4)
        ttk.Button(btns, text="Copy Command", command=self.copy_command).grid(row=0, column=4, padx=4)
        ttk.Button(btns, text="Save Settings", command=self.save_settings).grid(row=0, column=5, padx=4)
        self.status_var = tk.StringVar(value="Idle.")
        ttk.Label(btns, textvariable=self.status_var).grid(row=0, column=3, sticky="w")

        # ------------------ Advanced Tab ------------------
        r = 0
        self.dvgrab_path_var = tk.StringVar()
        ttk.Label(self.adv, text="dvgrab path (leave as 'dvgrab' if on PATH):").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(self.adv, textvariable=self.dvgrab_path_var).grid(row=r, column=1, sticky="ew", padx=8, pady=4)
        self.adv.grid_columnconfigure(1, weight=1)
        r += 1

        self.frames_per_file_var = tk.StringVar()
        self.every_nth_var = tk.StringVar()
        grid1 = ttk.Frame(self.adv); grid1.grid(row=r, column=0, columnspan=2, sticky="ew", padx=8, pady=2)
        ttk.Label(grid1, text="Frames per file (0=off):").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(grid1, textvariable=self.frames_per_file_var, width=10).grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(grid1, text="Record every Nth frame (1=all):").grid(row=0, column=2, sticky="w", padx=4, pady=4)
        ttk.Entry(grid1, textvariable=self.every_nth_var, width=10).grid(row=0, column=3, sticky="w", padx=4, pady=4)
        r += 1

        self.csize_mb_var = tk.StringVar()
        self.cmincutsize_mb_var = tk.StringVar()
        grid2 = ttk.Frame(self.adv); grid2.grid(row=r, column=0, columnspan=2, sticky="ew", padx=8, pady=2)
        ttk.Label(grid2, text="Collection size MB (0=off):").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(grid2, textvariable=self.csize_mb_var, width=10).grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(grid2, text="Cut ahead MB for collection:").grid(row=0, column=2, sticky="w", padx=4, pady=4)
        ttk.Entry(grid2, textvariable=self.cmincutsize_mb_var, width=10).grid(row=0, column=3, sticky="w", padx=4, pady=4)
        r += 1

        self.card_var = tk.StringVar()
        self.channel_var = tk.StringVar()
        self.guid_var = tk.StringVar()
        grid3 = ttk.Frame(self.adv); grid3.grid(row=r, column=0, columnspan=2, sticky="ew", padx=8, pady=2)
        ttk.Label(grid3, text="FireWire card #").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(grid3, textvariable=self.card_var, width=8).grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(grid3, text="Channel").grid(row=0, column=2, sticky="w", padx=4, pady=4)
        ttk.Entry(grid3, textvariable=self.channel_var, width=8).grid(row=0, column=3, sticky="w", padx=4, pady=4)
        ttk.Label(grid3, text="GUID (hex or 1)").grid(row=0, column=4, sticky="w", padx=4, pady=4)
        ttk.Entry(grid3, textvariable=self.guid_var, width=16).grid(row=0, column=5, sticky="w", padx=4, pady=4)
        r += 1

        self.duration_var = tk.StringVar()
        ttk.Label(self.adv, text="Max capture duration (SMIL time, e.g., 1h, 30min, 00:30:00):").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(self.adv, textvariable=self.duration_var).grid(row=r, column=1, sticky="ew", padx=8, pady=4)
        r += 1

        self.use_v4l2_var = tk.BooleanVar()
        self.v4l2_input_var = tk.StringVar()
        v4 = ttk.Frame(self.adv); v4.grid(row=r, column=0, columnspan=2, sticky="ew", padx=8, pady=2)
        ttk.Checkbutton(v4, text="Use V4L2 (USB DV)", variable=self.use_v4l2_var).grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Label(v4, text="V4L2 device (input):").grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Entry(v4, textvariable=self.v4l2_input_var, width=20).grid(row=0, column=2, sticky="w", padx=4, pady=4)

        # ------------------ Log Area ------------------
        sep = ttk.Separator(self, orient="horizontal")
        sep.pack(side="top", fill="x", padx=8, pady=8)
        log_frame = ttk.LabelFrame(self, text="dvgrab output")
        log_frame.pack(side="top", fill="both", expand=True, padx=8, pady=(0,8))
        self.log = tk.Text(log_frame, height=16, wrap="none")
        self.log.pack(side="left", fill="both", expand=True)
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        yscroll.pack(side="right", fill="y")
        self.log.config(yscrollcommand=yscroll.set)
        self.log.tag_configure("err", foreground="#b00")
        self.last_folder = None

    def _load_from_config(self):
        c = self.cfg
        self.output_dir_var.set(c.get("output_dir", DEFAULT_OUTPUT))
        self.subprefix_var.set(c.get("subfolder_prefix", "tape"))
        self.scheme_var.set(c.get("filename_scheme", "timestamp"))
        # format mapping to user label
        fmt = c.get("format", "dv2")
        label = {
            "dv2": "dv2 (AVI Type 2)",
            "dv1": "dv1 (AVI Type 1)",
            "raw": "raw (.dv)",
            "qt": "qt (QuickTime)",
            "mov": "mov (QuickTime)",
            "avi": "avi",
            "mpeg2": "mpeg2 (HDV .m2t)",
        }.get(fmt, "dv2 (AVI Type 2)")
        self.format_var.set(label)

        self.showstatus_var.set(bool(c.get("showstatus", True)))
        self.autosplit_var.set(bool(c.get("autosplit", False)))
        self.autosplit_secs_var.set(str(c.get("autosplit_seconds", 0)))
        self.size_mb_var.set(str(c.get("size_mb", 0)))
        self.csize_mb_var.set(str(c.get("csize_mb", 0)))
        self.cmincutsize_mb_var.set(str(c.get("cmincutsize_mb", 0)))
        self.rewind_var.set(bool(c.get("rewind", False)))
        self.noavc_var.set(bool(c.get("noavc", False)))
        self.recordonly_var.set(bool(c.get("recordonly", False)))
        self.opendml_var.set(bool(c.get("opendml", True)))
        self.frames_per_file_var.set(str(c.get("frames_per_file", 0)))
        self.every_nth_var.set(str(c.get("every_nth", 1)))
        self.card_var.set(str(c.get("card", "")))
        self.channel_var.set(str(c.get("channel", "")))
        self.guid_var.set(str(c.get("guid", "")))
        self.duration_var.set(str(c.get("duration", "")))
        self.use_v4l2_var.set(bool(c.get("use_v4l2", False)))
        self.v4l2_input_var.set(str(c.get("v4l2_input", "/dev/video0")))
        self.dvgrab_path_var.set(str(c.get("dvgrab_path", shutil.which("dvgrab") or "dvgrab")))

    def _gather_values(self):
        # Map the combobox label back to dvgrab -format token
        fmt_label = self.format_var.get()
        fmt_map = {
            "dv2 (AVI Type 2)": "dv2",
            "dv1 (AVI Type 1)": "dv1",
            "raw (.dv)": "raw",
            "qt (QuickTime)": "qt",
            "mov (QuickTime)": "mov",
            "avi": "avi",
            "mpeg2 (HDV .m2t)": "mpeg2",
        }
        fmt = fmt_map.get(fmt_label, "dv2")

        vals = {
            "output_dir": self.output_dir_var.get().strip(),
            "subfolder_prefix": self.subprefix_var.get().strip() or "tape",
            "filename_scheme": self.scheme_var.get(),
            "format": fmt,
            "showstatus": bool(self.showstatus_var.get()),
            "autosplit": bool(self.autosplit_var.get()),
            "autosplit_seconds": _as_int(self.autosplit_secs_var.get(), 0),
            "size_mb": _as_int(self.size_mb_var.get(), 0),
            "csize_mb": _as_int(self.csize_mb_var.get(), 0),
            "cmincutsize_mb": _as_int(self.cmincutsize_mb_var.get(), 0),
            "rewind": bool(self.rewind_var.get()),
            "noavc": bool(self.noavc_var.get()),
            "recordonly": bool(self.recordonly_var.get()),
            "opendml": bool(self.opendml_var.get()),
            "frames_per_file": _as_int(self.frames_per_file_var.get(), 0),
            "every_nth": _as_int(self.every_nth_var.get(), 1),
            "card": self.card_var.get().strip(),
            "channel": self.channel_var.get().strip(),
            "guid": self.guid_var.get().strip(),
            "duration": self.duration_var.get().strip(),
            "use_v4l2": bool(self.use_v4l2_var.get()),
            "v4l2_input": self.v4l2_input_var.get().strip(),
            "dvgrab_path": self.dvgrab_path_var.get().strip() or "dvgrab",
            "next_index_by_dir": self.cfg.get("next_index_by_dir", {}),
        }
        return vals

    def save_settings(self):
        vals = self._gather_values()
        self.cfg.update(vals)
        save_config(self.cfg)
        self._log("Settings saved.", is_err=False)

    def choose_output_dir(self):
        # Try native picker first
        picked = system_pick_directory(self.output_dir_var.get() or DEFAULT_OUTPUT)
        if not picked:
            # Fallback to Tk dialog
            picked = filedialog.askdirectory(initialdir=self.output_dir_var.get() or DEFAULT_OUTPUT)
        if picked:
            self.output_dir_var.set(picked)
            # optionally persist immediately so next launch remembers it
            self.cfg["output_dir"] = picked
            save_config(self.cfg)


    def copy_command(self):
        try:
            vals = self._gather_values()
            base_dir = Path(vals["output_dir"]).expanduser()
            subdir = find_next_subfolder(base_dir, vals["subfolder_prefix"], self.cfg)
            # Do not increment persistently on a dry-run; rebuild without storing
            cmd = build_dvgrab_cmd(vals, capture_dir=subdir)
            cmd_str = " ".join(shlex.quote(x) for x in cmd)
            self.master.clipboard_clear()
            self.master.clipboard_append(cmd_str)
            self._log("Copied command:\n" + cmd_str, is_err=False)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to build command: {e}")

    def open_last_folder(self):
        if not self.last_folder or not Path(self.last_folder).exists():
            messagebox.showinfo("Open Folder", "No recent capture folder yet.")
            return
        try:
            # xdg-open fallback
            subprocess.Popen(["xdg-open", str(self.last_folder)])
        except Exception as e:
            messagebox.showerror("Open Folder", f"Failed to open folder: {e}")

    # ------------------ Capture lifecycle ------------------

    def start_capture(self):
        if self.proc is not None:
            messagebox.showinfo("Capture", "Capture already running.")
            return
        vals = self._gather_values()

        dv_path = which_dvgrab(vals["dvgrab_path"])
        if not shutil.which(dv_path) and not Path(dv_path).exists():
            messagebox.showerror("dvgrab not found",
                                 f"dvgrab not found at '{dv_path}'. Install it or update Advanced → dvgrab path.")
            return

        base_dir = Path(vals["output_dir"]).expanduser()
        if not base_dir:
            messagebox.showerror("Output", "Choose an output directory.")
            return
        # Allocate subfolder and persist next index
        subdir = find_next_subfolder(base_dir, vals["subfolder_prefix"], self.cfg)
        subdir.mkdir(parents=True, exist_ok=True)
        self.last_folder = subdir
        save_config(self.cfg)  # persist the updated next_index_by_dir now

        cmd = build_dvgrab_cmd(vals, capture_dir=subdir)

        # Start subprocess in its own process group for clean SIGINT delivery
        self._log("Starting capture in: " + str(subdir), is_err=False)
        self._log("Command: " + " ".join(shlex.quote(x) for x in cmd), is_err=False)
        try:
            self.stop_requested = False
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                preexec_fn=os.setsid  # new process group
            )
        except Exception as e:
            self.proc = None
            messagebox.showerror("Start failed", f"Could not start dvgrab:\n{e}")
            return

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("Capturing…")
        self.proc_thread = threading.Thread(target=self._pump_process, daemon=True)
        self.proc_thread.start()

    def stop_capture(self):
        if self.proc is None:
            return
        self.stop_requested = True
        try:
            pgid = os.getpgid(self.proc.pid)
            os.killpg(pgid, signal.SIGINT)  # polite stop like Ctrl-C
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self._log("Stop signal sent.", is_err=False)

    def _pump_process(self):
        # Read stdout/stderr lines and put to queue.
        try:
            assert self.proc is not None
            for stream, tag in [(self.proc.stdout, None), (self.proc.stderr, "err")]:
                t = threading.Thread(target=self._read_stream, args=(stream, tag), daemon=True)
                t.start()

            ret = self.proc.wait()
            self.log_q.put(("[exit]", None))
            self.log_q.put((f"dvgrab exited with code {ret}", "err" if ret else None))
        finally:
            self.proc = None
            self.master.after(0, self._on_capture_finished)

    def _read_stream(self, stream, tag):
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            # dvgrab --showstatus prints carriage-return updates; normalize
            if "\r" in line:
                # show latest status line on its own
                parts = line.split("\r")
                for p in parts:
                    if p.strip():
                        self.log_q.put((p.rstrip(), tag))
            else:
                self.log_q.put((line.rstrip(), tag))
        try:
            stream.close()
        except Exception:
            pass

    def _on_capture_finished(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set("Idle.")

    def _schedule_log_pump(self):
        try:
            while True:
                line, tag = self.log_q.get_nowait()
                if line == "[exit]":
                    continue
                self._log(line, is_err=(tag == "err"))
        except queue.Empty:
            pass
        self.master.after(60, self._schedule_log_pump)

    def _log(self, text, is_err=False):
        ts = datetime.now().strftime("%H:%M:%S")
        tag = "err" if is_err else None
        self.log.insert("end", f"[{ts}] {text}\n", tag)
        # Trim
        if int(self.log.index('end-1c').split('.')[0]) > LOG_LINES_MAX:
            self.log.delete("1.0", "2.0")
        self.log.see("end")

    def on_close(self):
        try:
            self.save_settings()
        except Exception:
            pass
        if self.proc is not None:
            if not messagebox.askyesno("Quit", "Capture is running. Stop and quit?"):
                return
            self.stop_capture()
            # Best-effort wait a moment for process to exit
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
        self.master.destroy()

# --------------------------- Helpers ---------------------------

def _as_int(s, default):
    try:
        v = int(str(s).strip())
        return v
    except Exception:
        return default

# --------------------------- Main ---------------------------

def main():
    root = tk.Tk()
    # Slightly nicer defaults
    try:
        root.style = ttk.Style()
        if sys.platform.startswith("linux") and "clam" in root.style.theme_names():
            root.style.theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
