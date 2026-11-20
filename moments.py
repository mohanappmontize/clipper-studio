#!/usr/bin/env python3
"""Stage 3 - moments: score the whole video and return the best self-contained short clips.

Three signals, all local and free:
  speech  - hook strength of the opening line, questions, numbers, named entities, "you" address, payoff words
  audio   - loudness envelope + sudden peaks (laughter, explosions, a kill, a win) from the 16 kHz wav
  visual  - scene-cut density (PySceneDetect), high during action, low during a menu or a cutscene
Gameplay with no commentary scores on audio + visual alone, so silent footage still produces clips.
Boundaries always land on a sentence edge when there is speech, otherwise on a scene cut.
"""
import argparse, json, math, os, re, subprocess, sys

HOOK = re.compile(r"\b(how|why|what|when|watch|look|listen|wait|stop|never|always|secret|trick|mistake|best|worst|"
                  r"first|biggest|finally|actually|insane|crazy|wild|careful|warning|here'?s|this is)\b", re.I)
PAYOFF = re.compile(r"\b(because|so that|which means|the reason|turns out|that'?s why|the trick|the point|result)\b", re.I)
NUM = re.compile(r"\b\d[\d,.]*\s*(%|percent|rs|rupees|dollars?|k|million|billion|x|fps|gb|hours?|minutes?|seconds?)?\b", re.I)
FILLER = re.compile(r"^(um|uh|erm|ah|like|you know|i mean|sort of|kind of)$", re.I)


# ---------- signals ----------

