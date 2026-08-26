# Claude Video Watcher

Gives Claude a video input. It extracts one frame per shot plus dense uniform fill, builds
timestamped contact sheets, a clean transcript, pacing data and an audio energy curve — then
actually reads the frames.

Claude cannot ingest video, but it can read a large number of images. This converts one into the
other. That matters because **a transcript is blind to everything on screen**: the diagram, the
code, the terminal output, the settings panel, the chart, the title card, the price. Any tool that
summarizes a video from captions alone is guessing about all of it.

**No API keys. No accounts. Nothing to pay for.** yt-dlp and ffmpeg do the work locally.

## Three modes — it asks which one you want

Before it downloads anything, the skill asks what kind of analysis you want and how deep to go.
The extraction pipeline is identical in all three; only the write-up differs.

| Mode | Produces | Use it for |
|---|---|---|
| **Creator breakdown** | `NOTES.md` — fixed 10-section schema | How the video was *made*: hook map beat by beat, animation inventory with credit costs, pacing, composition mix, packaging, a steal list |
| **Content brief** | `BRIEF.md` | What the video *teaches*: the argument, the reproducible steps, and every command, diagram and figure read straight off the screen |
| **Specific question** | An answer in chat | "What happens at 4:20", "does it cover X", "what tool is that on screen" |

The creator breakdown is the one built for competitive research on a YouTube channel. **If you just
want to understand a video, pick the content brief** — it is the mode that puts the frames to work
on the content itself, and it will catch anything performed on screen but never said aloud.

## Setup

```bash
py ".claude\skills\watch-video\scripts\watch_video.py" --check
```

That reports every dependency and the exact command to install whatever is missing. It should
print `ready.` On macOS use `python3` instead of `py`.

You need **ffmpeg**, **ffprobe**, **yt-dlp** and **Node** (yt-dlp uses it to solve YouTube's JS
challenge). Either yt-dlp install works — the pip module or the standalone binary:

| | Windows | macOS |
|---|---|---|
| ffmpeg | `winget install Gyan.FFmpeg` | `brew install ffmpeg` |
| yt-dlp | `pip install -U yt-dlp` | `brew install yt-dlp` |
| Node | `winget install OpenJS.NodeJS.LTS` | `brew install node` |

**Windows and macOS are supported. Linux is untested** — it will probably work, but the font
lookup for contact-sheet labels has only been verified on the first two.

## Invoke it

Just ask. The skill fires on plain requests — "analyze this video", "summarize this video",
"what does this teach", "break down this video's hook", "what happens at 4:20" — or on a bare
pasted YouTube URL. It asks which mode and depth you want before doing anything.

To run the pipeline directly, from the repo root:

```bash
py ".claude\skills\watch-video\scripts\watch_video.py" "<URL>"
```

Useful flags: `--hook-only` (first 60s), `--start 2:15 --end 2:45` (a segment, in `MM:SS`),
`--depth deep` (see below), `--slug NAME`, `--keep-source`, `--font PATH`, `--patterns`.

### Depth: more frames, same measurement

`--depth` controls how many frames are **extracted**. It never touches the detection threshold
or the merge window, so the reported shots-per-minute is **identical at every depth** — the
pacing figure describes the video, never your setting. Verified on one 60s window: all three
depths returned `merged 27 (27.0/min)` while frames went 27 → 35 → 57.

| | fill interval | fills gaps over | recovers sub-second cuts |
|---|---|---|---|
| `standard` (default) | 5s | 15s | no |
| `deep` | 3s | 8s | yes |
| `max` | 1s | 2s | yes |

It exists to close two blind spots. The 1.0s merge window stops a camera push-in being counted
as fourteen shots, but it also means **nothing shorter than a second ever gets a frame** — on
one fast-cut video that discarded 184 of 584 detections. And fill only fires inside long gaps,
so high-paced videos, where the detector is blindest, get the least extra coverage.

```bash
py ".claude\skills\watch-video\scripts\watch_video.py" "<URL>" --hook-only --depth max
```

Use `deep` when the question is how something was made; use `max` only on a **segment** —
on a full video it will blow past the frame ceiling. Changing depth invalidates the cache.

Resumable — existing frames, sheets and data are reused, so re-running is cheap. A completed video
re-runs in about two seconds with no network. Re-runs find their folder by **video id**, so a
retitled video still lands in its original folder rather than starting a duplicate.

The cache invalidates itself if `--start`, `--end`, `--threshold` or `--depth` differ from the run
that produced it — it says what changed and re-detects.

### When YouTube fights back

Two failures are recoverable, and the error message names the fix:

```bash
# Download stalls or returns 0 bytes (SABR streaming). Try this first.
py ".claude\skills\watch-video\scripts\watch_video.py" "<URL>" --player-client tv,web_safari,mweb
```

```bash
# "Sign in to confirm you're not a bot". Close Chrome first - it locks the cookie DB on Windows.
py ".claude\skills\watch-video\scripts\watch_video.py" "<URL>" --cookies-from-browser chrome
```

