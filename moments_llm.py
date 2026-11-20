#!/usr/bin/env python3
"""Optional smarter moment picking, at zero Claude cost.

Sends a compact "mm:ss  line" digest of the transcript to a free local or hosted language model
(ollama, opencode, or your own script set as "llm_script" in settings.json) and asks for the best
self-contained clips as JSON. Falls back to the rule-based scorer in moments.py if no model answers,
so a run never depends on one.

  python3 moments_llm.py work/<job> --count 8
"""
import argparse, json, os, re, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
CHUNK = 3600                                     # longer asks are split by some backends, costing round trips


def llm_script():
    """Optional user script: called as  <script> chat --file PROMPT --out ANSWER --new [--model M]."""
    p = os.environ.get("CLIPPER_LLM_SCRIPT")
    if not p:
        try:
            sys.path.insert(0, HERE); import config
            p = config.load().get("llm_script")
        except Exception:
            p = None
    return os.path.expanduser(p) if p else None


def digest(sentences, every=1):
    out = []
    for s in sentences[::every]:
        t = int(s["start"])
        out.append("%02d:%02d %s" % (t // 60, t % 60, s["text"].strip()))
    return out


def build_prompt(lines, count, title, minlen, maxlen):
    return (
        "You pick viral short-form clips out of a long video transcript.\n"
        "VIDEO: %s\n"
        "Each line below is 'mm:ss text'.\n\n"
        "Return ONLY a JSON array of %d objects, no prose, no code fence:\n"
        '[{"start":"mm:ss","end":"mm:ss","title":"<=60 chars","hook":"<=40 chars burned on screen","why":"one short reason"}]\n'
        "Rules: each clip %d-%d seconds; must make sense with no other context; start on a strong first line "
        "(a claim, a question, a number, a surprise), end on a payoff, never mid-sentence; no two clips overlap; "
        "spread them across the whole video.\n\nTRANSCRIPT:\n%s\n"
    ) % (title or "untitled", count, minlen, maxlen, "\n".join(lines))


def grab_json(txt):
    m = re.search(r"\[\s*{.*}\s*\]", txt or "", re.S)
    if not m: return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print("  broken JSON: %s" % e, file=sys.stderr); return None


def ask_ollama(prompt, model, timeout=900):
    """Straight to the local ollama HTTP API. No account, no network, no browser - the only brain
    that works with everything else switched off. The opencode CLI wraps the same models but
    printed nothing to stdout when run non-interactively, so we skip it."""
    import urllib.request
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.3, "num_ctx": 32768}}).encode()
    req = urllib.request.Request("http://127.0.0.1:11434/api/generate", body,
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return grab_json(json.load(r).get("response", ""))
    except Exception as e:
        print("  ollama %s unavailable: %s" % (model, e), file=sys.stderr); return None


def ask_opencode(prompt, model, timeout=900):
    """OpenCode in non-interactive mode. ollama/* runs on this machine, so it works with no
    network and no account at all; opencode/*-free is the hosted free tier."""
    try:
        r = subprocess.run(["opencode", "run", "--model", model, "--format", "default", prompt],
                           timeout=timeout, capture_output=True, text=True)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print("  opencode %s unavailable: %s" % (model, e), file=sys.stderr); return None
    return grab_json(r.stdout)


def ask_script(prompt, model=None, timeout=900):
    script = llm_script()
    if not script or not os.path.exists(script): return None     # not configured on this machine
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(prompt); p = f.name
    out = p + ".out"
    cmd = [sys.executable, script, "chat", "--file", p, "--out", out, "--new"]
    if model: cmd += ["--model", model]
    try:
        subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        print("llm script timed out", file=sys.stderr); return None
    if not os.path.exists(out): return None
    return grab_json(open(out).read())


# Tried in order, each one free. The local ollama model needs nothing but this machine; the hosted
# free tier needs an account; the script is whatever you point "llm_script" at. The rule-based scorer in moments.py is the floor,
# so a missing brain degrades picking quality and never breaks a run.
BRAINS = [("ollama",   "qwen2.5:14b-ctx32k"),
          ("ollama",   "qwen2.5:14b"),
          ("opencode", "opencode/beam-free"),
          ("script",   None)]


def ask(lines, count, title, minlen, maxlen, model=None, timeout=900, chain=None):
    prompt = build_prompt(lines, count, title, minlen, maxlen)
    chain = chain or ([("script", model)] if model else BRAINS)
    for kind, m in chain:
        print("brain: %s %s" % (kind, m or "default"), file=sys.stderr)
        fn = {"ollama": ask_ollama, "opencode": ask_opencode}.get(kind, ask_script)
        r = fn(prompt, m, timeout)
        if r: return r
    return None


def to_sec(v):
    if isinstance(v, (int, float)): return float(v)
    p = [float(x) for x in str(v).split(":")]
    return p[0] * 60 + p[1] if len(p) == 2 else (p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0])


def run(job_dir, count=8, minlen=18, maxlen=50, model=None, every=1):
    meta = json.load(open(os.path.join(job_dir, "meta.json")))
    tp = os.path.join(job_dir, "transcript.json")
    if not os.path.exists(tp):
        sys.exit("no transcript: this path needs speech. Use moments.py for silent footage.")
    tr = json.load(open(tp))
    lines = digest(tr["sentences"], every)
    while sum(len(l) + 1 for l in lines) > 24000:        # keep the ask small enough for any backend
        every += 1; lines = digest(tr["sentences"], every)
    print("asking a language model about %d transcript lines (every %d)" % (len(lines), every))
    got = ask(lines, count, meta.get("title"), minlen, maxlen, model)
    if not got:
        print("falling back to the local scorer"); 
        sys.path.insert(0, HERE); import moments
        return moments.find(job_dir, count, minlen, maxlen, force=True)

    sents = tr["sentences"]
    clips = []
    for i, c in enumerate(got, 1):
        try:
            s, e = to_sec(c["start"]), to_sec(c["end"])
        except Exception:
            continue
        if e - s < 5 or e > meta["dur"]: continue
        s = min((x["start"] for x in sents if abs(x["start"] - s) <= 3.0), key=lambda x: abs(x - s), default=s)
        e = min((x["end"] for x in sents if abs(x["end"] - e) <= 3.0), key=lambda x: abs(x - e), default=e)
        if e - s > maxlen + 10: e = s + maxlen
        clips.append({"n": len(clips) + 1, "start": round(s, 2), "end": round(e, 2), "dur": round(e - s, 2),
                      "title": (c.get("title") or "")[:70], "hook": (c.get("hook") or "")[:40],
                      "why": [c.get("why", "model pick")], "score": 9.0,
                      "text": " ".join(x["text"] for x in sents if x["start"] >= s and x["end"] <= e)[:400]})
    clips.sort(key=lambda c: c["start"])
    for i, c in enumerate(clips, 1): c["n"] = i
    json.dump({"clips": clips, "cuts": []}, open(os.path.join(job_dir, "moments.json"), "w"), indent=2)
    for c in clips:
        print("%2d  %7.1f-%7.1f  %4.1f s  %s | %s" % (c["n"], c["start"], c["end"], c["dur"], c["hook"], c["title"][:50]))
    return {"clips": clips}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("job_dir"); p.add_argument("--count", type=int, default=8)
    p.add_argument("--min", type=float, default=18); p.add_argument("--max", type=float, default=50)
    p.add_argument("--model", default=None); p.add_argument("--every", type=int, default=1)
    a = p.parse_args()
    run(a.job_dir, a.count, a.min, a.max, a.model, a.every)
