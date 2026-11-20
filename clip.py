#!/usr/bin/env python3
"""clipper - one command: a long video (file or YouTube URL) becomes finished 9:16 short clips.

  python3 clip.py "https://youtu.be/XXXX" --count 8
  python3 clip.py ~/Downloads/gameplay.mp4 --count 6 --top webcam.mp4 --top-pct 40
  python3 clip.py ~/Downloads/gameplay.mp4 --layout crop --model small

Stages, each cached in work/<job>/ so a re-run only redoes what changed:
  ingest -> transcribe -> moments -> reframe -> render -> sheet + captions file
Nothing is ever posted: the clips land in work/<job>/out/ for review.
"""
import argparse, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ingest as S1, transcribe as S2, moments as S3, reframe as S4, render as S5


def contact_sheet(outdir, clips):
    """One strip of 3 frames per clip, so the whole batch can be judged in a single look."""
    tiles = []
    for c in clips:
        f = os.path.join(outdir, "clip_%02d.mp4" % c["n"])
        if not os.path.exists(f): continue
        for frac in (0.08, 0.5, 0.92):
            t = max(0.2, (c["end"] - c["start"]) * frac)
            png = os.path.join(outdir, ".sheet_%02d_%d.png" % (c["n"], int(frac * 100)))
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "%.2f" % t, "-i", f, "-frames:v", "1",
                            "-vf", "scale=240:-1", png], check=False)
            if os.path.exists(png): tiles.append(png)
    if not tiles: return None
    sheet = os.path.join(outdir, "sheet.png")
    args = []
    for t in tiles: args += ["-i", t]
    rows = (len(tiles) + 8) // 9
    cols = min(9, len(tiles))
    fc = "".join("[%d:v]" % i for i in range(len(tiles))) + "xstack=inputs=%d:grid=%dx%d:fill=black" % (len(tiles), cols, rows) if len(tiles) == cols * rows else "".join("[%d:v]" % i for i in range(len(tiles))) + "hstack=inputs=%d" % len(tiles)
    subprocess.run(["ffmpeg", "-y", "-v", "error"] + args + ["-filter_complex", fc, "-frames:v", "1", sheet], check=False)
    for t in tiles:
        try: os.remove(t)
        except OSError: pass
    return sheet if os.path.exists(sheet) else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("src", help="local video or YouTube URL")
    p.add_argument("--name"); p.add_argument("--count", type=int, default=6)
    p.add_argument("--min", type=float, default=18.0); p.add_argument("--max", type=float, default=50.0)
    p.add_argument("--model", default="medium", choices=S2.MODELS)
    p.add_argument("--lang", default=None); p.add_argument("--cpu", action="store_true")
    p.add_argument("--style", choices=sorted(S3.STYLES), default="talk",
                   help="talk = reward what is said; gameplay = reward what happens on screen")
    p.add_argument("--layout", choices=["auto", "stack", "crop"], default="auto")
    p.add_argument("--top", default=None, help="camera clip or still for the top band")
    p.add_argument("--top-pct", type=float, default=38.0)
    p.add_argument("--fill", default="blur"); p.add_argument("--round", type=int, default=28)
    p.add_argument("--cap-size", type=int, default=72); p.add_argument("--no-caps", action="store_true")
    p.add_argument("--cut-silence", action="store_true", help="shorten word-free stretches that are also quiet")
    p.add_argument("--cut-filler", action="store_true", help="drop um, uh and isolated verbal tics")
    p.add_argument("--gap", type=float, default=0.6)
    p.add_argument("--hook", default=None, help="text burned over the first 3 s; 'auto' uses the clip's own first line")
    p.add_argument("--cta", default=None)
    p.add_argument("--outdir", default=None, help="where the finished clips go (default work/<job>/out)")
    p.add_argument("--only", type=int, default=None, help="render just this clip number")
    p.add_argument("--redo", default="", help="comma list of stages to force: transcribe,moments,reframe")
    p.add_argument("--skip-asr", action="store_true", help="no speech in the source: score on audio + scene cuts only")
    a = p.parse_args()
    redo = set(x.strip() for x in a.redo.split(",") if x.strip())
    t0 = time.time()

    meta = S1.ingest(a.src, a.name)
    d = meta["dir"]
    S1.extract_audio(meta)                 # ingest no longer does this: it is the slow part
    if not a.skip_asr:
        S2.run(d, a.model, a.lang, not a.cpu, "transcribe" in redo)
    mom = S3.find(d, a.count, a.min, a.max, force=("moments" in redo or a.style != "talk"), style=a.style)
    S4.plan(d, a.layout, a.top, a.top_pct, force="reframe" in redo or a.top is not None)

    outdir = os.path.abspath(os.path.expanduser(a.outdir)) if a.outdir else os.path.join(d, "out")
    done = []
    for c in mom["clips"]:
        if a.only and c["n"] != a.only: continue
        hook = a.hook
        if hook == "auto":
            hook = " ".join(c["text"].split()[:7]) if c.get("text") else None
        done.append(S5.build(d, c["n"], outdir, hook, a.cta, a.top, a.top_pct, a.fill,
                             a.round, a.cap_size, no_caps=a.no_caps,
                             cut_silence=a.cut_silence, cut_filler=a.cut_filler, gap=a.gap))
    sheet = contact_sheet(outdir, mom["clips"])
    json.dump({"job": meta["job"], "clips": mom["clips"], "files": done},
              open(os.path.join(d, "result.json"), "w"), indent=2)
    print("\n%d clips in %s  (%.0f s total)" % (len(done), outdir, time.time() - t0))
    if sheet: print("contact sheet: %s" % sheet)


if __name__ == "__main__":
    main()
