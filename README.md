# Claude Video Watcher

Turns a competitor YouTube video into something Claude can actually study: one frame per shot plus
dense uniform fill, timestamped contact sheets, a clean transcript, pacing data and an audio energy
curve — then a fixed-schema `NOTES.md` covering the hook beat by beat, an animation inventory with
credit costs, on-screen text, structure, packaging and a steal list.

Transcripts alone are useless for this. What matters is visual: what animation appears at what
moment, what the on-screen text says, how the hook is built shot by shot. Claude cannot ingest
video, but it can read a large number of images. This converts one into the other.

## Invoke it

Just ask. The skill fires on plain requests — "analyze this video", "break down this video's hook",
"what can I steal from this" — or on a pasted YouTube URL in any editing context.

To run the pipeline directly, from the project root:

```
py ".claude\skills\watch-video\scripts\watch_video.py" "<URL>"
```

Useful flags: `--hook-only` (first 60s), `--start` / `--end` (a segment), `--slug NAME`,
`--threshold N`, `--no-download`, `--keep-source`.

Resumable — existing frames, sheets and data are reused, so re-running is cheap. A completed video
re-runs in well under a second with no network.

## Where output lands

```
research\
  INDEX.md                  one row per video, sorted by views ÷ subs
  <video-slug>\
    NOTES.md                the deliverable
    frames\                 one JPEG per shot + fill frames (gitignored)
    sheets\                 3x3 contact sheets, 1568px, timestamps burned in (gitignored)
    data\                   transcript, meta, pacing, cuts, audio, thumbnail
```

The source video is deleted automatically once frames and audio both succeed. Frames, sheets, CSVs
and `NOTES.md` are what gets kept; the source is disposable and re-downloadable. `source.*`,
`frames\` and `sheets\` are gitignored — `data\` and `NOTES.md` are tracked.

## Requirements

yt-dlp, ffmpeg, Python 3, Node (as yt-dlp's JS runtime). All local. No MCP servers, no APIs, no paid
services, no Adobe.

## Known limits

Read these before trusting a number.

- **No audio content.** It measures loudness, not sound. It cannot hear music, tone, voice quality
  or what the music is doing — only where energy rises and falls.
- **No motion.** Frames are stills. Easing, speed ramps, transition style and camera movement are
  invisible except where consecutive frames imply them.
- **Screen-recording bias.** Cuts inside screen recordings barely change the pixels — same window
  chrome, same background — so they score below the detection threshold and get under-counted. On
  screen-recording-heavy videos the shot rate is reported as a **range**, not a figure, and the
  lower number is a floor rather than a measurement.
- **SSIM cannot adjudicate cuts.** Fast camera motion depresses SSIM exactly like a hard cut, so it
  ranks continuous moving shots as the most definite cuts. Disputed timestamps are settled by
  extracting before/after frame pairs and looking at them.
- **Cut detection is blind to slow morphing graphics.** This is why uniform fill exists, and in
  practice the fill frames carry more of the design work than the detected cuts do. On the first
  video analyzed, all four flagged "dead zones" turned out to be the densest design stretches in
  the piece, and a 5s fill caught a sponsor card that a 10s fill had stepped straight over.
- **One video per session.** A full analysis is roughly 150–250K tokens of images. Run each video in
  its own fresh session.

## Documentation

- `VIDEO-ANALYSIS-METHOD.md` — the portable method: what it does, every hard-won lesson, the full
  output schema, the build hierarchy and credit rates. No code, no machine paths. Someone with no
  access to this machine could rebuild the whole thing from it.
- `.claude\skills\watch-video\SKILL.md` — the operational instructions Claude follows.
