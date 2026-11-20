#!/usr/bin/env python3
"""Stage 1 - ingest: a local file or a video URL becomes a working folder with the source, a
browser-safe preview copy and metadata.

Two things here are load-bearing and were wrong before:

* the browser cannot decode HEVC / VP9 / AV1 / 10-bit / ProRes / most .mkv and .mov files, so every
  source that is not plain 8-bit H.264 gets a **proxy**: H.264 yuv420p + AAC + faststart, at most
  720p. The editor's <video> and all of its preview geometry use the proxy; render.py keeps using the
  original file, so quality is untouched.
* audio extraction is NOT done here. On a multi-GB source it takes minutes, and it used to run
  synchronously before the page was ever given a video. The server extracts it in the background
  (ui/server.py ensure_audio) and the transcribe/moments stages wait for it.

Progress is printed as single lines ("proxy 42%", "[download] ...") so the server can stream them
into the page while the work runs.
"""
import argparse, json, os, re, shutil, subprocess, sys, time
from urllib.parse import urlparse

# the work folder belongs to THIS copy of the tool (v1 and v2 must never share job folders)
ROOT = os.environ.get("CLIPPER_WORK") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")

HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
         "twitch.tv", "www.twitch.tv", "clips.twitch.tv", "kick.com", "www.kick.com",
         "vimeo.com", "player.vimeo.com")
MAX_BYTES = 4 * 1024 ** 3          # a link is never allowed to fill the disk unnoticed
PROXY_H = 720                      # the preview copy is never taller than this


class Bad(SystemExit):
    """A message meant for the person using the editor, not a traceback."""