`--update` upgrades yt-dlp. It is the only install this tool will ever run, and it is worth trying
before anything else when YouTube behaviour changes.

## Where output lands

Everything goes in `video-research\` at the repo root, so a clone is self-contained and there is
nothing to set up by hand.

```
Claude Video Watcher\
├── .claude\skills\watch-video\     the tool
├── tests\                          py -m unittest discover -s tests
└── video-research\                 GITIGNORED - never committed
    ├── index.json                  source of truth, one record per video
    ├── INDEX.md                    generated from index.json, sorted by multiplier
    ├── PATTERNS-INPUT.md           generated by --patterns
    └── <video-slug>\
        ├── NOTES.md                creator breakdown (mode 1)
        ├── BRIEF.md                content brief (mode 2)
        ├── frames\                 one JPEG per shot + fill frames
        ├── sheets\                 3x3 contact sheets, 1568px, timestamps burned in
        └── data\                   transcript, meta, pacing, cuts, audio, thumbnail
```

**`video-research\` is in `.gitignore` and must stay there.** One analysis is 8–35 MB of frames and
contact sheets. The script checks on every run and warns loudly if that entry ever goes missing.
A `--root` inside `.git\` or `.claude\` is refused outright.

The source video is deleted automatically once frames and audio both succeed. Frames, sheets, CSVs
and `NOTES.md` are what gets kept; the source is disposable and re-downloadable.

## The index, and the two multipliers

`index.json` is the source of truth; `INDEX.md` is generated from it after every run and sorted by
channel multiplier. Don't hand-edit `INDEX.md` — edit the `takeaway` field in `index.json`, which is
the one field the script cannot fill and the one thing a re-run never overwrites.

- **`multiplier_vs_channel`** — views ÷ the channel's median recent views. How far a video beat
  *its own channel*. This is the number that means something.
- **`views_per_sub`** — views ÷ subscribers. Cruder, and often misleading.

They disagree, sometimes completely. In the current corpus one video reads 0.84x by views/sub and
7.1x against its own channel — it is the strongest video in the set and the crude number buries it.

## Cross-video patterns

```bash
py ".claude\skills\watch-video\scripts\watch_video.py" --patterns
```

Builds `PATTERNS-INPUT.md`: the numeric comparison table plus sections 2, 3b, 6, 7 and 9 of every
`NOTES.md` — hook map, reproduction cost, segment structure, composition mix, packaging. Claude
reads it in one pass and writes `PATTERNS.md`.

**Only creator breakdowns feed this.** A folder holding a `BRIEF.md` is listed separately and
excluded, because a content brief has no hook map or reproduction cost to compare.

**It refuses to run under 6 analyses** and stamps a provisional-findings banner under 12. Patterns
from four videos are noise dressed as findings, and the whole value of this tool is that it does not
manufacture confidence.

## Known limits

Read these before trusting a number.

- **No audio content.** It measures loudness, not sound. It cannot hear music, tone, voice quality
  or what the music is doing — only where energy rises and falls. A video with no audio stream
  completes fine, with section 8 marked unavailable.
- **No motion.** Frames are stills. Easing, speed ramps, transition style and camera movement are
  invisible except where consecutive frames imply them.
- **YouTube only.** Local files and other platforms are not supported: the schema's header, packaging
  and index sections all need YouTube metadata.
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
- **Metadata is cached.** The transcript step is skipped when `info.json` already exists, so view
  counts, subscriber counts and therefore both multipliers are frozen at the moment of first
  analysis. That is defensible for an index that is a snapshot, but re-running will not refresh a
  stale figure. Delete `data\*.info.json` to force one.
- **The channel baseline is measured at analysis time, not publish time.** For an old video on a
  channel that has since grown, `multiplier_vs_channel` is understated. It needs 8 usable uploads;
  below that it is `null`, which means "no baseline", not "0x".
- **One video per session, and roughly 40 minutes is the ceiling.** Measured on the longest run to
  date: a **36:07 video produced 469 frames and 53 contact sheets — about 228K image tokens**
  (173K for the survey pass, 55K for 45 microscope frames). That fits, but it is the top of the
  band, and it took about 12 minutes of wall clock and a 170 MB download. Past ~500 frames the
  script prints the projected cost and tells you to use `--hook-only` or `--start`/`--end` instead.
  It never reduces the frame count silently — it states the number and lets you decide.
  Run each video in its own fresh session, and never in the same session as a pattern pass.

## Tests

```bash
py -m unittest discover -s tests
```

No network, runs in about a second. Standard library only - same rule as the pipeline itself.
Every test corresponds to a bug that was actually shipped, so a failure names a real regression.

## Documentation

- `VIDEO-ANALYSIS-METHOD.md` — the portable method: what it does, every hard-won lesson, the full
  output schema, the build hierarchy and credit rates. No code, no machine paths. Someone with no
  access to this machine could rebuild the whole thing from it.
- `.claude\skills\watch-video\SKILL.md` — the operational instructions Claude follows.
