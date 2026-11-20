"""Where this copy of Clipper Studio keeps its projects and looks for videos.

Every machine has different folders, so nothing is hard-coded. The settings live in settings.json
next to this file (never committed). If the file is missing, sensible defaults for Windows, macOS
and Linux are used and the page asks once, on first start, whether they are right.

  work_dir    projects folder: one sub-folder per imported video (cuts, transcript, exports)
  video_dirs  folders listed under "Recent videos" (Downloads, Videos/Movies, Desktop by default)
  port        the local web page port (the next free one is used if it is busy)
  llm_script  optional: your own script that picks moments (see moments_llm.py); empty = not used
"""
import json, os, shutil, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(ROOT, "settings.json")
HOME = os.path.expanduser("~")
DEFAULT_PORT = 8804


def _user_dirs():
    """Downloads / Videos / Desktop as THIS operating system names them (OneDrive and XDG included)."""
    names = ["Downloads", "Videos", "Movies", "Desktop"]
    out = [os.path.join(HOME, n) for n in names]
    if sys.platform.startswith("win"):
        od = os.environ.get("OneDrive") or os.path.join(HOME, "OneDrive")
        out += [os.path.join(od, n) for n in ("Desktop", "Videos", "Downloads")]
    else:
        # Linux desktops can rename these (other languages, custom places): ask xdg-user-dir
        for key in ("DOWNLOAD", "VIDEOS", "DESKTOP"):
            if shutil.which("xdg-user-dir"):
                try:
                    import subprocess
                    p = subprocess.run(["xdg-user-dir", key], capture_output=True, text=True, timeout=3).stdout.strip()
                    if p and p != HOME: out.append(p)
                except Exception:
                    pass
    seen, res = set(), []
    for p in out:
        rp = os.path.realpath(p)
        if os.path.isdir(rp) and rp not in seen:
            seen.add(rp); res.append(rp)
    return res


def defaults():
    return {"work_dir": os.path.join(ROOT, "work"), "video_dirs": _user_dirs(), "port": DEFAULT_PORT, "llm_script": ""}


def first_run():
    return not os.path.exists(SETTINGS)


def load():
    s = defaults()
    try:
        saved = json.load(open(SETTINGS, encoding="utf-8"))
        if isinstance(saved, dict):
            for k in s:
                if k in saved and saved[k] not in (None, ""): s[k] = saved[k]
    except (OSError, ValueError):
        pass
    s["work_dir"] = os.path.abspath(os.path.expanduser(str(s["work_dir"])))
    s["video_dirs"] = [os.path.abspath(os.path.expanduser(str(p))) for p in (s["video_dirs"] or [])]
    try: s["port"] = int(s["port"])
    except (TypeError, ValueError): s["port"] = DEFAULT_PORT
    return s


def save(new):
    """Check and store settings. Raises ValueError with a message the page can show."""
    s = load()
    if "work_dir" in new:
        wd = os.path.abspath(os.path.expanduser(str(new["work_dir"]).strip()))
        if not wd or wd == os.path.abspath(os.sep):
            raise ValueError("choose a projects folder")
        try:
            os.makedirs(wd, exist_ok=True)
            probe = os.path.join(wd, ".write_test")
            open(probe, "w").close(); os.remove(probe)
        except OSError as e:
            raise ValueError("cannot write to %s (%s)" % (wd, e.strerror or e))
        s["work_dir"] = wd
    if "video_dirs" in new:
        dirs = []
        for p in new["video_dirs"] or []:
            p = os.path.abspath(os.path.expanduser(str(p).strip()))
            if not os.path.isdir(p):
                raise ValueError("folder not found: %s" % p)
            if p not in dirs: dirs.append(p)
        s["video_dirs"] = dirs
    if "port" in new:
        try:
            port = int(new["port"])
        except (TypeError, ValueError):
            raise ValueError("port must be a number")
        if not 1024 <= port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        s["port"] = port
    tmp = SETTINGS + ".tmp"
    json.dump(s, open(tmp, "w", encoding="utf-8"), indent=2)
    os.replace(tmp, SETTINGS)
    return s


def setup_tools():
    """Make ffmpeg, ffprobe and yt-dlp findable without activating anything.

    The installer puts them in this tool's own .venv (yt-dlp) and, when the system has no ffmpeg,
    the static-ffmpeg package. Child processes inherit this PATH.
    """
    parts = os.environ.get("PATH", "").split(os.pathsep)
    add = [os.path.dirname(sys.executable), os.path.join(ROOT, "bin")]
    for d in add:
        if os.path.isdir(d) and d not in parts:
            parts.insert(0, d)
    os.environ["PATH"] = os.pathsep.join(parts)
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        try:
            import static_ffmpeg                       # downloaded once by the installer
            static_ffmpeg.add_paths()
        except Exception:
            pass
    # stages started from the page read their projects folder from here
    os.environ["CLIPPER_WORK"] = load()["work_dir"]


def font_dir():
    """Caption font shipped with the tool, so captions look the same on every machine."""
    return os.path.join(ROOT, "fonts")


def ff_path(p):
    """A file path written inside an ffmpeg filter (C:\\x\\y.ass -> C\\:/x/y.ass)."""
    return p.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
