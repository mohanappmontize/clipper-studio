#!/usr/bin/env python3
"""Stage 4 - reframe: decide, per clip, how the 16:9 source becomes 9:16.

Two layouts, chosen from what is actually in the frame:
  stack  - top band (a camera clip or a still) + the full uncropped 16:9 below. Right for gameplay and
           screen capture, where cropping would throw away the map, the HUD and half the action.
  crop   - a 9:16 crop that follows the subject. Right for a talking head.
The crop path tracks with YOLO (person) and OpenCV (face) every `--step` seconds, then smooths the centre
with an exponential filter so the frame never jitters, and only jumps when the subject really moves.
Output is a per-clip plan written to reframe.json; render.py turns it into ffmpeg.
"""
import argparse, json, math, os, subprocess, sys

W, H = 1080, 1920


def pick_box(boxes, w_img, prev):
    """Choose which detected person this frame's crop should follow.

    With no lock we take the most confident detection, the old behaviour. With a lock (the editor
    clicked a person) we take the detection closest to where that person was, in centre and in width,
    so a second person walking through no longer steals the frame."""
    if not boxes: return None
    if prev is None:
        conf, (x1, _, x2, _) = max(boxes)
        return ((x1 + x2) / 2) / w_img, (x2 - x1) / w_img, conf
    best = None
    for conf, (x1, _, x2, _) in boxes:
        cx, bw = ((x1 + x2) / 2) / w_img, (x2 - x1) / w_img
        d = abs(cx - prev[0]) + 0.5 * abs(bw - prev[1])
        if best is None or d < best[0]: best = (d, cx, bw, conf)
    if best is None or best[0] > 0.35: return None        # nobody near the locked subject this frame
    return best[1], best[2], best[3]


def sample_centres(video, start, end, step=0.4, model=None, device=None, subject=None):
    """Return [(t, cx_fraction, conf)] of the main subject across the clip.

    subject = (cx, w) fractions of a person the editor picked; the track then follows that one."""
    import cv2
    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    yolo = None
    if model:
        try:
            from ultralytics import YOLO
            yolo = YOLO(model)
            if device: yolo.to(device)
        except Exception as e:
            print("yolo off: %s" % str(e)[:80], file=sys.stderr)
    pts, t = [], start
    lock = tuple(subject) if subject else None            # (cx, width) of the person we are following
    miss = 0
    while t < end:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if not ok: break
        h, w = fr.shape[:2]
        cx, conf = 0.5, 0.0
        if lock is not None: cx = lock[0]                 # hold the locked subject when a frame misses
        if yolo is not None:
            r = yolo.predict(fr, classes=[0], verbose=False, conf=0.35, device=device or None)[0]      # class 0 = person
            boxes = [(float(b.conf), b.xyxy[0].tolist()) for b in r.boxes] if len(r.boxes) else []
            got = pick_box(boxes, w, lock)
            if not got and lock is not None and boxes:
                miss += 1
                if miss * step >= 1.5:                     # ~1.5 s with nobody near the lock = a camera cut:
                    got = pick_box(boxes, w, None)         # re-lock on the most confident person in the new shot
            if got:
                cx, bw, conf = got; miss = 0
                if subject: lock = (cx, bw)               # the lock walks with the person
            elif lock is not None:
                conf = 0.35                               # kept, but not confident enough to re-centre hard
        if conf < 0.4 and lock is None:
            g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            f = face.detectMultiScale(g, 1.2, 5, minSize=(int(w * 0.04),) * 2)
            if len(f):
                x, y, fw, fh = max(f, key=lambda b: b[2] * b[3])
                cx, conf = (x + fw / 2) / w, 0.6
        pts.append((round(t, 2), round(cx, 4), round(conf, 2)))
        t += step
    cap.release()
    return pts, fps


def smooth(pts, alpha=0.12, jump=0.18):
    """Exponential smoothing with a dead zone: small wander is ignored, a real move is followed."""
    out, cur = [], (pts[0][1] if pts else 0.5)
    for t, cx, conf in pts:
        if conf < 0.3:
            out.append((t, cur)); continue
        if abs(cx - cur) > jump:
            cur = cx                                      # subject really moved: cut straight there
        else:
            cur = cur + alpha * (cx - cur)
        out.append((t, round(min(0.85, max(0.15, cur)), 4)))
    return out


def plan(job_dir, layout="auto", top=None, top_pct=38.0, step=0.5, yolo="yolov8n.pt", force=False,
         device=None, subject=None, only=None):
    out = os.path.join(job_dir, "reframe.json")
    if os.path.exists(out) and not force:
        print("reframe cached"); return json.load(open(out))
    meta = json.load(open(os.path.join(job_dir, "meta.json")))
    clips = json.load(open(os.path.join(job_dir, "moments.json")))["clips"]
    src_ar = meta["w"] / max(1, meta["h"])
    prev = {}
    if os.path.exists(out):
        try: prev = {q["n"]: q for q in json.load(open(out)).get("plans", [])}
        except Exception: prev = {}
    plans = []
    for c in clips:
        if only is not None and c["n"] != only and c["n"] in prev:
            plans.append(prev[c["n"]]); continue             # re-tracking one clip leaves the others alone
        lay = layout
        if lay == "auto":
            lay = "stack" if src_ar > 1.2 else "crop"       # a 16:9 source keeps all of itself in a stack
        p = {"n": c["n"], "start": c["start"], "end": c["end"], "layout": lay,
             "top": top, "top_pct": top_pct}
        if lay == "crop":
            sub = subject or (prev.get(c["n"], {}) or {}).get("subject")
            pts, fps = sample_centres(meta["video"], c["start"], c["end"], step, yolo, device, sub)
            p["track"] = smooth(pts)
            p["tracked"] = sum(1 for _, _, cf in pts if cf >= 0.4)
            if sub: p["subject"] = list(sub)
            print("clip %d crop: %d samples, %d with a subject%s"
                  % (c["n"], len(pts), p["tracked"], " (locked)" if sub else ""))
        else:
            print("clip %d stack: source %dx%d stays whole, band %.0f%%" % (c["n"], meta["w"], meta["h"], top_pct))
        for k in ("boxes", "box", "box_fit", "subject", "cap_y"):                         # hand-drawn crop rectangles survive a re-plan
            if k in prev.get(c["n"], {}): p[k] = prev[c["n"]][k]
        plans.append(p)
    json.dump({"plans": plans}, open(out, "w"), indent=2)
    return {"plans": plans}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("job_dir")
    p.add_argument("--layout", choices=["auto", "stack", "crop"], default="auto")
    p.add_argument("--top", default=None, help="camera clip or still for the top band (stack layout)")
    p.add_argument("--top-pct", type=float, default=38.0)
    p.add_argument("--step", type=float, default=0.5)
    p.add_argument("--yolo", default="yolov8n.pt")
    p.add_argument("--force", action="store_true")
    p.add_argument("--device", default=None, help="cpu to keep the GPU free for whisper")
    p.add_argument("--subject", default=None,
                   help="cx,width (fractions) of the person to follow; without it the track takes "
                        "whoever is most confident in each frame")
    p.add_argument("--clip", type=int, default=None, help="re-plan only this clip number")
    a = p.parse_args()
    sub = None
    if a.subject:
        try: sub = tuple(float(x) for x in a.subject.split(",")[:2])
        except ValueError: sub = None
    plan(a.job_dir, a.layout, a.top, a.top_pct, a.step, a.yolo, a.force, a.device, sub, a.clip)