def audio_energy(wav, step=0.5):
    """RMS dB per step seconds, via one ffmpeg pass."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", wav, "-af",
                          "astats=metadata=1:reset=%d,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-" % max(1, int(step * 16000 / 1024)),
                          "-f", "null", "-"], capture_output=True, text=True).stdout
    vals = [float(m) for m in re.findall(r"RMS_level=(-?\d+\.?\d*)", out)]
    if not vals:
        return [], step
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", wav],
                               capture_output=True, text=True).stdout.strip() or 0)
    return vals, (dur / len(vals) if vals else step)


def scene_cuts(video, threshold=0.25, fps=4, width=160):
    """Scene cuts via ffmpeg's own scene score, on a tiny 4 fps copy.

    PySceneDetect decodes the whole file in python and takes longer on a 67-minute video than the GPU
    takes to transcribe it. ffmpeg does the same job in one optimised pass, and 4 fps is plenty for the
    1-2 s resolution the scorer needs.
    """
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", video, "-an", "-sn",
             "-vf", "fps=%d,scale=%d:-2,select='gt(scene,%.2f)',metadata=print:file=-" % (fps, width, threshold),
             "-f", "null", "-"], capture_output=True, text=True, timeout=1800).stdout
        return [round(float(m), 2) for m in re.findall(r"pts_time:(\d+\.?\d*)", out)]
    except Exception as e:
        print("scene detect skipped: %s" % str(e)[:100], file=sys.stderr)
        return []


def near(sorted_vals, t, tol):
    """True when sorted_vals holds a value within tol of t. bisect, so it stays cheap inside the window loop."""
    if not sorted_vals: return False
    import bisect
    i = bisect.bisect_left(sorted_vals, t)
    for j in (i - 1, i):
        if 0 <= j < len(sorted_vals) and abs(sorted_vals[j] - t) <= tol:
            return True
    return False


# ---------- scoring ----------

# A clip is good for different reasons depending on what the video is. "talk" rewards what is said;
# "gameplay" rewards what happens on screen and treats commentary as a bonus, not the point.
STYLES = {
    "talk":     {"speech": 1.0, "audio": 1.0, "visual": 1.0, "need_clean_start": True,
                 "peak_db": 8, "busy_cpm": 12},
    "gameplay": {"speech": 0.35, "audio": 2.4, "visual": 2.6, "need_clean_start": True,
                 "peak_db": 6, "busy_cpm": 8},
}


def window_score(ws, we, words, sentences, energy, estep, cuts, chapters, emed=None, by_t=None,
                 style="talk", sent_starts=None, sent_ends=None):
    w8 = STYLES.get(style, STYLES["talk"])
    sp = by_t(ws, we) if by_t else [w for w in words if w["s"] >= ws and w["e"] <= we]
    text = " ".join(w["w"] for w in sp)
    sc, why = 0.0, []

    # --- hard rule: never open mid-sentence. A clip that starts on half a line is unusable, whatever
    # else it scores. With speech present the start must sit on a sentence start or a scene cut.
    has_speech = bool(words)
    on_sentence = near(sent_starts, ws, 1.2) if sent_starts is not None else False
    on_cut = near(cuts, ws, 1.0)
    if w8["need_clean_start"] and has_speech and not (on_sentence or on_cut):
        return -99.0, text, ["mid-sentence start"]

    if sp:
        k = w8["speech"]
        opening = " ".join(w["w"] for w in sp[:14])
        if HOOK.search(opening):   sc += 2.2 * k; why.append("hook opener")
        if "?" in opening or re.match(r"^(how|why|what|can|should|do|does|is)\b", opening, re.I):
            sc += 1.4 * k; why.append("question opener")
        n_num = len(NUM.findall(text))
        if n_num:                  sc += min(1.6, 0.4 * n_num) * k; why.append("%d numbers" % n_num)
        if PAYOFF.search(text):    sc += 1.3 * k; why.append("payoff")
        if re.search(r"\byou\b|\byour\b", text, re.I): sc += 0.6 * k
        wpm = len(sp) / max(1e-6, (we - ws) / 60)
        if 110 <= wpm <= 210:      sc += 0.8 * k
        elif wpm < 60 and style == "talk": sc -= 1.0; why.append("sparse speech")
        sc -= 0.08 * k * sum(1 for w in sp if FILLER.match(w["w"].strip(".,?!")))

    if on_sentence and near(sent_ends, we, 1.2):
        sc += 1.0; why.append("clean sentence edges")

    if energy:
        k = w8["audio"]
        i0, i1 = int(ws / estep), int(we / estep)
        seg = [v for v in energy[i0:i1] if v > -90]
        if seg:
            med = emed if emed is not None else -30
            peak, mean = max(seg), sum(seg) / len(seg)
            spread = peak - min(seg)
            if peak - med > w8["peak_db"]: sc += 1.5 * k; why.append("audio peak +%.0f dB" % (peak - med))
            if mean - med > 2:             sc += 0.6 * k
            if mean - med < -8:            sc -= 1.5 * k; why.append("quiet stretch")
            if style == "gameplay" and spread > 14:
                sc += 0.8 * k; why.append("loud and quiet in one clip")

    if cuts:
        k = w8["visual"]
        n = sum(1 for c in cuts if ws <= c <= we)
        per_min = n / max(1e-6, (we - ws) / 60)
        if per_min > w8["busy_cpm"]:
            sc += 1.2 * k; why.append("%.0f cuts/min (action)" % per_min)
        elif per_min < 1:
            sc -= 0.4 * k
            if style == "gameplay": sc -= 1.6; why.append("static - menu or cutscene")
        if on_cut: sc += 0.5 * k; why.append("starts on a cut")

    for ch in chapters or []:
        if abs(ch.get("start_time", -99) - ws) < 3: sc += 1.0; why.append("chapter start")

    return sc, text, why


def snap(t, sentences, cuts, kind):
    """Pull a boundary onto the nearest sentence edge, else the nearest scene cut."""
    cands = [s["start"] for s in sentences] if kind == "start" else [s["end"] for s in sentences]
    cands = [c for c in cands if abs(c - t) <= 3.0]
    if cands: return min(cands, key=lambda c: abs(c - t))
    cands = [c for c in cuts if abs(c - t) <= 2.0]
    if cands: return min(cands, key=lambda c: abs(c - t))
    return t


def find(job_dir, count=8, lo=18.0, hi=60.0, stride=3.0, force=False, style="talk"):
    out = os.path.join(job_dir, "moments.json")
    if os.path.exists(out) and not force:
        print("moments cached"); return json.load(open(out))
    meta = json.load(open(os.path.join(job_dir, "meta.json")))
    tr = json.load(open(os.path.join(job_dir, "transcript.json"))) if os.path.exists(os.path.join(job_dir, "transcript.json")) else {"words": [], "sentences": []}
    words, sentences = tr["words"], tr["sentences"]
    sig_p = os.path.join(job_dir, "signals.json")          # the two ffmpeg passes are the slow part: cache them
    if os.path.exists(sig_p) and not force:
        sig = json.load(open(sig_p)); energy, estep, cuts = sig["energy"], sig["estep"], sig["cuts"]
        print("signals cached", file=sys.stderr)
    else:
        print("signals: audio energy ...", file=sys.stderr)
        energy, estep = audio_energy(meta["wav"])
        print("signals: scene cuts ...", file=sys.stderr)
        cuts = scene_cuts(meta["video"])
        json.dump({"energy": energy, "estep": estep, "cuts": cuts}, open(sig_p, "w"))
    print("  %d energy steps (%.2f s), %d scene cuts" % (len(energy), estep, len(cuts)), file=sys.stderr)

    live = sorted(v for v in energy if v > -90)
    emed = live[len(live) // 2] if live else -30
    import bisect
    sent_starts = sorted(x["start"] for x in sentences)
    sent_ends = sorted(x["end"] for x in sentences)
    cuts_sorted = sorted(cuts)
    wstarts = [w["s"] for w in words]
    def by_t(ws, we):                                          # words inside the window, without rescanning all of them
        i = bisect.bisect_left(wstarts, ws)
        j = bisect.bisect_right(wstarts, we)
        return [w for w in words[i:j] if w["e"] <= we]

    dur, cands = meta["dur"], []
    t = 2.0
    while t + lo < dur - 2:
        for length in (lo, (lo + hi) / 2, hi):
            we = min(t + length, dur - 1)
            if we - t < lo - 1: continue
            sc, text, why = window_score(t, we, words, sentences, energy, estep, cuts_sorted,
                                         meta.get("chapters"), emed, by_t, style, sent_starts, sent_ends)
            sc += 0.4 if 22 <= (we - t) <= 45 else 0           # the length that performs
            cands.append({"start": t, "end": we, "score": round(sc, 2), "text": text[:400], "why": why})
        t += stride

    cands = [c for c in cands if c["score"] > -50]             # mid-sentence starts are dropped outright
    if not cands:
        sys.exit("no clip survived the clean-start rule; widen --min/--max or use --style gameplay")
    cands.sort(key=lambda c: -c["score"])
    picked = []
    for c in cands:                                            # no overlap between chosen clips
        if any(not (c["end"] <= p["start"] + 1 or c["start"] >= p["end"] - 1) for p in picked):
            continue
        s = snap(c["start"], sentences, cuts, "start")
        e = snap(c["end"], sentences, cuts, "end")
        if e - s < lo - 2: s, e = c["start"], c["end"]
        c["start"], c["end"], c["dur"] = round(s, 2), round(e, 2), round(e - s, 2)
        picked.append(c)
        if len(picked) >= count: break

    picked.sort(key=lambda c: c["start"])
    for i, c in enumerate(picked, 1):
        c["n"] = i
        c["title"] = (c["text"][:70] + "...") if c["text"] else "gameplay moment %d" % i
    json.dump({"clips": picked, "cuts": cuts[:4000]}, open(out, "w"), indent=2)
    for c in picked:
        print("%2d  %7.1f-%7.1f  %4.1f s  score %5.2f  %s" %
              (c["n"], c["start"], c["end"], c["dur"], c["score"], ", ".join(c["why"])[:70]))
    return {"clips": picked, "cuts": cuts}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("job_dir"); p.add_argument("--count", type=int, default=8)
    p.add_argument("--min", type=float, default=18.0); p.add_argument("--max", type=float, default=60.0)
    p.add_argument("--stride", type=float, default=3.0); p.add_argument("--force", action="store_true")
    p.add_argument("--style", choices=sorted(STYLES), default="talk",
                   help="talk = reward what is said; gameplay = reward what happens on screen")
    a = p.parse_args()
    find(a.job_dir, a.count, a.min, a.max, a.stride, a.force, a.style)