def allowed(url):
    """Only a plain http(s) video link from a known host, with no credentials in it."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return (u.scheme in ("http", "https") and not u.username and not u.password
            and host in HOSTS and u.port in (None, 80, 443))


def link_source(src, video):
    """Point work/<job>/source.ext at the original without copying it.

    A symlink where the system allows one (Linux, macOS), a hard link on the same drive, and on a
    Windows machine without symlink rights simply the original path itself.
    """
    src = os.path.abspath(src)
    for how in (os.symlink, os.link):
        try:
            how(src, video); return video
        except (OSError, NotImplementedError, AttributeError):
            pass
    return src


def slug(s, n=48):
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return (s[:n] or "clip").strip("_")


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                       capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        raise Bad("this file is not a video ffmpeg can read - choose a playable video file")
    d = json.loads(r.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    if v is None:
        raise Bad("this file has no video track - choose a playable video file")
    rate = v.get("r_frame_rate") or "0/1"
    try:
        num, den = rate.split("/"); fps = (float(num) / float(den)) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {"w": int(v.get("width") or 0), "h": int(v.get("height") or 0), "fps": fps,
            "dur": float(d.get("format", {}).get("duration") or 0), "audio": a is not None,
            "vcodec": v.get("codec_name") or "", "pix_fmt": v.get("pix_fmt") or "",
            "bitrate": int(d.get("format", {}).get("bit_rate") or 0)}


def browser_safe(path, info):
    """True only for files Chrome plays as they are: 8-bit H.264 in mp4, small enough to stream."""
    return (info["vcodec"] == "h264" and info["pix_fmt"] in ("yuv420p", "yuvj420p")
            and info["h"] <= 1080 and os.path.splitext(path)[1].lower() in (".mp4", ".m4v")
            and (info["bitrate"] == 0 or info["bitrate"] <= 12_000_000))


def say(msg, pct=None):
    print(("%s %d%%" % (msg, pct)) if pct is not None else msg, flush=True)


def build_proxy(src, out, dur):
    """H.264 / yuv420p / AAC / faststart, <=720p, with a percentage on stdout as it encodes."""
    tmp = out + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", "-i", src,
           "-map", "0:v:0", "-map", "0:a:0?",
           "-vf", "scale=-2:'min(%d,ih)':flags=fast_bilinear" % PROXY_H,
           # capped bitrate as well as CRF: noisy footage at CRF 24 alone produced a preview copy
           # bigger than the source, which is the opposite of the point
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p",
           "-maxrate", "2500k", "-bufsize", "5000k",
           "-profile:v", "high", "-level", "4.0",
           "-c:a", "aac", "-b:a", "128k", "-ac", "2",
           "-movflags", "+faststart", "-f", "mp4", tmp]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    last = -1
    try:
        for line in p.stdout:
            line = line.strip()
            m = re.match(r"out_time_ms=(\d+)", line)
            if m and dur > 0:
                pct = min(99, int(100 * (int(m.group(1)) / 1e6) / dur))
                if pct != last and pct % 2 == 0:
                    last = pct; say("preparing a preview copy", pct)
            elif line and not re.match(r"[a-z0-9_]+=", line):
                say(line[:200])
        p.wait()
    except BaseException:
        p.kill(); p.wait()
        if os.path.exists(tmp): os.remove(tmp)
        raise
    if p.returncode or not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
        if os.path.exists(tmp): os.remove(tmp)
        raise Bad("could not build a preview copy of this file (ffmpeg failed)")
    os.replace(tmp, out)                    # only a finished file ever appears as proxy.mp4
    say("preview copy ready", 100)
    return out


def extract_audio(meta, on_pct=None):
    """The 16 kHz mono wav the transcriber and the moment scorer read.

    Deliberately NOT part of ingest(): on a multi-GB source this takes minutes, and doing it before
    the page ever got a video is what made a big file look like a hang. The editor runs it in the
    background; the command-line pipeline calls it straight after ingest.
    """
    src = meta.get("video")
    wav = meta.get("wav") or os.path.join(meta["dir"], "audio.wav")
    if os.path.exists(wav) and os.path.getsize(wav) > 1024:
        return wav
    if not src or not os.path.exists(src):
        raise Bad("this job has no source video")
    dur = meta.get("dur") or 0
    tmp = wav + ".part"
    p = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
                          "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", tmp],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    last = -1
    try:
        for line in p.stdout:
            m = re.match(r"out_time_ms=(\d+)", line.strip())
            if m and dur > 0:
                pct = min(99, int(100 * (int(m.group(1)) / 1e6) / dur))
                if pct != last and pct % 5 == 0:
                    last = pct
                    (on_pct or (lambda v: say("extracting audio", v)))(pct)
        p.wait()
    except BaseException:
        p.kill(); p.wait()
        if os.path.exists(tmp): os.remove(tmp)
        raise
    if p.returncode or not os.path.exists(tmp):
        if os.path.exists(tmp): os.remove(tmp)
        raise Bad("could not extract audio from this file")
    os.replace(tmp, wav)
    return wav


def cached_url_job(url):
    """The same link is never downloaded twice: an earlier job for this URL is reused."""
    if not os.path.isdir(ROOT):
        return None
    for name in sorted(os.listdir(ROOT)):
        m = os.path.join(ROOT, name, "meta.json")
        try:
            meta = json.load(open(m))
        except (OSError, ValueError):
            continue
        if meta.get("url") == url or meta.get("src") == url:
            v = meta.get("video")
            if v and os.path.exists(v):
                return meta
    return None


def download(url, d, keep_res=720):
    """yt-dlp with a height cap, a size cap, timeouts and live progress. The folder is the caller's
    to delete if this raises."""
    if not shutil.which("yt-dlp"):
        raise Bad("yt-dlp is not installed - install it or pick a local file")
    # H.264 first: YouTube's own "best mp4" is often AV1 10-bit, which the browser cannot decode and
    # which would cost a full re-encode here. avc1 at the same height plays as it is.
    fmt = ("bestvideo[height<=%(r)d][vcodec^=avc1]+bestaudio[ext=m4a]/"
           "best[height<=%(r)d][vcodec^=avc1][ext=mp4]/"
           "best[height<=%(r)d][ext=mp4]/best[height<=%(r)d]" % {"r": keep_res})
    cmd = ["yt-dlp", "--newline", "--no-playlist", "--no-warnings",
           "-f", fmt, "--merge-output-format", "mp4",
           "--max-filesize", str(MAX_BYTES), "--socket-timeout", "15",
           "--retries", "2", "--fragment-retries", "2", "--extractor-retries", "1",
           "--write-info-json", "--write-auto-subs", "--sub-lang", "en.*", "--convert-subs", "srt",
           "-o", os.path.join(d, "source.%(ext)s"), url]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail = []
    for line in p.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        tail[:] = tail[-12:]
        m = re.search(r"\[download\]\s+([\d.]+)% of\s+~?\s*([\d.]+\w+)", line)
        if m:
            say("downloading %sp: %s of %s" % (keep_res, m.group(1) + "%", m.group(2)),
                min(99, int(float(m.group(1)))))
        else:
            say(line[:200])
    p.wait()
    got = [f for f in sorted(os.listdir(d)) if f.startswith("source.")
           and os.path.splitext(f)[1].lower() in (".mp4", ".mkv", ".webm", ".m4v", ".mov")]
    if p.returncode or not got:
        raise Bad("the link could not be downloaded (%s)" % ("; ".join(tail[-3:]) or "yt-dlp failed"))
    return os.path.join(d, got[0])


def ingest(src, name=None, keep_res=720, want_proxy=True):
    os.makedirs(ROOT, exist_ok=True)
    is_url = src.startswith("http://") or src.startswith("https://")
    if is_url:
        if not allowed(src):
            raise Bad("paste a full YouTube, Twitch, Kick or Vimeo video link")
        cached = cached_url_job(src)
        if cached:
            say("job %s  (already imported - reusing it)" % cached["job"])
            return cached
    else:
        src = os.path.abspath(os.path.expanduser(src))
        if not os.path.isfile(src):
            raise Bad("no such file: %s" % src)

    job = slug(name or (("yt_" + src[-11:]) if is_url else os.path.splitext(os.path.basename(src))[0]))
    d = os.path.join(ROOT, job)
    fresh = not os.path.isdir(d)
    os.makedirs(d, exist_ok=True)
    meta = {"job": job, "dir": d, "src": src, "ingested": time.time()}

    try:
        if is_url:
            have = [f for f in sorted(os.listdir(d)) if f.startswith("source.")
                    and os.path.splitext(f)[1].lower() in (".mp4", ".mkv", ".webm", ".m4v", ".mov")]
            video = os.path.join(d, have[0]) if have else download(src, d, keep_res)
            meta["url"] = src
        else:
            # the original is never copied or re-containered: it is linked under its own extension,
            # so nothing downstream is fooled into thinking a .mkv is an mp4
            video = os.path.join(d, "source" + os.path.splitext(src)[1].lower())
            if os.path.islink(video) and not os.path.exists(video):
                os.remove(video)                # the original moved: link it again
            if not os.path.lexists(video):
                video = link_source(src, video)
        info = probe(video)                 # rejects an unplayable file before anything else is built
        meta.update(info)

        info_json = os.path.join(d, "source.info.json")
        if os.path.exists(info_json):
            j = json.load(open(info_json))
            meta["title"] = j.get("title"); meta["chapters"] = j.get("chapters") or []
            meta["uploader"] = j.get("uploader"); meta["url"] = j.get("webpage_url") or meta.get("url")
        else:
            meta["title"] = os.path.splitext(os.path.basename(src if not is_url else video))[0]
            meta["chapters"] = []

        meta["video"] = video
        meta["wav"] = os.path.join(d, "audio.wav")      # built in the background by the server
        proxy = os.path.join(d, "proxy.mp4")
        if browser_safe(video, info):
            meta["proxy"] = None
            say("source plays in the browser as it is - no preview copy needed")
        elif want_proxy:
            if not os.path.exists(proxy):
                say("%s %s %dp - building a browser-safe preview copy"
                    % (info["vcodec"] or "?", info["pix_fmt"] or "?", info["h"]))
                build_proxy(video, proxy, info["dur"])
            meta["proxy"] = proxy
        else:
            meta["proxy"] = proxy if os.path.exists(proxy) else None

        json.dump(meta, open(os.path.join(d, "meta.json"), "w"), indent=2)
    except BaseException:
        # a half-finished import must not be left behind as a job, and must never fill the disk
        if fresh and os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        raise

    say("job %s  %dx%d  %.1f min  audio=%s  proxy=%s  chapters=%d"
        % (job, meta["w"], meta["h"], meta["dur"] / 60, meta["audio"],
           "yes" if meta.get("proxy") else "no", len(meta["chapters"])))
    return meta


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("src", help="local video file or a video URL")
    p.add_argument("--name")
    p.add_argument("--res", type=int, default=720)
    p.add_argument("--no-proxy", action="store_true")
    a = p.parse_args()
    try:
        ingest(a.src, a.name, a.res, not a.no_proxy)
    except Bad as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
