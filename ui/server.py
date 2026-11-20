#!/usr/bin/env python3
"""Clipper Studio - a local editor for turning a long video into short 9:16 clips.

Nothing is uploaded: you pick a file that is already on this machine (or paste a YouTube URL) and every
stage runs here. Stdlib only, same as the Agent Studio dashboard.  http://127.0.0.1:8804

  start.sh / start.bat (or: python ui/server.py)     # then open the page
"""
import html, json, mimetypes, os, re, shutil, subprocess, sys, threading, time, traceback, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # this copy of the tool (v1 and v2 never share)
sys.path.insert(0, ROOT)
import config                                       # projects folder + video folders, per machine
config.setup_tools()                                # ffmpeg / yt-dlp from the tool's own .venv
H = os.path.expanduser("~")
SET = config.load()
WORK = SET["work_dir"]                              # changed from the Settings dialog
PORT = SET["port"]
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")

LOCK = threading.RLock()
JOBS = {}                                          # job_id -> {state, log[], pct, title}
PROCS = {}                                         # job_id -> Popen, so a stage can be cancelled


def slug_of(src, name=None):
    """The folder ingest.py will create, so the page can open the job the moment ingest finishes."""
    import ingest as _ing
    base = name or (("yt_" + src[-11:]) if src.startswith("http") else os.path.splitext(os.path.basename(src))[0])
    return _ing.slug(base)


# ---------------- helpers ----------------

def jl(p, d=None):
    try:
        return json.load(open(p))
    except Exception:
        return d


def work_file(p):
    """Resolve a /media path, refusing anything whose folder is not inside WORK.

    The path itself and every directory component must resolve inside work, so /etc/passwd,
    ../../ingest.py and a symlinked directory all get refused. The last component may be a symlink,
    because ingest.py stores each job's video as work/<job>/source.mp4 -> the original file.
    """
    if not p: return None
    root = os.path.realpath(WORK)
    fp = os.path.abspath(os.path.expanduser(p))
    if fp == root: return None                         # the folder itself is not a file to serve
    d = os.path.realpath(os.path.dirname(fp))
    if d == root or d.startswith(root + os.sep): return fp
    # a video or image may be previewed before it is ingested when it sits in home, a video folder
    # from Settings, or the folder of a job's original (Windows keeps the original path when it
    # cannot link it); anything else (and any non-media file) stays refused
    if not fp.lower().endswith(VIDEO_EXT + IMG_EXT): return None
    for r in media_roots():
        if d == r or d.startswith(r.rstrip(os.sep) + os.sep): return fp
    return None


def media_roots():
    roots = [H] + list(SET.get("video_dirs") or [])
    try:
        for name in os.listdir(WORK):
            m = jl(os.path.join(WORK, name, "meta.json"), {}) or {}
            if m.get("video") and not str(m.get("src", "")).startswith("http"):
                roots.append(os.path.dirname(m["video"]))
    except OSError:
        pass
    return [os.path.realpath(os.path.expanduser(r)) for r in roots if r]


def export_roots():
    """Export folders saved by the editor (ui.json outdir), so Open folder reaches them wherever
    the user put them - a different drive or /tmp - without opening arbitrary paths."""
    out = []
    try:
        for name in os.listdir(WORK):
            ui = jl(os.path.join(WORK, name, "ui.json"), {}) or {}
            if ui.get("outdir"): out.append(ui["outdir"])
    except Exception:
        pass
    return [os.path.realpath(os.path.expanduser(p)) for p in out]


def reveal_target(p):
    """What /api/reveal may hand to the OS opener: an existing file or folder inside work/, home,
    or an export folder this editor saved. A URL, /etc, a path that does not exist: all refused."""
    fp = os.path.realpath(os.path.expanduser(p or H))
    if not os.path.exists(fp): return None
    for root in [os.path.realpath(WORK)] + media_roots() + export_roots():
        if fp == root or fp.startswith(root + os.sep): return fp
    return None


def job_new(title):
    jid = "j%d" % int(time.time() * 1000)
    with LOCK:
        JOBS[jid] = {"id": jid, "title": title, "state": "running", "log": [], "started": time.time(), "pct": 0}
    return jid


def job_log(jid, line, pct=None):
    with LOCK:
        j = JOBS.get(jid)
        if not j: return
        j["log"].append(line[:400])
        j["log"] = j["log"][-200:]
        if pct is not None: j["pct"] = pct


def job_end(jid, state, line=""):
    with LOCK:
        j = JOBS.get(jid)
        if not j: return
        if line: j["log"].append(line[:400])
        j["state"] = state
        j["pct"] = 100 if state == "done" else j.get("pct", 0)
        j["ended"] = time.time()


