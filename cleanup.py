#!/usr/bin/env python3
"""Remove silences and filler words from a clip, from the transcript alone - no re-listening.

Produces the list of time ranges worth keeping, and the mapping that turns an original timestamp into its
place on the shortened timeline, so burned captions stay in sync after the cuts.

A cut is only made when it is safe: never inside a word, never shorter than --min-cut (a cut you cannot
hear is just a risk), and always leaving a little air either side so speech does not sound clipped.
"""
import re

FILLER = re.compile(r"^(um+|uh+|erm|ah+|eh|hmm+|like|so|well|yeah|okay|right|anyway|basically|literally|"
                    r"actually|honestly|obviously|i\s*mean|you\s*know|sort\s*of|kind\s*of)$", re.I)
# only these are ever cut on their own; the rest need to be isolated by pauses to count as filler
HARD_FILLER = re.compile(r"^(um+|uh+|erm|ah+|eh|hmm+)$", re.I)


def _clean(w):
    return w.strip().strip(".,!?;:’'\"").lower()


def filler_spans(words, pad=0.04, isolated_gap=0.18):
    """Spans to drop. A hard filler always goes. A soft one ('like', 'you know') only when it sits in a
    pause on at least one side, which is what makes it a verbal tic rather than a real word."""
    out = []
    for i, w in enumerate(words):
        t = _clean(w["w"])
        if not t:
            continue
        hard = bool(HARD_FILLER.match(t))
        soft = bool(FILLER.match(t)) and not hard
        if not (hard or soft):
            continue
        if soft:
            before = w["s"] - words[i - 1]["e"] if i else 99
            after = words[i + 1]["s"] - w["e"] if i + 1 < len(words) else 99
            if max(before, after) < isolated_gap:
                continue
        out.append((max(0.0, w["s"] - pad), w["e"] + pad, "filler:" + t))
    return out


def _quiet(a, b, energy, estep, emed, quiet_db):
    """Is this stretch actually silent? No words is not the same as no sound: a gunfight, an engine or
    music has no transcript but must never be cut. Without an energy track, refuse to call it silence."""
    if not energy or not estep:
        return False
    seg = [v for v in energy[int(a / estep):int(b / estep)] if v > -90]
    if not seg:
        return True
    return (sum(seg) / len(seg)) < (emed - quiet_db)


def silence_spans(words, lo, hi, gap=0.6, keep=0.18, lead=0.4,
                  energy=None, estep=None, emed=None, quiet_db=6.0):
    """Word-free stretches that are also quiet get shortened to `keep` seconds of air."""
    out = []
    prev = lo
    for w in words:
        if w["s"] - prev > gap and _quiet(prev, w["s"], energy, estep, emed, quiet_db):
            out.append((prev + keep / 2, w["s"] - keep / 2, "silence %.1fs" % (w["s"] - prev)))
        prev = max(prev, w["e"])
    if words and hi - prev > gap + lead and _quiet(prev, hi, energy, estep, emed, quiet_db):
        out.append((prev + lead, hi, "tail %.1fs" % (hi - prev)))
    return out


def plan_cuts(words, lo, hi, do_silence=True, do_filler=True, gap=0.6, min_cut=0.12,
              energy=None, estep=None, emed=None, quiet_db=6.0):
    """Return (keep_ranges, cuts, saved_seconds) in absolute source time."""
    ws = [w for w in words if w["e"] > lo and w["s"] < hi]
    cuts = []
    if do_silence: cuts += silence_spans(ws, lo, hi, gap=gap, energy=energy, estep=estep,
                                         emed=emed, quiet_db=quiet_db)
    if do_filler:  cuts += filler_spans(ws)
    cuts = [(max(lo, a), min(hi, b), why) for a, b, why in cuts if b - a >= min_cut and b > lo and a < hi]
    cuts.sort()

    merged = []                                            # overlapping cuts become one
    for a, b, why in cuts:
        if merged and a <= merged[-1][1] + 0.01:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b), merged[-1][2] + " + " + why)
        else:
            merged.append((a, b, why))

    keep, t = [], lo
    for a, b, _ in merged:
        if a - t > 0.05: keep.append((t, a))
        t = b
    if hi - t > 0.05: keep.append((t, hi))
    if not keep: keep = [(lo, hi)]
    saved = (hi - lo) - sum(b - a for a, b in keep)
    return keep, merged, round(saved, 2)


def remap(t, keep, lo):
    """An absolute source time, placed on the shortened timeline (relative to the clip start)."""
    out = 0.0
    for a, b in keep:
        if t < a: return round(out, 3)
        if t <= b: return round(out + (t - a), 3)
        out += b - a
    return round(out, 3)


def remap_words(words, keep, lo):
    """Words that survive the cuts, with timestamps on the new timeline.

    Whisper sometimes emits overlapping spans where it heard a repetition. Left alone those become two
    caption lines drawn on top of each other, so the times are forced to run strictly forward.
    """
    out = []
    for w in words:
        if not any(a - 0.01 <= w["s"] and w["e"] <= b + 0.01 for a, b in keep):
            continue
        s, e = remap(w["s"], keep, lo), remap(w["e"], keep, lo)
        if e <= s:
            continue
        if out and s < out[-1]["e"]:                      # never start before the previous word ended
            s = out[-1]["e"]
            e = max(e, s + 0.06)
        out.append({"w": w["w"], "s": round(s, 3), "e": round(e, 3), "p": w.get("p", 1.0)})
    return out


def select_expr(keep, lo):
    """ffmpeg select/aselect expression over the clip-relative timeline."""
    return "+".join("between(t,%.3f,%.3f)" % (a - lo, b - lo) for a, b in keep)
