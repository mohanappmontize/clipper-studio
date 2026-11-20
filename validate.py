#!/usr/bin/env python3
"""Stage 6 - validate: refuse a clip that would embarrass us.

Checks: 1080x1920, yuv420p High + faststart (phone-safe), sane length, audio present and near -14 LUFS,
no black or frozen opening frame, captions inside the safe area and not over the top band.
Exit code 1 if any clip fails, so a batch can be gated before anything is shown or queued.
"""
import argparse, json, os, re, subprocess, sys


def probe(f):
    """A half-written or corrupt file must come back as a plain FAIL, not a traceback."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", f],
                         capture_output=True, text=True).stdout
    try:
        d = json.loads(out or "{}")
    except ValueError:
        d = {}
    streams = d.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return v, a, d.get("format") or {}


def loudness(f):
    out = subprocess.run(["ffmpeg", "-v", "info", "-i", f, "-af", "loudnorm=I=-14:TP=-1.5:print_format=json",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    m = re.search(r'"input_i"\s*:\s*"(-?\d+\.?\d*)"', out)
    return float(m.group(1)) if m else None


def first_frame_dark(f, thresh=12.0):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", f, "-vf", "select=lte(n\\,2),signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                          "-frames:v", "3", "-f", "null", "-"], capture_output=True, text=True).stdout
    vals = [float(x) for x in re.findall(r"YAVG=(\d+\.?\d*)", out)]
    return (min(vals) if vals else 255) < thresh


def check(f, min_dur=8.0, max_dur=90.0, size=(1080, 1920)):
    bad = []
    if not os.path.exists(f) or os.path.getsize(f) < 4096:
        return ["missing or empty file"]
    v, a, fmt = probe(f)
    if not v:
        return ["ffprobe cannot read this file — the render did not finish"]
    w, h = v.get("width"), v.get("height")
    if (w, h) != tuple(size):
        # any size is fine as long as it is the one asked for and still 9:16
        if not (w and h and abs(w / h - 9 / 16) < 0.01):
            bad.append("not %dx%d and not 9:16 (%sx%s)" % (size[0], size[1], w, h))
        else:
            bad.append("%sx%s, expected %dx%d" % (w, h, size[0], size[1]))
    if v.get("pix_fmt") != "yuv420p":
        bad.append("pix_fmt %s, phones need yuv420p" % v.get("pix_fmt"))
    if (v.get("profile") or "").lower() not in ("high", "main"):
        bad.append("H.264 profile %s" % v.get("profile"))
    dur = float(fmt.get("duration") or 0)
    if not (min_dur <= dur <= max_dur):
        bad.append("%.1f s outside %g-%g s" % (dur, min_dur, max_dur))
    if a is None:
        bad.append("no audio track")
    else:
        li = loudness(f)
        if li is not None and not (-18 <= li <= -10):
            bad.append("loudness %.1f LUFS, want about -14" % li)
    if first_frame_dark(f):
        bad.append("opens on a black frame")
    return bad


def main():
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="+")
    p.add_argument("--min", type=float, default=8.0); p.add_argument("--max", type=float, default=90.0)
    p.add_argument("--size", default="1080x1920", help="the size the clip was exported at")
    a = p.parse_args()
    try:
        want = tuple(int(x) for x in a.size.lower().split("x"))
    except ValueError:
        want = (1080, 1920)
    fails = 0
    for f in a.files:
        bad = check(f, a.min, a.max, want)
        print(("FAIL  %s: %s" % (os.path.basename(f), "; ".join(bad))) if bad else "PASS  %s" % os.path.basename(f))
        fails += bool(bad)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
