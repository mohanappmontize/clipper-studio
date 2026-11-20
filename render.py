#!/usr/bin/env python3
"""Stage 5 - render: turn one scored moment into a finished 9:16 clip.

Per clip it builds ONE ffmpeg command that does:
  cut -> layout (stacked 16:9, or a tracked 9:16 crop) -> hook card -> karaoke captions
      -> loudness normalise -> NVENC yuv420p High + faststart
A tracked crop drives the crop filter from a sendcmd file, so the frame follows the subject without
any per-frame python. Captions come from captions.py as ASS and are burned with libass.
The encoder's own preset is picked per codec and a failed hardware encoder falls back to libx264.
"""
import argparse, json, math, os, shlex, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import captions as cap
import config
import cleanup as cl

W, H = 1080, 1920                                 # the default export size; --size changes it
SIZES = {"1080x1920": (1080, 1920), "720x1280": (720, 1280), "1440x2560": (1440, 2560)}
QUALITY = {"high": "12M", "standard": "9M", "small": "5M"}   # --quality fills --bitrate

SOFT_CODEC = "libx264"                            # present in any normal ffmpeg build
PRESET = {"h264_nvenc": "p5", "hevc_nvenc": "p5",   # p1-p7 are NVENC-only: libx264 rejects p5
          "libx264": "medium", "libx265": "medium"}


