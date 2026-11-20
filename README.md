# Clipper Studio

Turn a long video (a local file or a YouTube / Twitch / Kick / Vimeo link) into short 9:16 clips
with captions, auto-tracking crop, split / stacked layouts and a hook line. Everything runs on your
own machine in a local web page. No account and no upload to any server.

## 1. Install (one click)

You need **Python 3.9 or newer** ([python.org](https://www.python.org/downloads/)). On Windows,
tick "Add python.exe to PATH" in the installer.

| System | Install | Start |
|---|---|---|
| Windows | double-click `install.bat` | double-click `start.bat` |
| macOS / Linux | `./install.sh` | `./start.sh` |

The installer keeps everything in a private `.venv` folder next to the tool. It does not install
anything system-wide, and you can run it again safely because it only adds what is missing.

- **torch:** you get the NVIDIA GPU build when an NVIDIA card is found, otherwise the CPU build.
  Add `--cpu` to force the CPU build.
- **ffmpeg:** your system ffmpeg is used if it exists. If not, a private copy is downloaded once.
- **Models:** the speech model (whisper medium, about 1.5 GB) and the person detector (yolov8n) are
  downloaded once. Add `--no-models` to skip this; they then download on first use.
- **Self-check:** the installer ends with a self-check and tells you exactly what is missing.

Ubuntu/Debian only: if the installer says it cannot create `.venv`, run
`sudo apt install python3-venv` and run the installer again.

## 2. First start

`start` opens the page in your browser, at `http://127.0.0.1:8804` or the next free port. On the
first start you are asked two things:

- **Projects folder:** each imported video gets its own sub-folder here (cuts, transcript, exports).
  The default is `work/` next to the tool. Any folder you can write to is fine, including another
  drive.
- **Video folders:** the folders listed under "Recent videos". The defaults are your Downloads,
  Videos/Movies and Desktop folders, as your system names them. This includes OneDrive folders on
  Windows and renamed XDG folders on Linux.

You can change both later under **Settings**. They are saved in `settings.json`, which belongs to
your copy only and is never committed.

## 3. Importing videos

- **Local file:** pick it from "Recent videos", the Desktop chip, or "Browse" (any folder on any
  drive). The original file is linked, not copied.
- **Upload…** sends a file into the projects folder. Use it when the video is on another drive or
  you want a copy.
- **Link:** paste a YouTube / Twitch / Kick / Vimeo URL.
  - It downloads at up to **720p**, preferring H.264, so the browser can play it without a
    re-encode.
  - Downloads are capped at **4 GB**, with timeouts and retries.
  - A live "downloading 720p: 41% of 180 MiB" line shows progress.
  - The same link is not downloaded twice.
  - If a download fails, the half-finished folder is removed.

**Big or unusual files (HEVC, AV1, 10-bit, ProRes, .mkv, .mov):** the editor plays a small
browser-safe preview copy (H.264, at most 720p), but the final render always uses the original
file. Sound extraction for the transcript runs in the background with a percentage, so the page
never freezes on a large file.

> A link must finish downloading before you can edit it. Long streams take time: a 2-hour video at
> 720p is roughly 1-2 GB.

## 4. Making clips

1. **Transcribe** creates the words with timings, which captions and moment-finding need.
2. **Find moments** suggests clips. You can also drag on the clips lane to make one yourself.
3. Choose a 9:16 layout for each clip:
   - **stack:** face cam on top, game or screen below.
   - **split:** two boxes you place.
   - **full one box:** one crop box you place.
   - **full auto-track:** the crop follows the person. "Pick subject" chooses who to follow.
4. Choose a caption style, colour and words per line. Drag the captions to place them, and click a
   word to fix it.
5. **Render** one clip or all of them: 720p/1080p/1440p, 30/60 fps. Every export is checked with
   `validate.py`.

## 5. Command line (optional)

```bash
.venv/bin/python ingest.py "https://youtu.be/VIDEO" --name myjob --res 720
.venv/bin/python clip.py work/myjob --count 5 --min 20 --max 45 --layout auto --style pop
```

Useful `clip.py` flags: `--lang hi`, `--cpu`, `--no-caps`, `--cut-silence`, `--cut-filler`,
`--hook auto`, `--cta "Follow for more"`, `--only 2`, `--redo`. Run with `--help` for all of them.
On Windows, use `.venv\Scripts\python` instead of `.venv/bin/python`.

## 6. Smarter moment picking (optional)

`moments_llm.py` tries a local LLM first and falls back to the built-in rule-based picker:

1. [Ollama](https://ollama.com) with `qwen2.5:14b`
2. `opencode`, if it is installed
3. Your own script, set as `"llm_script"` in `settings.json` or the `CLIPPER_LLM_SCRIPT`
   environment variable. It is called as `<script> chat --file PROMPT --out ANSWER --new`.

None of these are needed: without them the rule-based picker is used.

## Known limits

- Captions support Latin and Devanagari (Hindi) through the bundled fonts. The hook/CTA text line
  supports Latin only for now.
- This release has been tested on Linux. Windows and macOS installers are included but have had
  less testing, so please open an issue with the self-check output if something fails.

## License

MIT. See `LICENSE`. The bundled fonts keep their own licences (see `fonts/`).
