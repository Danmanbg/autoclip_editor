# AutoClip Editor

Turn your stream recordings into ready-to-post videos — on your own PC, no
uploads, no subscription.

- **Short clips** — finds the best moments and makes vertical 9:16 clips with
  your facecam or PNGTuber on top, gameplay below, and word-by-word captions.
- **Full stream, cleaned up** — the whole VOD exactly as it was, with swearing
  bleeped so it stays monetised. The picture is copied, not re-encoded, so a
  two-hour stream takes minutes and loses no quality.
- **Highlight reel** — suggests the best moments, **you pick which go in**, and
  it joins them into one horizontal video with chapters.
- **Titles and descriptions** for every result, built from what you actually
  said on stream.

Works with Twitch VODs, YouTube links, or any video file on your computer.

---

## Quick start (Windows)

You need **Python 3.10+** and **ffmpeg**.

```bash
winget install Gyan.FFmpeg
```

```bash
git clone https://github.com/<you>/autoclip_editor.git
cd autoclip_editor
py -3 -m pip install -r requirements.txt
```

Then double-click **`Start AutoClip Editor.bat`** (or run `py -3 run.py`). The
app opens in your browser at <http://127.0.0.1:8420>. Close the terminal window
to stop it.

Speech models (for captions and bleeping) download automatically the first time
they are needed. The AI title ideas are optional — see [below](#titles-and-descriptions).

---

## How to use it

1. **Your stream** — paste a Twitch VOD or YouTube link, pick a file with
   *Browse…*, or drop a video onto the box. For a long VOD you can fetch just
   part of it.
2. **What do you want to make?** — short clips, the full stream cleaned up, or a
   highlight reel.
3. **Layout** (clips) — drag the box onto your facecam or PNGTuber. The preview
   on the right is rendered by the same ffmpeg pipeline as the final video, so
   what you see is what you get.
4. **Which moments / where to look** — how many clips and how long, and which
   part of the video to search (drag the two handles to skip an intro or outro).
5. **Captions** (clips) — style, font, colours, position.
6. **Keep it monetised** — bleeping on or off, and your own word lists.

Finished videos appear at the bottom as they render and are saved to `output/`.

### Highlight reel

Set a target length (10–15 minutes by default) and press **Find moments**. It
suggests about twice as much footage as you need, so you have a real choice;
thumbnails appear first, then what you said in each moment. Tick the ones you
want, click a picture to watch that moment, nudge the start or end by two
seconds, or scrub the player and add a moment it missed. The bar turns green
when you are inside the target. **Build** joins them in order with quick fades,
bleeps any swearing, and writes chapters into the MP4, named after your quotes.

### Full stream, cleaned up

The video is stream-copied bit-for-bit; only the audio is rebuilt. Alongside it
you get a report listing every bleep with a timestamp (so you can spot-check
it) and an `.srt` of the whole stream with the swearing starred out, ready to
upload as YouTube captions. Only the first audio track is kept — OBS
multi-track recordings often put the raw mic on track 2, and copying it would
put every swear word back.

---

## How the clip finder works

Five signals are scored across the stream:

- **Loudness**, and **burst** — how much louder than the last ~90 seconds, which
  separates a real reaction from a permanently loud game.
- **Brightness** — high-frequency energy: laughing, shouting, cheering.
- **Voice** — speech activity, so dead air scores low.
- **Chat** — on Twitch VODs, the chat replay's message rate weighted by how
  excited the messages are (`OMEGALUL`, `POG`, all-caps…), shifted 2.5 s
  earlier because chat reacts late.

The best windows are picked in three passes — spread widely across the video
first, then more tightly, then topped up — so you get the number of clips you
asked for, and a long stream does not give you five clips from the same five
minutes. Picks from the last pass are marked `EXTRA`. Each clip is placed so
the peak lands about 45% in (some build-up first), and cut points snap to quiet
moments so clips do not start mid-word.

---

## Keeping it monetised

- Bleeps (or mutes) the words that cost you ads; mild words such as *damn* or
  *hell* are optional.
- Matches whole words plus real endings (-s, -ing, -ed, -head…), never
  substrings, so *assassin*, *cocktail*, *classic* and *Scunthorpe* are safe.
- Swearing is also starred out of burned-in captions, `.srt` files, titles and
  file names — platforms read those too.
- Add your own words, or protect words your game uses innocently. A **Try it**
  box shows exactly what would be bleeped as you type.

It catches what the speech model hears; mumbled or shouted-over words can slip
through, which is what the report is for.

---

## Titles and descriptions

Every result comes with suggestions and copy buttons:

- **From your own words** — for each moment it finds the punchline line (near
  the loudest instant, exclaimed, the right length, not filler):
  *"Everyone said Xen is bad. Everyone was right."* Clips are named after it.
- **AI ideas** *(optional)* — a small AI running locally writes
  *"Half-Life but…"*-style titles. It is small, so some will miss; press
  **Suggest more** for a new batch. To turn it on (once, ~1.1 GB):

  ```bash
  py -3 -m pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
  ```

  ```bash
  py -3 run.py --get-ai-model
  ```

- **Description** — assembled from facts, never invented: your best moments as
  timestamps (YouTube turns them into chapters), the game and your hashtags. The
  game is guessed from the stream title; change it and your hashtags in the panel.

---

## Speed

Measured on a Ryzen 5 7520U laptop (4 cores, no dedicated GPU):

| Task | Time |
| --- | --- |
| Finding moments | a few seconds per 10 min of stream |
| One 30 s vertical clip, captioned | ~45 s |
| Full 2-hour stream, cleaned (`base` model) | ~16 min of listening + seconds to write |
| 12-minute highlight reel | ~7 min |

Faster machines are proportionally quicker. A GPU encoder (AMD/NVIDIA/Intel) can
be picked in *Output & presets*.

---

## What goes online

Everything is processed locally. The app only connects out to:

- download the video you paste (via [yt-dlp](https://github.com/yt-dlp/yt-dlp)),
- read a Twitch VOD's chat replay, through Twitch's public but undocumented web
  API — it can break if Twitch changes it, and detection then carries on with
  audio alone,
- fetch speech/AI models from Hugging Face the first time.

Please only download and repost videos you have the rights to.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "ffmpeg missing" | `winget install Gyan.FFmpeg`, then open a new terminal. Or set `FFMPEG_DIR` to the folder with `ffmpeg.exe`. |
| Captions are blank | Your ffmpeg needs libass — `ffmpeg -version` should show `--enable-libass` (the Gyan full build does). |
| A Twitch VOD will not download | Sub-only or deleted VODs cannot be fetched. Download it yourself and pick the file. |
| It is slow | Use the `base` speech model, 720p download quality, or a GPU encoder. |
| Disk filling up | *Clear cached downloads* in *Output & presets*. Finished videos are never touched. |

Developed and tested on Windows 11. Most of it is plain Python + ffmpeg and
should run elsewhere, but the file picker and a few paths are Windows-first.

---

## Project layout

```
run.py                 launcher (--port, --no-browser, --get-ai-model)
app/
  server.py            Flask routes - the JSON API the page talks to
  pipeline.py          clips and clean-stream jobs
  reel.py              highlight reel: find moments, transcript cache, build + chapters
  detect.py            audio analysis, scoring, clip selection
  chat.py              Twitch chat replay sampling
  source.py            yt-dlp / local file handling
  transcribe.py        faster-whisper word timings, long-form chunking
  censor.py            profanity matching and the bleep filter
  titles.py            title and description suggestions, local AI ideas
  captions.py          phrase grouping and .ass subtitle generation
  render.py            the ffmpeg filter graphs
  downloads.py         reliable Hugging Face downloads
  ffmpeg_tools.py, fonts.py, settings.py, jobs.py, native.py, paths.py
  web/                 the interface (plain HTML/CSS/JS, no build step)
models/                downloaded speech/AI models (not committed)
work/, output/         caches and your videos (not committed)
```

---

## Licence

MIT — see [LICENSE](LICENSE).

Models are downloaded from their publishers, not shipped here:
[faster-whisper](https://huggingface.co/Systran) conversions of OpenAI Whisper
(MIT) and [Qwen2.5-1.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF)
(Apache-2.0). ffmpeg is installed separately under its own licence.

Not affiliated with Twitch, YouTube, or any streaming or clipping service.
