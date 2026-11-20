#!/usr/bin/env python3
"""One-click setup for Clipper Studio. Run by install.sh (macOS/Linux) or install.bat (Windows).

Everything goes into .venv next to this file; nothing is installed system-wide and no folder
path is hard-coded. Safe to run again: it only adds what is missing.

  python3 install.py              full setup + model download (~1.6 GB on first run)
  python3 install.py --no-models  skip the speech + person-detection model download
  python3 install.py --cpu        never install the NVIDIA (CUDA) build of torch
"""
import os, platform, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(ROOT, ".venv")
WIN = sys.platform.startswith("win")
MAC = sys.platform == "darwin"
PY = os.path.join(VENV, "Scripts" if WIN else "bin", "python.exe" if WIN else "python")


def say(msg):
    print("\n==> " + msg, flush=True)


def die(msg):
    print("\nSETUP FAILED: " + msg, flush=True)
    sys.exit(1)


def run(cmd, what, ok_fail=False):
    print("   $ " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode and not ok_fail:
        die("%s (exit %d). Fix the message above and run the installer again." % (what, r.returncode))
    return r.returncode == 0


def pip(*args, what="pip install"):
    return run([PY, "-m", "pip", "install", "--disable-pip-version-check", *args], what)


def venv_ok():
    if not os.path.exists(PY):
        return False
    try:
        return subprocess.run([PY, "-c", "import pip, encodings"], capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


def has_nvidia():
    if MAC or "--cpu" in sys.argv:
        return False
    smi = shutil.which("nvidia-smi")
    if not smi and WIN:
        cand = r"C:\Windows\System32\nvidia-smi.exe"
        smi = cand if os.path.exists(cand) else None
    if not smi:
        return False
    try:
        return "GPU" in subprocess.run([smi, "-L"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return False


def main():
    say("Python %s on %s %s" % (platform.python_version(), platform.system(), platform.machine()))
    if sys.version_info < (3, 9):
        die("Python 3.9 or newer is needed (found %s). Install it from python.org and run again."
            % platform.python_version())

    # 1. private environment. A .venv left half-built by an earlier failed run is rebuilt.
    if os.path.exists(VENV) and not venv_ok():
        say("The existing .venv does not work - rebuilding it")
        shutil.rmtree(VENV, ignore_errors=True)
    if not os.path.exists(PY):
        say("Creating the private Python environment (.venv)")
        try:
            import venv
            # symlinks on macOS/Linux: a COPIED interpreter from uv / pyenv / conda builds cannot
            # find its own standard library ("No module named 'encodings'")
            venv.EnvBuilder(with_pip=True, clear=True, symlinks=not WIN).create(VENV)
        except Exception as e:
            shutil.rmtree(VENV, ignore_errors=True)
            hint = (" On Ubuntu/Debian first run:  sudo apt install python3-venv"
                    if sys.platform.startswith("linux") else "")
            die("could not create .venv: %s.%s" % (e, hint))
    if not venv_ok():
        die("the .venv at %s cannot run pip. Delete the .venv folder and run the installer again." % VENV)
    pip("--upgrade", "pip", "wheel", what="upgrading pip")

    # 2. torch: CPU build unless an NVIDIA card is present (the CUDA build is ~2.5 GB)
    gpu = has_nvidia()
    say("Installing torch (%s)" % ("NVIDIA GPU build" if gpu else "CPU build"))
    if MAC:
        pip("torch", what="installing torch")
    elif gpu:
        pip("torch", "--index-url", "https://download.pytorch.org/whl/cu124", what="installing torch (GPU)")
    else:
        pip("torch", "--index-url", "https://download.pytorch.org/whl/cpu", what="installing torch (CPU)")

    # 3. everything else
    say("Installing Clipper Studio's packages (requirements.txt)")
    pip("-r", os.path.join(ROOT, "requirements.txt"), what="installing requirements.txt")
    if gpu and not WIN:
        # faster-whisper on the GPU needs cuBLAS + cuDNN; transcribe.py preloads them from here
        say("Adding NVIDIA speech libraries (optional)")
        run([PY, "-m", "pip", "install", "--disable-pip-version-check",
             "nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"], "nvidia libs", ok_fail=True)

    # 4. ffmpeg: use the system one when it exists, otherwise a private static copy
    say("Checking ffmpeg")
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        print("   system ffmpeg found: " + shutil.which("ffmpeg"))
    else:
        print("   no system ffmpeg - downloading a private copy (about 80 MB, once)")
        run([PY, "-c", "import static_ffmpeg; static_ffmpeg.add_paths(); "
                       "import shutil; assert shutil.which('ffmpeg'), 'ffmpeg missing'; print('   ok', shutil.which('ffmpeg'))"],
            "downloading ffmpeg")

    # 5. models, so the first real run does not stall on a download
    if "--no-models" not in sys.argv:
        say("Downloading the person-detection model (yolov8n, 6 MB)")
        run([PY, "-c", "from ultralytics import YOLO; YOLO('yolov8n.pt'); print('   ok')"],
            "downloading yolov8n.pt", ok_fail=True)
        say("Downloading the speech model (whisper medium, ~1.5 GB - the slow part)")
        run([PY, "-c", "from faster_whisper import WhisperModel; WhisperModel('medium', device='cpu', "
                       "compute_type='int8'); print('   ok')"], "downloading whisper medium", ok_fail=True)

    # 6. self-check with the same test the page uses
    say("Self-check")
    code = ("import sys, os; sys.path.insert(0, %r); import config; config.setup_tools()\n"
            "import shutil\n"
            "bad = [b for b in ('ffmpeg', 'ffprobe', 'yt-dlp') if not shutil.which(b)]\n"
            "for m in ('cv2', 'PIL', 'numpy', 'faster_whisper', 'ultralytics'):\n"
            "    try: __import__(m)\n"
            "    except Exception as e: bad.append('%%s (%%s)' %% (m, e))\n"
            "print('   missing: ' + ', '.join(bad) if bad else '   all tools found')\n"
            "sys.exit(1 if bad else 0)\n") % ROOT
    run([PY, "-c", code], "self-check")

    start = "start.bat" if WIN else "./start.sh"
    say("Done. Start Clipper Studio with:  %s" % start)
    print("   The page opens in your browser. On first start it asks where to keep projects\n"
          "   and which folders to list videos from - change both later under Settings.")


if __name__ == "__main__":
    main()
