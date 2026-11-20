#!/usr/bin/env python3
"""Word-level karaoke captions as an ASS file: one event per word, the spoken word highlighted.

Lines are built from the word list, never longer than --chars, and are placed in a safe band that the
caller chooses, so captions never sit over a face or over the top band of a stacked layout.
"""
import os, re

FONT = "DejaVu Sans"
# Hindi / Marathi words: DejaVu has no Devanagari, and a bare Windows ffmpeg has no fallback font,
# so lines would turn into boxes. Both fonts ship in fonts/ next to this file.
FONT_DEVANAGARI = "Noto Sans Devanagari"
_DEVA = re.compile(r"[\u0900-\u097F]")

# Caption looks the editor can pick between. Each one is a few ASS knobs, not a different writer:
#   box      BorderStyle 3 (a filled box behind the words) instead of an outline
#   pop      the spoken word springs up and settles, instead of just changing colour
#   grow     the whole line is drawn a touch larger while it is on screen
#   caps     the words are upper-cased
STYLES = {
    "clean":   dict(border=1, outline=5, pop=0, caps=False, active="&H0020E0FF&"),
    "box":     dict(border=3, outline=14, pop=0, caps=False, active="&H0020E0FF&"),
    "pop":     dict(border=1, outline=6, pop=1, caps=False, active="&H0020E0FF&"),
    "shout":   dict(border=3, outline=16, pop=1, caps=True,  active="&H004DFF4D&"),
    "neon":    dict(border=1, outline=8, pop=1, caps=False, active="&H00FF7AE0&"),
}


def hex_to_ass(c, default="&H0020E0FF&"):
    """#RRGGBB from a colour picker -> ASS's &HAABBGGRR&."""
    if not c: return default
    c = c.strip().lstrip("#")
    if len(c) != 6: return default
    try: r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError: return default
    return "&H00%02X%02X%02X&" % (b, g, r)


def _esc(s):
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def monotonic(words):
    """Force word times strictly forward. Whisper emits overlapping spans where it heard a repetition,
    and overlapping spans become two caption lines drawn on top of each other."""
    out = []
    for w in words:
        s, e = float(w["s"]), float(w["e"])
        if out and s < out[-1]["e"]:
            s = out[-1]["e"]
            e = max(e, s + 0.06)
        if e <= s:
            continue
        out.append(dict(w, s=round(s, 3), e=round(e, 3)))
    return out


def lines_from_words(words, max_chars=26, max_words=4, max_gap=0.7):
    lines, cur = [], []
    for w in monotonic(words):
        txt = w["w"].strip()
        if not txt: continue
        gap = cur and (w["s"] - cur[-1]["e"] > max_gap)
        too_long = cur and (sum(len(x["w"]) + 1 for x in cur) + len(txt) > max_chars or len(cur) >= max_words)
        if gap or too_long:
            lines.append(cur); cur = []
        cur.append(w)
        if re.search(r"[.!?]$", txt) and len(cur) >= 2:
            lines.append(cur); cur = []
    if cur: lines.append(cur)
    return [l for l in lines if l]


def write_ass(words, path, W=1080, H=1920, y_px=1500, size=72, active=None,
              base="&H00FFFFFF&", outline=None, max_chars=26, offset=0.0, bold=True,
              style="clean", max_words=4):
    """y_px = distance from the top of the frame to the caption baseline band.

    style picks one of STYLES; active (a colour) and outline override that style's own values."""
    st = dict(STYLES.get(style, STYLES["clean"]))
    if active: st["active"] = active
    if outline is not None: st["outline"] = outline
    active, outline, border, pop, caps = st["active"], st["outline"], st["border"], st["pop"], st["caps"]
    margin_v = max(10, H - y_px)
    font = FONT_DEVANAGARI if any(_DEVA.search(str(w.get("w") or w.get("word") or w.get("text") or "")) for w in words) else FONT
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{base},{base},&H00101010&,&HA0000000&,{1 if bold else 0},0,0,0,100,100,0,0,{border},{outline},{0 if border == 3 else 2},2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def ts(t):
        t = max(0.0, t - offset)
        h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
        return "%d:%02d:%05.2f" % (h, m, s)

    ev = []
    lines = lines_from_words(words, max_chars=max_chars, max_words=max_words)
    if caps:
        lines = [[dict(w, w=w["w"].upper()) for w in L] for L in lines]
    for li, line in enumerate(lines):
        # a line may linger a moment after its last word, but never into the next line
        nxt = lines[li + 1][0]["s"] if li + 1 < len(lines) else None
        tail = line[-1]["e"] + 0.12
        if nxt is not None: tail = min(tail, nxt)
        for i, w in enumerate(line):
            start = w["s"] if i else line[0]["s"]
            end = line[i + 1]["s"] if i + 1 < len(line) else tail
            if end <= start: continue
            parts = []
            for j, x in enumerate(line):
                t = _esc(x["w"].strip())
                if j != i:
                    parts.append(t); continue
                if pop:
                    # the word springs to 126% and settles back over 140 ms, then holds
                    hit = ("{\\c%s\\fscx126\\fscy126\\t(0,140,\\fscx108\\fscy108)}%s"
                           "{\\c%s\\fscx100\\fscy100}" % (active, t, base))
                else:
                    hit = "{\\c%s\\fscx108\\fscy108}%s{\\c%s\\fscx100\\fscy100}" % (active, t, base)
                parts.append(hit)
            ev.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s" % (ts(start), ts(end), " ".join(parts)))
    open(path, "w").write(head + "\n".join(ev) + "\n")
    return path, len(ev)