def run_bg(title, fn):
    jid = job_new(title)

    def go():
        try:
            fn(jid)
            if JOBS[jid]["state"] == "running":
                job_end(jid, "done", "finished")
        except SystemExit as e:
            job_end(jid, "error", str(e))
        except Exception:
            job_end(jid, "error", traceback.format_exc().splitlines()[-1])
    threading.Thread(target=go, daemon=True).start()
    return jid


def stream(jid, cmd, cwd=ROOT, on_line=None):
    """Run a command and push each line into the job log, live.

    on_line sees every line as it arrives - the percentage in the page's progress bar has to move
    while a download or an encode runs, not once it has finished."""
    job_log(jid, "$ " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    with LOCK: PROCS[jid] = p                          # so Cancel in the page can stop this stage
    seen = []
    for line in p.stdout:
        line = line.rstrip()
        if line:
            job_log(jid, line); seen.append(line)
            if on_line: on_line(line)
    p.wait()
    with LOCK: PROCS.pop(jid, None)
    if p.returncode:
        raise SystemExit("exited %d" % p.returncode)
    return seen


# ---------------- pipeline actions ----------------

def a_ingest(src, name):
    """Import a file or a link: download/link it, then build the browser-safe preview copy.

    ingest.py prints one progress line at a time ("downloading 720p: 41% of 180MiB",
    "preparing a preview copy 42%"), which is what the page shows while this runs. Audio extraction
    is started afterwards in its own background job, so the player never waits for it.
    """
    def fn(jid):
        job_log(jid, "reading source ...", 2)
        cmd = [sys.executable, os.path.join(ROOT, "ingest.py"), src]
        if name: cmd += ["--name", name]
        seen = {}
        def watch(line):
            m = re.match(r"job (\S+)", line)          # ingest prints the folder it created
            if m:
                seen["job"] = m.group(1)
                with LOCK: JOBS[jid]["job"] = m.group(1)
                return
            m = re.search(r"(\d{1,3})%$", line)        # download / proxy percentage
            if m:
                with LOCK: JOBS[jid]["pct"] = max(2, min(99, int(m.group(1))))
        stream(jid, cmd, on_line=watch)
        job = seen.get("job")
        if job:
            try:
                a_audio(job)                           # 16 kHz wav for ASR + VAD, in the background
            except BadJob:
                pass
    return run_bg("ingest", fn)


AUDIO_LOCKS = {}


def ensure_audio(job, jid=None):
    """The 16 kHz mono wav the transcriber and the moment scorer read.

    This used to run inside ingest with check=True and no output, which is why a multi-GB file looked
    like a hang. It runs here instead: in the background after ingest, and again (waiting) as the
    first step of transcribe/moments if it has not finished yet.
    """
    import ingest as _ing
    d = jd(job)
    meta = jl(os.path.join(d, "meta.json"), {}) or {}
    meta.setdefault("dir", d)
    wav = meta.get("wav") or os.path.join(d, "audio.wav")
    with LOCK:
        lk = AUDIO_LOCKS.setdefault(job, threading.Lock())
    with lk:                                           # two stages asking at once extract it once
        if os.path.exists(wav) and os.path.getsize(wav) > 1024:
            return wav
        if jid: job_log(jid, "extracting audio (16 kHz mono) ...", 2)
        out = _ing.extract_audio(meta, (lambda v: job_log(jid, "audio %d%%" % v, v)) if jid else None)
        if jid: job_log(jid, "audio ready", 100)
        return out


def a_audio(job):
    jd(job)
    return run_bg("audio", lambda jid: ensure_audio(job, jid))


def a_proxy(job):
    """An H.264 preview copy for sources the browser cannot decode (HEVC, AV1, 10-bit captures).

    The pipeline still renders from the original; this file only feeds the editor's player."""
    d = jd(job)
    meta = jl(os.path.join(d, "meta.json"), {}) or {}
    out = os.path.join(d, "proxy.mp4")

    def fn(jid):
        job_log(jid, "building an H.264 preview copy - the render still uses the original", 5)
        import ingest as _ing
        _ing.build_proxy(meta["video"], out, meta.get("dur") or 0)
        mp = os.path.join(d, "meta.json")                # so the page picks it up on the next refresh
        m2 = jl(mp, {}) or {}
        m2["proxy"] = out
        json.dump(m2, open(mp, "w"), indent=2)
        job_log(jid, "preview copy ready", 100)
    return run_bg("proxy", fn)


def a_transcribe(job, model):
    jd(job)                                            # a bad job id must never reach a subprocess
    def fn(jid):
        ensure_audio(job, jid)
        job_log(jid, "whisper %s - this is the slow one, about 1 minute per 5 minutes of video" % model, 5)
        stream(jid, [sys.executable, os.path.join(ROOT, "transcribe.py"), os.path.join(WORK, job), "--model", model])
    return run_bg("transcribe", fn)


def a_moments(job, style, count, lo, hi):
    jd(job)
    def fn(jid):
        ensure_audio(job, jid)
        job_log(jid, "scoring windows (%s) ..." % style, 10)
        stream(jid, [sys.executable, os.path.join(ROOT, "moments.py"), os.path.join(WORK, job),
                     "--count", str(count), "--min", str(lo), "--max", str(hi), "--style", style, "--force"])
    return run_bg("find moments", fn)


def a_reframe(job, layout, top, top_pct, device, subject=None, clip=None):
    jd(job)
    def fn(jid):
        cmd = [sys.executable, os.path.join(ROOT, "reframe.py"), os.path.join(WORK, job),
               "--layout", layout, "--top-pct", str(top_pct), "--force"]
        if top: cmd += ["--top", top]
        if device: cmd += ["--device", device]
        if subject: cmd += ["--subject", "%.4f,%.4f" % (float(subject[0]), float(subject[1]))]
        if clip is not None: cmd += ["--clip", str(int(clip))]
        stream(jid, cmd)
    return run_bg("reframe", fn)


def a_render(job, ns, opts):
    d = jd(job)
    def fn(jid):
        outdir = opts.get("outdir") or os.path.join(d, "out")
        # remember where this job last rendered, so the page keeps finding the files
        uip = os.path.join(d, "ui.json")
        ui = jl(uip, {}) or {}
        ui.update({"outdir": outdir, "opts": opts})        # keep the editor project state next to it
        json.dump(ui, open(uip, "w"), indent=2)
        for i, n in enumerate(ns):
            job_log(jid, "rendering clip %d ..." % n, int(100 * i / max(1, len(ns))))
            cmd = [sys.executable, os.path.join(ROOT, "render.py"), d,
                   "--n", str(n), "--out", outdir,
                   "--fill", opts.get("fill", "blur"),
                   "--round", str(opts.get("round", 28)), "--cap-size", str(opts.get("cap_size", 72)),
                   "--bitrate", opts.get("bitrate", "9M"),
                   "--size", opts.get("size", "1080x1920"), "--fps", str(opts.get("fps", 30)),
                   "--cap-style", opts.get("cap_style", "clean"),
                   "--cap-words", str(opts.get("cap_words", 4))]
            if opts.get("cap_color"): cmd += ["--cap-color", opts["cap_color"]]
            if opts.get("cap_y") is not None: cmd += ["--cap-y", str(opts["cap_y"])]
            if opts.get("band_fit"): cmd += ["--band-fit"]
            if opts.get("top_pct") is not None:
                cmd += ["--top-pct", str(opts["top_pct"])]  # no key: the plan's own height stands
            if opts.get("top"):
                cmd += ["--top", opts["top"]]
            elif opts.get("no_top"):
                cmd += ["--no-top"]                     # only an explicitly cleared field drops the plan's band
            # neither key: leave the band to the clip plan (reframe.py --top)
            hk = (opts.get("hooks") or {}).get(str(n)) or opts.get("hook")
            if hk: cmd += ["--hook", hk]
            if opts.get("cta"): cmd += ["--cta", opts["cta"]]
            if opts.get("no_caps"): cmd += ["--no-caps"]
            if opts.get("cut_silence"): cmd += ["--cut-silence"]
            if opts.get("cut_filler"): cmd += ["--cut-filler"]
            if opts.get("gap"): cmd += ["--gap", str(opts["gap"])]
            stream(jid, cmd)
        job_log(jid, "validating ...", 98)
        files = [os.path.join(outdir, "clip_%02d.mp4" % n) for n in ns]
        r = subprocess.run([sys.executable, os.path.join(ROOT, "validate.py"),
                            "--size", opts.get("size", "1080x1920")] + files,
                           capture_output=True, text=True)
        outline = [line for line in (r.stdout + r.stderr).splitlines() if line.strip()]
        for line in outline: job_log(jid, line)
        rep_p = os.path.join(d, "validate.json")
        rep = jl(rep_p, {}) or {}
        for line in outline:
            m = re.match(r"(PASS|FAIL)\s+clip_(\d+)\.mp4:?\s*(.*)", line.strip())
            if m: rep[str(int(m.group(2)))] = {"ok": m.group(1) == "PASS", "why": m.group(3)}
        json.dump(rep, open(rep_p, "w"), indent=2)
        if r.returncode:
            # a clip that fails validation must not end the job as "done"
            # (the FAIL line is already the last log entry; don't repeat it)
            bad = any(line.startswith("FAIL") for line in outline)
            raise SystemExit("" if bad else "validation failed")
    return run_bg("render", fn)


# ---------------- data for the page ----------------

def list_jobs():
    out = []
    if not os.path.isdir(WORK): return out
    for name in sorted(os.listdir(WORK)):
        d = os.path.join(WORK, name)
        meta = jl(os.path.join(d, "meta.json"))
        if not meta: continue
        out.append({"job": name, "title": meta.get("title") or name, "dur": meta.get("dur", 0),
                    "w": meta.get("w"), "h": meta.get("h"),
                    "has_transcript": os.path.exists(os.path.join(d, "transcript.json")),
                    "has_moments": os.path.exists(os.path.join(d, "moments.json")),
                    "clips": len((jl(os.path.join(d, "moments.json"), {}) or {}).get("clips", [])),
                    "mtime": os.path.getmtime(os.path.join(d, "meta.json"))})
    return out


def job_detail(job):
    d = jd(job)
    meta = jl(os.path.join(d, "meta.json")) or {}
    mom = jl(os.path.join(d, "moments.json"), {}) or {}
    ref = jl(os.path.join(d, "reframe.json"), {}) or {}
    tr = jl(os.path.join(d, "transcript.json"), {}) or {}
    plans = {p["n"]: p for p in ref.get("plans", [])}
    ui = jl(os.path.join(d, "ui.json"), {}) or {}
    outdir = ui.get("outdir") or os.path.join(d, "out")
    clips = []
    for c in mom.get("clips", []):
        f = os.path.join(outdir, "clip_%02d.mp4" % c["n"])
        clips.append(dict(c, layout=(plans.get(c["n"], {}) or {}).get("layout", "-"),
                          rendered=os.path.exists(f), file=f if os.path.exists(f) else None,
                          size=(os.path.getsize(f) if os.path.exists(f) else 0)))
    rep = jl(os.path.join(d, "validate.json"), {}) or {}
    for c in clips:
        v = rep.get(str(c["n"]))
        if v and c["rendered"]: c["valid"] = v
    proxy = meta.get("proxy") or os.path.join(d, "proxy.mp4")
    wav = meta.get("wav") or os.path.join(d, "audio.wav")
    return {"job": job, "meta": meta, "clips": clips, "outdir": outdir, "ui": ui.get("opts", {}),
            "proxy": proxy if (proxy and os.path.exists(proxy)) else None,
            "has_audio": os.path.exists(wav) and os.path.getsize(wav) > 1024,
            "plans": ref.get("plans", []), "project": ui.get("project", {}),
            "transcript": {"words": len(tr.get("words", [])), "sentences": len(tr.get("sentences", [])),
                           "lang": tr.get("lang"), "model": tr.get("model"), "device": tr.get("device")},
            "source": meta.get("video")}


# ---------------- editor data (timeline, waveform, project state) ----------------

class BadJob(Exception):
    """A job id that is not the name of a folder directly inside work/."""


def jd(job):
    """The work/<job> folder, and nothing else.

    Only one plain folder name that really exists is accepted, so no id can point at ../, an
    absolute path, a hidden folder or a symlinked one and read or write outside work/.
    """
    name = (job or "").strip()
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise BadJob("bad job id")
    d = os.path.realpath(os.path.join(WORK, name))
    if os.path.dirname(d) != os.path.realpath(WORK) or not os.path.isdir(d):
        raise BadJob("no such job: %s" % name)
    return d


def backup(path):
    if os.path.exists(path) and not os.path.exists(path + ".bak"):
        shutil.copy2(path, path + ".bak")


def waveform(job, bins=1600):
    """Downsample the energy track signals.py already computed, as 0..1 peaks the page can draw."""
    d = jd(job)
    sig = jl(os.path.join(d, "signals.json"), {}) or {}
    meta = jl(os.path.join(d, "meta.json"), {}) or {}
    e = sig.get("energy") or []
    step = sig.get("estep") or 0
    dur = meta.get("dur", 0) or (len(e) * step)
    if not e:
        return {"peaks": [], "dur": dur, "cuts": sig.get("cuts", []), "step": 0, "have": False}
    bins = max(100, min(6000, int(bins)))
    n = len(e)
    per = max(1, n // bins)
    lo, hi = -60.0, -5.0
    peaks, floor_ = [], []
    for i in range(0, n, per):
        chunk = e[i:i + per]
        mx = max(chunk)
        mn = min(chunk)
        peaks.append(round(max(0.0, min(1.0, (mx - lo) / (hi - lo))), 3))
        floor_.append(round(max(0.0, min(1.0, (mn - lo) / (hi - lo))), 3))
    live = sorted(v for v in e if v > -90)
    return {"peaks": peaks, "floor": floor_, "dur": dur, "step": step * per, "have": True,
            "cuts": sig.get("cuts", []), "median_db": (live[len(live) // 2] if live else None)}


def words_of(job, start=None, end=None):
    d = jd(job)
    tr = jl(os.path.join(d, "transcript.json"), {}) or {}
    ws = tr.get("words", [])
    out = []
    for i, w in enumerate(ws):
        if start is not None and w["e"] < start: continue
        if end is not None and w["s"] > end: continue
        out.append({"i": i, "w": w["w"], "s": w["s"], "e": w["e"]})
    return {"words": out, "total": len(ws),
            "sentences": [s for s in tr.get("sentences", [])
                          if (start is None or s["end"] >= start) and (end is None or s["start"] <= end)][:2000]}


def words_write(job, edits):
    """Write edited words back to transcript.json. transcript.json.bak keeps the original."""
    d = jd(job)
    p = os.path.join(d, "transcript.json")
    tr = jl(p)
    if not tr: raise SystemExit("no transcript")
    backup(p)
    shutil.copy2(p, p + ".prev")                       # the step before this edit, for a quick revert
    ws = tr.get("words", [])
    drop = set()
    for e in edits:
        i = int(e["i"])
        if i < 0 or i >= len(ws): continue
        if e.get("delete"):
            drop.add(i); continue
        if "w" in e: ws[i]["w"] = str(e["w"])[:80]
        if "s" in e: ws[i]["s"] = round(float(e["s"]), 3)
        if "e" in e: ws[i]["e"] = round(float(e["e"]), 3)
        if ws[i]["e"] <= ws[i]["s"]: ws[i]["e"] = ws[i]["s"] + 0.08
    if drop:
        ws = [w for i, w in enumerate(ws) if i not in drop]
    tr["words"] = ws
    # sentences are only an index for the page; rebuild the text of any sentence the edit touched
    for sen in tr.get("sentences", []):
        inside = [w["w"] for w in ws if w["s"] >= sen["start"] - 0.05 and w["e"] <= sen["end"] + 0.05]
        if inside: sen["text"] = " ".join(inside)
    json.dump(tr, open(p, "w"))
    return {"ok": True, "words": len(ws)}


def clips_write(job, clips):
    """Replace the clip list (trim, split, delete, reorder) and keep reframe.json in step."""
    d = jd(job)
    p = os.path.join(d, "moments.json")
    mom = jl(p, {}) or {}
    backup(p)
    out = []
    for c in clips:
        n = int(c["n"]); s = round(float(c["start"]), 2); e = round(float(c["end"]), 2)
        if e <= s: e = s + 0.5
        old = next((o for o in mom.get("clips", []) if o.get("n") == n), {})
        out.append(dict(old, n=n, start=s, end=e, dur=round(e - s, 2),
                        title=c.get("title", old.get("title", "")),
                        text=c.get("text", old.get("text", "")),
                        score=c.get("score", old.get("score", 0)),
                        why=old.get("why", [])))
    mom["clips"] = out
    json.dump(mom, open(p, "w"), indent=2)
    sync_plans(job)
    return {"ok": True, "clips": len(out)}


def sync_plans(job):
    """reframe.json must hold a plan for every clip, or render.py raises a KeyError."""
    d = jd(job)
    rp = os.path.join(d, "reframe.json")
    meta = jl(os.path.join(d, "meta.json"), {}) or {}
    mom = jl(os.path.join(d, "moments.json"), {}) or {}
    ref = jl(rp, {"plans": []}) or {"plans": []}
    old = {p["n"]: p for p in ref.get("plans", [])}
    ar = (meta.get("w") or 16) / max(1, meta.get("h") or 9)
    plans = []
    for c in mom.get("clips", []):
        p = dict(old.get(c["n"], {"layout": "stack" if ar > 1.2 else "crop", "top": None, "top_pct": 38.0}))
        if p.get("track") and (abs(p.get("start", -1) - c["start"]) > 0.01 or abs(p.get("end", -1) - c["end"]) > 0.01):
            p.pop("track", None); p.pop("tracked", None)   # stale: those samples were timed to the old in/out
        p.update({"n": c["n"], "start": c["start"], "end": c["end"]})
        plans.append(p)
    ref["plans"] = plans
    json.dump(ref, open(rp, "w"), indent=2)
    return ref


def boxes_write(job, n, body):
    """Store the hand-drawn crop rectangles (fractions of the source frame) on this clip's plan.

    boxes.top / boxes.bot are [x, y, w, h] in 0..1 of the source frame; split is the top half's
    percentage of the 1080x1920 output. boxes.top_fit / boxes.bot_fit (and box_fit for the single
    region) ask render.py to letterbox that rectangle instead of cover-cropping it, so the whole box
    stays visible. render.py reads all of this instead of the auto layout."""
    d = jd(job)
    rp = os.path.join(d, "reframe.json")
    if not os.path.exists(rp): sync_plans(job)
    ref = jl(rp, {"plans": []}) or {"plans": []}
    hit = None
    for p in ref.get("plans", []):
        if p.get("n") == int(n): hit = p
    if hit is None:
        sync_plans(job)
        ref = jl(rp, {"plans": []}) or {"plans": []}
        hit = next((p for p in ref["plans"] if p.get("n") == int(n)), None)
    if hit is None: raise SystemExit("no such clip")
    if body.get("layout"): hit["layout"] = body["layout"]
    for k in ("top_pct", "top", "fill", "round", "box_fit", "cap_y", "subject"):
        if body.get(k) is not None: hit[k] = body[k]
    # both portrait shapes may sit on one plan; hit["layout"] says which one render.py uses, so
    # flipping between full and split in the editor no longer throws the other one's boxes away
    if body.get("boxes") is not None: hit["boxes"] = body["boxes"]
    if body.get("box") is not None: hit["box"] = body["box"]
    if body.get("clear"):
        hit.pop("boxes", None); hit.pop("box", None); hit.pop("box_fit", None)
    if body.get("clear_subject"):
        hit.pop("subject", None)
    json.dump(ref, open(rp, "w"), indent=2)
    return {"ok": True, "plan": hit}


def project_read(job):
    d = jd(job)
    ui = jl(os.path.join(d, "ui.json"), {}) or {}
    return {"project": ui.get("project", {}), "opts": ui.get("opts", {}), "outdir": ui.get("outdir")}


def project_write(job, project):
    d = jd(job)
    p = os.path.join(d, "ui.json")
    ui = jl(p, {}) or {}
    ui["project"] = project
    if project.get("opts"): ui["opts"] = project["opts"]
    if project.get("outdir"): ui["outdir"] = project["outdir"]
    json.dump(ui, open(p, "w"), indent=2)
    return {"ok": True}


def browse(path):
    path = os.path.abspath(os.path.expanduser(path or H))
    if not os.path.isdir(path): path = H
    dirs, files = [], []
    try:
        for n in sorted(os.listdir(path), key=str.lower):
            if n.startswith("."): continue
            p = os.path.join(path, n)
            if os.path.isdir(p):
                dirs.append({"name": n, "path": p})
            elif n.lower().endswith(VIDEO_EXT + IMG_EXT):
                try: sz = os.path.getsize(p)
                except OSError: sz = 0
                files.append({"name": n, "path": p, "size": sz,
                              "kind": "image" if n.lower().endswith(IMG_EXT) else "video"})
    except PermissionError:
        pass
    return {"path": path, "up": os.path.dirname(path), "dirs": dirs[:400], "files": files[:400],
            "places": places()}


def places():
    """Shortcuts beside the folder list: home, the video folders, and on Windows every drive."""
    out = [{"name": "Home", "path": H}]
    for p in SET.get("video_dirs") or []:
        out.append({"name": os.path.basename(p.rstrip(os.sep)) or p, "path": p})
    if sys.platform.startswith("win"):
        import string
        for L in string.ascii_uppercase:
            if os.path.isdir(L + ":\\"): out.append({"name": L + ":", "path": L + ":\\"})
    elif sys.platform == "darwin" and os.path.isdir("/Volumes"):
        out.append({"name": "Drives", "path": "/Volumes"})
    elif os.path.isdir("/media"):
        out.append({"name": "Drives", "path": "/media"})
    seen, res = set(), []
    for x in out:
        if x["path"] not in seen and os.path.isdir(x["path"]): seen.add(x["path"]); res.append(x)
    return res


def open_in_os(fp):
    """Show a file or folder in the system file manager on Windows, macOS or Linux."""
    if sys.platform.startswith("win"):
        os.startfile(fp)                                     # noqa - Windows only
    elif sys.platform == "darwin":
        subprocess.Popen(["open", fp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", fp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def settings_get():
    s = dict(SET); s["first_run"] = config.first_run(); s["defaults"] = config.defaults()
    s["jobs_here"] = len(list_jobs())
    return s


def settings_set(body):
    global SET, WORK
    s = config.save(body)                                    # ValueError -> shown on the page
    SET = s; WORK = s["work_dir"]
    os.environ["CLIPPER_WORK"] = WORK                       # every stage started from now on
    os.makedirs(WORK, exist_ok=True)
    return settings_get()


ENV_CACHE = {}

def env_check():
    if ENV_CACHE: return ENV_CACHE
    out = {}
    for b in ("ffmpeg", "ffprobe", "yt-dlp"):
        out[b] = bool(shutil.which(b))
    for m in ("cv2", "PIL", "faster_whisper", "ultralytics"):
        try:
            __import__(m); out[m] = True
        except Exception:
            out[m] = False
    try:
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        out["h264_nvenc"] = "h264_nvenc" in enc
    except Exception:
        out["h264_nvenc"] = False
    missing = [k for k, v in out.items() if not v]
    out["_missing"] = missing
    ENV_CACHE.update(out)
    return out


def recent_videos(limit=25):
    out = []
    for base in list(SET.get("video_dirs") or []) + [H]:
        if not os.path.isdir(base): continue
        try:
            for n in os.listdir(base):
                p = os.path.join(base, n)
                if n.lower().endswith(VIDEO_EXT) and os.path.isfile(p):
                    out.append({"name": n, "path": p, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
        except (PermissionError, OSError):
            pass
    out.sort(key=lambda x: -x["mtime"])
    return out[:limit]


# ---------------- http ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str): body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _file_range(self, path):
        """Serve a local file with Range support, so the preview can scrub without copying anything."""
        if not os.path.isfile(path):
            return self._send(404, b"no such file", "text/plain")
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1): start = int(m.group(1))
                if m.group(2): end = min(int(m.group(2)), size - 1)
                if start >= size: start = 0
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk: break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            return self._get(u.path, q)
        except BadJob as e:
            return self._json({"error": str(e)}, 400)   # a bad job id answers, it never drops the connection

    def _get(self, p, q):
        if p in ("/", "/index.html"):
            page = open(os.path.join(HERE, "app.html"), "rb").read()
            return self._send(200, page, "text/html; charset=utf-8")
        if p == "/api/jobs":
            return self._json({"jobs": list_jobs()})
        if p == "/api/job":
            return self._json(job_detail(q.get("job", "")))
        if p == "/api/status":
            with LOCK:
                return self._json({"jobs": [JOBS[k] for k in sorted(JOBS)][-8:]})
        if p == "/api/browse":
            return self._json(browse(q.get("path")))
        if p == "/api/recent":
            return self._json({"files": recent_videos()})
        if p == "/api/waveform":
            return self._json(waveform(q.get("job", ""), int(q.get("bins", 1600))))
        if p == "/api/words":
            st = float(q["start"]) if q.get("start") else None
            en = float(q["end"]) if q.get("end") else None
            return self._json(words_of(q.get("job", ""), st, en))
        if p == "/api/project":
            return self._json(project_read(q.get("job", "")))
        if p == "/api/env":
            return self._json(env_check())
        if p == "/api/transcript":
            d = jl(os.path.join(jd(q.get("job", "")), "transcript.json"), {}) or {}
            return self._json({"sentences": d.get("sentences", [])[:4000]})
        if p == "/media":
            fp = work_file(q.get("path", ""))
            if not fp:
                return self._send(403, b"only files inside the clipper work folder can be served", "text/plain")
            return self._file_range(fp)
        if p == "/api/reveal":
            fp = reveal_target(q.get("path"))
            if not fp:
                return self._json({"error": "only an existing file or folder inside work/ or home can be opened"}, 403)
            open_in_os(fp)
            return self._json({"ok": True, "path": fp})
        if p == "/api/settings":
            return self._json(settings_get())
        return self._send(404, b"not found", "text/plain")

    def _upload(self, q):
        """Stream an uploaded file to disk in chunks, ffprobe it, then ingest it.

        Nothing is held in memory and nothing half-written is accepted: the file lands as .part and
        is only renamed once the whole body has arrived and ffprobe says it is a video.
        """
        name = os.path.basename(q.get("name") or "upload.mp4").replace("\x00", "")
        name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)[:120] or "upload.mp4"
        if not name.lower().endswith(VIDEO_EXT):
            name += ".mp4"
        total = int(self.headers.get("Content-Length") or 0)
        if total <= 0:
            return self._json({"error": "no file was sent"}, 400)
        updir = os.path.join(WORK, "_uploads")
        os.makedirs(updir, exist_ok=True)
        dest = os.path.join(updir, name)
        tmp = dest + ".part"
        got = 0
        try:
            with open(tmp, "wb") as f:
                while got < total:
                    chunk = self.rfile.read(min(1 << 20, total - got))
                    if not chunk: break
                    f.write(chunk); got += len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            if os.path.exists(tmp): os.remove(tmp)
            return self._json({"error": "upload failed: %s" % e}, 500)
        if got < total:
            os.remove(tmp)
            return self._json({"error": "upload stopped early - try again"}, 400)
        os.replace(tmp, dest)
        try:
            import ingest as _ing
            _ing.probe(dest)                           # unplayable files are refused here, not in the player
        except Exception as e:
            os.remove(dest)
            return self._json({"error": "choose a playable video file (%s)" % str(e)[:120]}, 400)
        return self._json({"job_id": a_ingest(dest, None), "job": slug_of(dest), "path": dest})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p == "/api/upload":
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                return self._upload(q)
            except Exception:
                return self._json({"error": traceback.format_exc().splitlines()[-1]}, 500)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "body is not json"}, 400)
        try:
            if p == "/api/settings":
                try:
                    return self._json(settings_set(body))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
            if p == "/api/ingest":
                return self._json({"job_id": a_ingest(body["src"], body.get("name")),
                                   "job": slug_of(body["src"], body.get("name"))})
            if p == "/api/proxy":
                return self._json({"job_id": a_proxy(body["job"])})
            if p == "/api/audio":
                return self._json({"job_id": a_audio(body["job"])})
            if p == "/api/cancel":
                with LOCK: pr = PROCS.get(body.get("job_id"))
                if pr:
                    pr.terminate(); job_end(body["job_id"], "error", "cancelled")
                    return self._json({"ok": True})
                return self._json({"ok": False, "error": "nothing running"})
            if p == "/api/transcribe":
                return self._json({"job_id": a_transcribe(body["job"], body.get("model", "medium"))})
            if p == "/api/moments":
                return self._json({"job_id": a_moments(body["job"], body.get("style", "talk"),
                                                       int(body.get("count", 6)), float(body.get("min", 18)),
                                                       float(body.get("max", 45)))})
            if p == "/api/reframe":
                return self._json({"job_id": a_reframe(body["job"], body.get("layout", "auto"),
                                                       body.get("top"), float(body.get("top_pct", 38)),
                                                       body.get("device"), body.get("subject"),
                                                       body.get("clip"))})
            if p == "/api/render":
                jd(body["job"])
                return self._json({"job_id": a_render(body["job"], body.get("clips") or [], body.get("opts") or {})})
            if p == "/api/clips":
                return self._json(clips_write(body["job"], body.get("clips") or []))
            if p == "/api/boxes":
                return self._json(boxes_write(body["job"], body["n"], body))
            if p == "/api/words":
                return self._json(words_write(body["job"], body.get("edits") or []))
            if p == "/api/project":
                return self._json(project_write(body["job"], body.get("project") or {}))
            if p == "/api/sync_plans":
                return self._json({"ok": True, "plans": len(sync_plans(body["job"])["plans"])})
            if p == "/api/trim":
                d = os.path.join(jd(body["job"]), "moments.json")
                mom = jl(d) or {"clips": []}
                for c in mom["clips"]:
                    if c["n"] == int(body["n"]):
                        c["start"] = round(float(body["start"]), 2)
                        c["end"] = round(float(body["end"]), 2)
                        c["dur"] = round(c["end"] - c["start"], 2)
                json.dump(mom, open(d, "w"), indent=2)
                return self._json({"ok": True})
        except BadJob as e:
            return self._json({"error": str(e)}, 400)
        except Exception:
            return self._json({"error": traceback.format_exc().splitlines()[-1]}, 500)
        return self._json({"error": "unknown endpoint"}, 404)


if __name__ == "__main__":
    os.makedirs(WORK, exist_ok=True)
    srv = None
    for port in range(PORT, PORT + 20):                     # busy port -> take the next free one
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler); break
        except OSError:
            continue
    if not srv:
        sys.exit("no free port between %d and %d" % (PORT, PORT + 19))
    url = "http://127.0.0.1:%d" % srv.server_address[1]
    print("Clipper Studio on " + url, flush=True)
    print("projects folder: " + WORK, flush=True)
    if "--open" in sys.argv:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv.serve_forever()
