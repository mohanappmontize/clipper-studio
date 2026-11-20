#!/usr/bin/env python3
"""9:16 stacked layout: top band (camera clip or static image) + the FULL 16:9 source below, never cropped.

  python3 stack916.py --main long.mp4 --top cam.mp4 --top-pct 40 --out out.mp4
  python3 stack916.py --main long.mp4 --top face.jpg --top-pct 35 --fill blur --out out.mp4

Layout maths (1080x1920): a 16:9 clip at full width is 1080x608 = 31.7% of the height, so the top band may be
anything from 0 to 68.3%. Whatever is left over after band + video is the gap; --gap says where it goes.
"""
import argparse, json, os, shlex, subprocess, sys

W, H = 1080, 1920


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,codec_type,nb_frames,duration", "-show_entries",
                          "format=duration", "-of", "json", path], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    st = (d.get("streams") or [{}])[0]
    dur = st.get("duration") or d.get("format", {}).get("duration")
    has_audio = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                "stream=codec_type", "-of", "csv=p=0", path],
                               capture_output=True, text=True).stdout.strip() != ""
    is_img = os.path.splitext(path)[1].lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp")
    return {"w": int(st.get("width") or 0), "h": int(st.get("height") or 0),
            "dur": float(dur) if dur else None, "audio": has_audio, "image": is_img}


def build(a):
    main, top = probe(a.main), probe(a.top)
    vh = round(W * 9 / 16) // 2 * 2                       # 608: the 16:9 video at full width
    band = round(H * a.top_pct / 100) // 2 * 2
    gap = H - band - vh
    if gap < 0:
        sys.exit("--top-pct %.1f leaves no room: band %d px + video %d px > %d px. Max is %.1f%%."
                 % (a.top_pct, band, vh, H, 100 * (H - vh) / H))
    if a.gap == "below":   vy = band                      # video right under the band, gap at the bottom (caption room)
    elif a.gap == "above": vy = H - vh                    # video pinned to the bottom
    else:                  vy = band + gap // 2           # centred in the leftover
    vy = vy // 2 * 2

    # background: blurred, scaled-up copy of the main video, or a flat colour
    if a.fill == "blur":
        bg = ("[0:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
              "gblur=sigma=40,eq=brightness=-0.12[bg];" % (W, H, W, H))
    else:
        bg = "color=c=%s:s=%dx%d:r=%d[bg];" % (a.fill, W, H, a.fps)

    # top band: cover-crop whatever is given (camera clip or still) into W x band
    tl = "[1:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d" % (W, band, W, band)
    if a.top_zoom and top["image"]:                       # slow push on a still so it is not dead
        tl += ",zoompan=z='min(1.08,1+0.08*on/%d)':d=1:s=%dx%d:fps=%d" % (max(1, int(a.fps * (a.dur or 10))), W, band, a.fps)
    tl += ",setsar=1[band];"

    mask_path = None
    if a.round:                                           # rounded video card, via a real alpha mask
        from PIL import Image, ImageDraw
        m = Image.new("L", (W, vh), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, W - 1, vh - 1], radius=a.round, fill=255)
        mask_path = os.path.splitext(a.out)[0] + "_mask.png"
        Image.merge("RGBA", (m, m, m, m)).save(mask_path)

    mainl = ("[0:v]scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
             % (W, vh, W, vh))
    if a.round:
        mainl += ",format=yuva420p[vraw];[2:v]format=gray,scale=%d:%d[mask];[vraw][mask]alphamerge=shortest=1[vid];" % (W, vh)
    else:
        mainl += "[vid];"

    fc = (bg + tl + mainl +
          "[bg][band]overlay=0:0:shortest=0[s1];" +
          "[s1][vid]overlay=0:%d:shortest=1,format=yuv420p[v]" % vy)

    cmd = ["ffmpeg", "-y", "-i", a.main]
    cmd += (["-loop", "1", "-i", a.top] if top["image"] else ["-i", a.top])
    if mask_path:
        cmd += ["-loop", "1", "-i", mask_path]
    cmd += ["-filter_complex", fc, "-map", "[v]"]

    # audio: main video by default; --audio top uses the camera clip (a talking-head over b-roll)
    src = 1 if (a.audio == "top" and top["audio"]) else 0
    if (src == 0 and main["audio"]) or (src == 1 and top["audio"]):
        cmd += ["-map", "%d:a" % src, "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    if a.dur:
        cmd += ["-t", str(a.dur)]
    cmd += ["-c:v", a.vcodec, "-preset", a.preset, "-b:v", a.bitrate, "-maxrate", a.bitrate, "-bufsize", "20M",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart", "-r", str(a.fps), a.out]

    print("band %d px (%.1f%%)  video 1080x%d at y=%d  gap %d px (%s)" % (band, 100 * band / H, vh, vy, gap, a.gap))
    print(" ".join(shlex.quote(c) for c in cmd)[:400])
    subprocess.run(cmd, check=True)
    print("wrote", a.out)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--main", required=True, help="the 16:9 video that must stay uncropped")
    p.add_argument("--top", required=True, help="camera clip or a still image for the top band")
    p.add_argument("--top-pct", type=float, default=38.0, help="band height as %% of 1920 (0-68.3)")
    p.add_argument("--gap", choices=["below", "centre", "above"], default="below",
                   help="where the leftover space goes: below the video (caption room), split, or above it")
    p.add_argument("--fill", default="blur", help="'blur' (blurred main video) or an ffmpeg colour like black/#101014")
    p.add_argument("--audio", choices=["main", "top"], default="main")
    p.add_argument("--round", type=int, default=0, help="corner radius on the video card, px (0 = square)")
    p.add_argument("--top-zoom", action="store_true", help="slow push on a still top band")
    p.add_argument("--dur", type=float, default=None)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--vcodec", default="h264_nvenc")
    p.add_argument("--preset", default="p5")
    p.add_argument("--bitrate", default="9M")
    p.add_argument("--out", required=True)
    build(p.parse_args())
