#!/usr/bin/env python3
"""Stage 2 - transcribe: faster-whisper word timestamps + VAD, cached as transcript.json.

Falls back from GPU float16 to GPU int8 to CPU int8 on its own, so a missing cuDNN never stops a run.
Gameplay with no commentary comes back with an empty word list; the moment scorer then uses audio energy
and scene cuts instead, so the pipeline still works on silent footage.
"""
import argparse, json, os, sys, time

MODELS = ("large-v3", "medium", "small", "base")


def _cuda_libs():
    """ctranslate2 dlopens cuBLAS/cuDNN by SONAME. Setting LD_LIBRARY_PATH after start is too late, so
    preload the pip-installed CUDA 12 libraries by full path: dlopen then finds them already loaded."""
    import ctypes, glob
    pats = ["nvidia/cublas/lib/libcublas.so.12", "nvidia/cublas/lib/libcublasLt.so.12",
            "nvidia/cudnn/lib/libcudnn*.so.9", "nvidia/cuda_nvrtc/lib/libnvrtc.so.12*"]
    seen = []
    for base in sys.path:
        for pat in pats:
            for f in sorted(glob.glob(os.path.join(base, pat))):
                try:
                    ctypes.CDLL(f, mode=ctypes.RTLD_GLOBAL); seen.append(os.path.basename(f))
                except OSError:
                    pass
    if seen:
        print("preloaded %d CUDA libs" % len(seen), file=sys.stderr)


def load(size, prefer_gpu=True):
    _cuda_libs()
    from faster_whisper import WhisperModel
    tries = ([("cuda", "float16"), ("cuda", "int8_float16")] if prefer_gpu else []) + [("cpu", "int8")]
    last = None
    for dev, ct in tries:
        try:
            m = WhisperModel(size, device=dev, compute_type=ct)
            print("whisper %s on %s/%s" % (size, dev, ct), file=sys.stderr)
            return m, dev
        except Exception as e:
            last = e
            print("  %s/%s unavailable: %s" % (dev, ct, str(e).split("\n")[0][:120]), file=sys.stderr)
    raise SystemExit("no whisper backend: %s" % last)


def load_wav(path):
    """Read the 16 kHz mono wav straight into numpy - faster-whisper's own decoder breaks on PyAV 19."""
    import wave, numpy as np
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2 and w.getframerate() == 16000, "expected 16 kHz 16-bit mono"
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if w.getnchannels() > 1:
        pcm = pcm.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)
    return pcm.astype("float32") / 32768.0


def run(job_dir, size="medium", lang=None, prefer_gpu=True, force=False):
    out = os.path.join(job_dir, "transcript.json")
    if os.path.exists(out) and not force:
        print("transcript cached"); return json.load(open(out))
    meta = json.load(open(os.path.join(job_dir, "meta.json")))
    model, dev = load(size, prefer_gpu)
    t0 = time.time()
    segs, info = model.transcribe(load_wav(meta["wav"]), language=lang, word_timestamps=True,
                                  vad_filter=True, vad_parameters={"min_silence_duration_ms": 400},
                                  beam_size=5, condition_on_previous_text=False)
    words, sentences = [], []
    for s in segs:
        sentences.append({"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
                          "nospeech": round(getattr(s, "no_speech_prob", 0.0), 3)})
        for w in (s.words or []):
            words.append({"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3),
                          "p": round(w.probability, 3)})
        if len(sentences) % 40 == 0:
            print("  %.1f min transcribed (%.0f s elapsed)" % (s.end / 60, time.time() - t0), file=sys.stderr)
    d = {"lang": info.language, "lang_p": round(info.language_probability, 3), "device": dev,
         "model": size, "words": words, "sentences": sentences,
         "speech_sec": round(sum(s["end"] - s["start"] for s in sentences), 1)}
    json.dump(d, open(out, "w"))
    print("transcribed: %d words, %d sentences, %.1f s of speech, %.0f s wall" %
          (len(words), len(sentences), d["speech_sec"], time.time() - t0))
    return d


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("job_dir")
    p.add_argument("--model", default="medium", choices=MODELS)
    p.add_argument("--lang", default=None)
    p.add_argument("--cpu", action="store_true")
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    run(a.job_dir, a.model, a.lang, not a.cpu, a.force)