def wrap_hook(text, max_chars=22, max_lines=2):
    """Keep a burned hook inside the frame: wrap to at most two short lines, then cut."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > max_chars and cur:
            lines.append(cur); cur = w
            if len(lines) == max_lines: break
        else:
            cur = (cur + " " + w).strip()
    if cur and len(lines) < max_lines: lines.append(cur)
    out = lines[:max_lines]
    if len(" ".join(words)) > sum(len(l) + 1 for l in out):
        out[-1] = out[-1][:max_chars - 1].rstrip(" ,.") + "..."
    return "\n".join(out)


# shipped with the tool so text renders on machines without fontconfig (most Windows ffmpeg builds)
FONT_BOLD = config.ff_path(os.path.join(config.font_dir(), "DejaVuSans-Bold.ttf"))


def esc_drawtext(s):
    # drawtext is used with expansion=none, so % is literal: escaping it as \% made every percent
    # hook fail to parse ("Stray % near ..."). Only the filtergraph's own : and \ need escaping.
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")


def sendcmd_file(path, track, src_w, src_h, start):
    """crop x over time, from the smoothed subject track."""
    cw = int(src_h * 9 / 16) // 2 * 2
    cw = min(cw, src_w)
    lines = []
    for t, cx in track:
        x = int(min(src_w - cw, max(0, cx * src_w - cw / 2))) // 2 * 2
        lines.append("%.2f crop x %d;" % (max(0.0, t - start), x))
    open(path, "w").write("\n".join(lines) + "\n")
    return cw


def build(job_dir, n, outdir, hook=None, cta=None, top=None, top_pct=38.0, fill="blur",
          round_px=28, cap_size=72, vcodec="h264_nvenc", bitrate="9M", fps=30, no_caps=False,
          cut_silence=False, cut_filler=False, gap=0.6, no_top=False, size="1080x1920",
          cap_y_pct=None, cap_style="clean", cap_color=None, cap_words=4, band_fit=False):
    W, H = SIZES.get(size, (1080, 1920))
    VH = (W * 9 // 16) // 2 * 2                   # a full-width 16:9 video inside the 9:16 frame
    k = H / 1920.0                                # every hard-coded 1080x1920 distance scales by this
    cap_size = max(14, int(round(cap_size * k)))
    meta = json.load(open(os.path.join(job_dir, "meta.json")))
    clips = {c["n"]: c for c in json.load(open(os.path.join(job_dir, "moments.json")))["clips"]}
    plans = {p["n"]: p for p in json.load(open(os.path.join(job_dir, "reframe.json")))["plans"]}
    tr = json.load(open(os.path.join(job_dir, "transcript.json"))) if os.path.exists(os.path.join(job_dir, "transcript.json")) else {"words": []}
    c, p = clips[n], plans[n]
    s, e = c["start"], c["end"]
    dur = e - s

    win = e - s                                            # how much source to read
    keep, cuts, saved = [(s, e)], [], 0.0
    if (cut_silence or cut_filler) and tr.get("words"):
        sig = json.load(open(os.path.join(job_dir, "signals.json"))) if os.path.exists(os.path.join(job_dir, "signals.json")) else {}
        live = sorted(v for v in sig.get("energy", []) if v > -90)
        emed = live[len(live) // 2] if live else None
        keep, cuts, saved = cl.plan_cuts(tr["words"], s, e, cut_silence, cut_filler, gap=gap,
                                         energy=sig.get("energy"), estep=sig.get("estep"), emed=emed)
        if saved > 0.05:
            dur = sum(b - a for a, b in keep)
            print("  cut %.2f s in %d places (%s)" % (saved, len(cuts), ", ".join(w for _, _, w in cuts)[:90]))
        else:
            keep, cuts, saved = [(s, e)], [], 0.0
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, "clip_%02d.mp4" % n)
    lay = p["layout"]
    boxes = p.get("boxes")                                 # two hand-drawn regions (the Rumble split screen)
    manbox = p.get("box")                                  # one hand-drawn 9:16 region
    # a plan can hold both portrait shapes at once (the editor keeps each one's rectangles while you
    # flip between them), so the plan's own layout decides which is live; older plans said "stack"
    if lay == "boxes" and boxes: pass
    elif lay == "crop" and manbox: boxes = None
    elif boxes: lay = "boxes"
    elif manbox: lay = "crop"
    # an explicit "no band" from the editor must beat a band the plan remembers
    top = None if no_top else (top or p.get("top"))
    # the band height the caller asked for always wins; a plan's stale top_pct only fills the gap
    top_pct = top_pct if top_pct is not None else p.get("top_pct", 38.0)
    try:
        top_pct = float(top_pct)
    except (TypeError, ValueError):
        top_pct = 38.0

    def rect(r, default=(0.0, 0.0, 1.0, 1.0)):
        """[x, y, w, h] in 0..1 of the source frame -> even pixel crop values."""
        x, y, w, h = (list(r) + list(default))[:4] if r else default
        sw, sh = meta["w"], meta["h"]
        cw = max(16, min(sw, int(round(w * sw)))) // 2 * 2
        ch = max(16, min(sh, int(round(h * sh)))) // 2 * 2
        cx = max(0, min(sw - cw, int(round(x * sw)))) // 2 * 2
        cy = max(0, min(sh - ch, int(round(y * sh)))) // 2 * 2
        return cw, ch, cx, cy

    def region(fc, src, lab, r, ow, oh, do_fit):
        """One hand-drawn rectangle -> an ow x oh region of the output.

        do_fit (the editor's "fit whole box") letterboxes: the WHOLE rectangle stays visible and the
        spare space is a blurred copy of it (or the flat --fill colour). Without it the rectangle is
        cover-cropped to the region's aspect, which is what loses the sides of a wide spec table."""
        cw, ch, cx, cy = rect(r)
        if not do_fit:
            fc.append("[%s]crop=%d:%d:%d:%d,scale=%d:%d:force_original_aspect_ratio=increase,"
                      "crop=%d:%d,setsar=1[%s]" % (src, cw, ch, cx, cy, ow, oh, ow, oh, lab))
        elif fill == "blur":
            fc.append("[%s]crop=%d:%d:%d:%d,split=2[%s_a][%s_b]" % (src, cw, ch, cx, cy, lab, lab))
            fc.append("[%s_a]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
                      "gblur=sigma=40,eq=brightness=-0.14,setsar=1[%s_bg]" % (lab, ow, oh, ow, oh, lab))
            fc.append("[%s_b]scale=%d:%d:force_original_aspect_ratio=decrease,setsar=1[%s_fg]"
                      % (lab, ow, oh, lab))
            fc.append("[%s_bg][%s_fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1[%s]" % (lab, lab, lab))
        else:
            fc.append("[%s]crop=%d:%d:%d:%d,scale=%d:%d:force_original_aspect_ratio=decrease,"
                      "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=%s,setsar=1[%s]"
                      % (src, cw, ch, cx, cy, ow, oh, ow, oh, fill, lab))

    inputs =["-ss", "%.3f" % s, "-t", "%.3f" % win, "-i", meta["video"]]
    idx = 1
    top_is_img = bool(top) and os.path.splitext(top)[1].lower() in (".jpg", ".jpeg", ".png", ".webp")
    if top:
        inputs += (["-loop", "1", "-i", top] if top_is_img else ["-ss", "%.3f" % s, "-t", "%.3f" % win, "-i", top])
        top_i = idx; idx += 1

    # mask / sendcmd / ass are inputs to the encoder only, so they live outside the export folder
    # and are deleted once the clip is written (B16).
    scratch = tempfile.mkdtemp(prefix="clip%02d-" % n)
    try:
        fc = []
        vsrc = "0:v"
        if cuts:
            expr = cl.select_expr(keep, s)
            uses = 2 if lay in ("stack", "boxes") else 1       # stack and split-screen read the source twice
            outs = "".join("[cut%d]" % i for i in range(uses))
            fc.append("[0:v]select='%s',setpts=N/FRAME_RATE/TB%s%s"
                      % (expr, (",split=%d" % uses) if uses > 1 else "", outs))
            vsrc = "cut0"
        if lay == "boxes":
            # split screen: the top rectangle fills the top region, the bottom rectangle the rest
            split = float((boxes or {}).get("split", 50))
            th = max(120, min(H - 120, int(round(H * split / 100)))) // 2 * 2
            bh = H - th
            if not cuts:
                fc.append("[%s]split=2[cut0][cut1]" % vsrc)   # both regions come from the same frame
            for lab, key, hh, src in (("tbox", "top", th, "cut0"), ("bbox", "bot", bh, "cut1")):
                region(fc, src, lab, (boxes or {}).get(key), W, hh,
                       bool((boxes or {}).get(key + "_fit")))
            fc.append("[tbox][bbox]vstack=inputs=2[comp]")
            cap_y = min(H - 120, th + bh - 230)
        elif lay == "stack":
            band = int(round(H * top_pct / 100)) // 2 * 2
            gap = H - band - VH
            if gap < 0:
                raise SystemExit("top-pct %.1f too tall: band %d + video %d > %d" % (top_pct, band, VH, H))
            vy = (band if top else (H - VH) // 2) // 2 * 2
            if fill == "blur":
                fc.append("[%s]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,gblur=sigma=40,eq=brightness=-0.14[bg]" % (vsrc, W, H, W, H))
            else:
                fc.append("color=c=%s:s=%dx%d:r=%d:d=%.3f[bg]" % (fill, W, H, fps, dur))
            fc.append("[%s]scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[vid]"
                      % ("cut1" if cuts else vsrc, W, VH, W, VH))
            if round_px:
                from PIL import Image, ImageDraw
                m = Image.new("L", (W, VH), 0)
                ImageDraw.Draw(m).rounded_rectangle([0, 0, W - 1, VH - 1], radius=round_px, fill=255)
                mask_path = os.path.join(scratch, "mask.png")
                Image.merge("RGBA", (m, m, m, m)).save(mask_path)
                inputs += ["-loop", "1", "-i", mask_path]
                mask_i = idx; idx += 1
                fc.append("[vid]format=yuva420p[vraw]")
                fc.append("[%d:v]format=gray,scale=%d:%d[mask]" % (mask_i, W, VH))
                fc.append("[vraw][mask]alphamerge=shortest=1[vidr]")
                vlab = "vidr"
            else:
                vlab = "vid"
            if top and band_fit:
                # the whole picture stays in the band; the spare width is a blurred copy of it
                fc.append("[%d:v]split=2[bnd_a][bnd_b]" % top_i)
                fc.append("[bnd_a]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
                          "gblur=sigma=40,eq=brightness=-0.14,setsar=1[bnd_bg]" % (W, band, W, band))
                fc.append("[bnd_b]scale=%d:%d:force_original_aspect_ratio=decrease,setsar=1[bnd_fg]" % (W, band))
                fc.append("[bnd_bg][bnd_fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1[band]")
            elif top:
                fc.append("[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1[band]" % (top_i, W, band, W, band))
            if top:
                fc.append("[bg][band]overlay=0:0:shortest=0[s1]")
                base = "s1"
            else:
                base = "bg"
            fc.append("[%s][%s]overlay=0:%d:shortest=1[comp]" % (base, vlab, vy))
            cap_y = min(H - 120, vy + VH + 170) if (gap > 260 or not top) else H - 230
        else:
            track = [] if manbox else (p.get("track") or [])
            if manbox:
                region(fc, vsrc, "comp", manbox, W, H, bool(p.get("box_fit")))
            elif track:
                cmds = os.path.join(scratch, "crop.cmd")
                cw = sendcmd_file(cmds, track, meta["w"], meta["h"], s)
                fc.append("[%s]sendcmd=f='%s',crop=%d:%d:0:0,scale=%d:%d,setsar=1[comp]" % (vsrc, config.ff_path(cmds), cw, meta["h"], W, H))
            else:
                fc.append("[%s]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1[comp]" % (vsrc, W, H, W, H))
            cap_y = 1480

        last = "comp"
        if hook:
            y0 = int((120 if lay == "crop" else 54) * k)
            for i, line in enumerate(wrap_hook(hook).split("\n")):     # two drawtexts, so no newline escaping
                fc.append("[%s]drawtext=expansion=none:fontfile='%s':text='%s':fontsize=%d:fontcolor=white:borderw=%d:bordercolor=black@0.85:"
                          "box=1:boxcolor=black@0.45:boxborderw=%d:x=(w-tw)/2:y=%d:"
                          "enable='between(t,0,%.2f)'[hook%d]" % (last, FONT_BOLD, esc_drawtext(line), int(64 * k),
                                                                  max(2, int(6 * k)), max(6, int(20 * k)),
                                                                  y0 + int(i * 84 * k), min(3.0, dur), i))
                last = "hook%d" % i

        if cta:
            fc.append("[%s]drawtext=expansion=none:fontfile='%s':text='%s':fontsize=%d:fontcolor=white:borderw=%d:bordercolor=black@0.8:"
                      "x=(w-tw)/2:y=h-%d:enable='gte(t,%.2f)'[cta]"
                      % (last, FONT_BOLD, esc_drawtext(cta), int(48 * k), max(2, int(4 * k)), int(150 * k), max(0.0, dur - 4)))
            last = "cta"

        if cap_y_pct is not None:                 # the editor dragged the caption block itself
            cap_y = int(max(0.06 * H, min(H - 0.03 * H, float(cap_y_pct) / 100.0 * H)))
        words = [w for w in tr["words"] if w["s"] >= s - 0.2 and w["e"] <= e + 0.2]
        cap_offset = s
        if words and cuts:
            words = cl.remap_words(words, keep, s)             # timestamps are already clip-relative now
            cap_offset = 0.0
        if words and not no_caps:
            ass = os.path.join(scratch, "cap.ass")
            cap.write_ass(words, ass, W, H, y_px=cap_y, size=cap_size, offset=cap_offset,
                          style=cap_style, active=cap.hex_to_ass(cap_color) if cap_color else None,
                          max_words=max(1, int(cap_words)))
            fc.append("[%s]subtitles=filename='%s':fontsdir='%s'[capd]" % (last, config.ff_path(ass), config.ff_path(config.font_dir())))
            last = "capd"
        fc.append("[%s]format=yuv420p[v]" % last)

        base = ["ffmpeg", "-y", "-v", "error", "-nostats"] + inputs + ["-filter_complex", ";".join(fc), "-map", "[v]"]
        if meta["audio"]:
            af = "loudnorm=I=-14:TP=-1.5:LRA=11"
            if cuts:
                af = "aselect='%s',asetpts=N/SR/TB,%s" % (cl.select_expr(keep, s), af)
            base += ["-map", "0:a", "-af", af, "-c:a", "aac", "-b:a", "192k"]
        else:
            base += ["-an"]

        def ffmpeg_cmd(codec):
            pres = PRESET.get(codec)                 # unlisted (*_vaapi, *_qsv) take -quality
            return base + ["-c:v", codec] + (["-preset", pres] if pres else []) + [
                "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", "18M",
                "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart", "-r", str(fps),
                "-t", "%.3f" % dur, out]

        print("clip %d  %s  %dx%d  %.1f s -> %s" % (n, lay, W, H, dur, os.path.basename(out)))
        cmd = ffmpeg_cmd(vcodec)
        r = subprocess.run(cmd)
        if r.returncode and vcodec != SOFT_CODEC:
            # the encoder is advertised but cannot start here (no GPU, driver, or the wrong preset)
            print("  %s failed, retrying with %s" % (vcodec, SOFT_CODEC))
            cmd = ffmpeg_cmd(SOFT_CODEC)
            r = subprocess.run(cmd)
        if r.returncode:
            print(" ".join(shlex.quote(x) for x in cmd)[:900]); raise SystemExit("ffmpeg failed on clip %d" % n)
        return out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("job_dir"); p.add_argument("--n", type=int, required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--hook"); p.add_argument("--cta")
    p.add_argument("--top"); p.add_argument("--top-pct", type=float, default=None)
    p.add_argument("--no-top", action="store_true", help="ignore any top band stored on the clip plan")
    p.add_argument("--fill", default="blur"); p.add_argument("--round", type=int, default=28)
    p.add_argument("--cap-size", type=int, default=72); p.add_argument("--no-caps", action="store_true")
    p.add_argument("--vcodec", default="h264_nvenc"); p.add_argument("--bitrate", default="9M")
    p.add_argument("--cut-silence", action="store_true", help="shorten word-free stretches that are also quiet")
    p.add_argument("--cut-filler", action="store_true", help="drop um, uh and isolated verbal tics")
    p.add_argument("--gap", type=float, default=0.6, help="a pause longer than this counts as silence")
    p.add_argument("--size", default="1080x1920", choices=sorted(SIZES), help="export frame size")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--quality", default=None, choices=sorted(QUALITY),
                   help="fills --bitrate: high 12M, standard 9M, small 5M")
    p.add_argument("--cap-y", type=float, default=None, help="caption baseline, %% of frame height")
    p.add_argument("--cap-style", default="clean", choices=sorted(cap.STYLES))
    p.add_argument("--cap-color", default=None, help="#RRGGBB for the spoken word")
    p.add_argument("--cap-words", type=int, default=4, help="most words on one caption line")
    p.add_argument("--band-fit", action="store_true", help="fit the whole top-band file, do not crop it")
    a = p.parse_args()
    build(a.job_dir, a.n, a.out or os.path.join(a.job_dir, "out"), a.hook, a.cta, a.top, a.top_pct,
          a.fill, a.round, a.cap_size, a.vcodec, a.quality and QUALITY[a.quality] or a.bitrate,
          fps=a.fps, no_caps=a.no_caps,
          cut_silence=a.cut_silence, cut_filler=a.cut_filler, gap=a.gap, no_top=a.no_top,
          size=a.size, cap_y_pct=a.cap_y, cap_style=a.cap_style, cap_color=a.cap_color,
          cap_words=a.cap_words, band_fit=a.band_fit)
