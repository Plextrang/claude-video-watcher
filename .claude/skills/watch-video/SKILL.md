---
name: watch-video
description: Analyze a YouTube video visually — extract one frame per shot plus dense uniform fill, build timestamped contact sheets, transcript, pacing and audio-energy data, then write a fixed-schema NOTES.md covering hook map, animation inventory, reproduction cost in credits, pacing, on-screen text, structure, composition mix, audio, packaging and a steal list. Trigger on ANY request to look at, study, break down or learn from a video, including bare phrasings — "analyze this video", "analyse this video", "analyze this video for my channel", "break down this video", "break down this video's hook", "watch this video", "study this video", "what's this video doing", "how is this hook built", "how did they edit this", "what animations does this use", "what can I steal from this", "how is this packaged", "review this competitor video" — or whenever Eddie pastes a YouTube URL in any context about editing, animation, hooks, pacing, thumbnails or packaging. Also fires on "run the pipeline", "watch-video", or the script name. A bare YouTube URL with no instruction counts. NOT for transcript-only summaries or for answering what a video says — this exists because the useful information is visual.
---

# watch-video

Convert a video into the densest useful set of images plus text, then analyse that.
Transcripts alone are useless here; the information needed is visual.

**Core design decision: one frame per detected shot, plus dense uniform fill — never uniform sampling alone.**
Uniform sampling lands mid-shot and misses short animations. An animation appearing IS a scene change.
But cut detection is blind to slow morphing motion graphics, so uniform fill covers what it cannot see.
In practice the fill frames carry more of the design work than the detected cuts do.

Local only: yt-dlp, ffmpeg, Python stdlib. No MCP servers, no APIs, no paid services.
The only permitted `pip install` is a yt-dlp upgrade. Anything else, ask first.

## Run the pipeline

From the project the analysis belongs to, so output lands in that project's `research\`:

This skill is project-local. Run it from the project root, so output lands in this project's
`research\` and the path stays valid if the project moves:

```
py ".claude\skills\watch-video\scripts\watch_video.py" "<URL>"
```

Produces `research\<slug>\` containing `frames\`, `sheets\`, `data\`, and later `NOTES.md`.

Flags:
- `--slug NAME` — folder name (default: derived from title)
- `--root PATH` — output root (default: `./research`)
- `--start SEC` / `--end SEC` — analyse a segment only
- `--hook-only` — first 60 seconds only
- `--threshold N` — override scene threshold
- `--no-download` — reuse an already-downloaded source
- `--keep-source` — do not delete the source file when done

Resumable. Existing frames, sheets and data are reused, so re-running is cheap.

The script deletes the source video once frames and audio both succeed. Frames, sheets,
CSVs and NOTES.md are what gets kept; the source is disposable and re-downloadable.

Quote every path. These live under paths with spaces in them.

## What the pipeline decides, so you do not have to

**Pacing.** Detection runs at a fixed 0.20. Detections closer than 1.0s are merged into one shot,
because high-motion animation (camera push-ins) trips scene detection repeatedly inside a single
continuous shot. **Merged shots is the pacing number. Raw detections get one line, never a table.**
The threshold only drops (0.15, then 0.12) if merged shots/min falls under 4, which means cuts are
genuinely being missed. Band is 5–30 merged shots/min. If a video lands outside it, say so in the
report and move on — do not chase it with the threshold.

**Low-cut sections.** Any gap over 15s gets fill frames every 5s. These are reported as *low-cut
sections* and you describe what is actually in them. The pipeline runs SSIM across each section's
fill frames; only a section whose frames come back near-identical (≥0.97) is flagged a real
**dead zone**. Everything else is dense animation the detector could not see — the opposite of dead.

## SSIM cannot adjudicate cuts

**SSIM cannot adjudicate cuts. Fast camera motion depresses SSIM identically to a hard cut, so it
ranks continuous moving shots as the most definite cuts. Adjudicate disputed timestamps by
extracting before/after frame pairs and looking at them. SSIM remains valid ONLY for the dead-zone
check, comparing near-identical static fill frames.**

This was learned the hard way: on the first video analysed, SSIM's three most confident "real cuts"
(0.43–0.63) were all one continuous shot with a camera push. The dead-zone check is a different
question — "did anything change at all" — and SSIM answers that correctly.

To adjudicate: extract frames at `t-0.45` and `t+0.45`, tile them into labelled BEFORE/AFTER pairs,
and read them. Never trust a number here.

## Screen-recording bias

Cuts inside screen recordings score under 0.20, because window chrome and background barely change
across them. Videos built on screen recordings therefore **under-count**.

1. Decide from the composition mix in section 7. If screen recording is roughly a third or more of
   runtime, treat the video as screen-recording heavy.
2. In section 4, state explicitly that **merged shots/min is a floor, not a measurement**, and why:
   cuts inside screen recordings score under 0.20 because window chrome and background barely change.
3. Report both numbers as a range. The pipeline always computes a 0.12 reference during the run and
   stores it in `pacing.json` under `secondary_012` — the source is deleted after extraction, so this
   cannot be recomputed later. Use 0.20 as the floor and 0.12 as the ceiling: "between X and Y per
   minute". Do not present either end as the true figure.

## Build hierarchy — read before writing 3b

Cheapest first. Route every animation to the highest tier it can be done in.

1. **Remotion / coded template — FREE, 0 credits.** Deterministic text, reusable forever, no model
   drift. Build once, reuse across every video. This is the right route for **type, cards,
   diagrams, charts, wipes, lower-thirds, counters, timelines, UI mockups, and anything that
   repeats**. A repeating card template is a **Yes, build once** — never a No.
2. **Generated stills + video — costs credits, non-deterministic.** Nano Banana Pro for stills,
   Kling 3.0 or Seedance for motion. Correct only for photoreal, textured or illustrated b-roll
   that code cannot make: claymation, felt, 3D renders, cinematic scenes, characters.
3. **No Adobe. Ever.** If the answer would be "do it in After Effects", the answer is Remotion.
   Do not mark anything No because it needs AE.

Note that competitor motion graphics that look like AE are usually Remotion or coded HTML —
flat vector, crisp type, glass cards, gradient fields and spring easing all point that way.
Assume code unless there is real evidence otherwise.

**Separate assets from composition before costing any row.**
**Assets** are the images and clips *inside* the video — illustrations, characters, textures,
b-roll. They route to generation and cost credits.
**Composition** is the edit itself — how cards animate in, how type reveals, transitions, layout,
lower-thirds. It routes to code and costs nothing after the build.
A competitor can use generated assets inside a coded composition, and usually does: a title card
holding a generated illustration is an asset (costs credits) inside a composition (free). Cost only
the asset. Pricing a whole graphic sequence as generation because it looks polished is what
inverted this analysis the first time.

**Credit rates:** still ~2 cr · Kling 3.0 3s standard ~15 cr · Seedance 5s 720p standard ~22.5 cr ·
Remotion 0 cr. Budget is **150–250 credits per video**.

## Analysis

Read `data\transcript.txt`, `data\meta.json`, `data\pacing.json`, `data\audio_summary.json`,
`data\low_cut_sections.csv` first. Then the sheets.

**Maximum 60 images per read batch.** After each batch append findings to a working file and drop
those images from context. Notes persist, frames do not need to. The API caps at 100 images per
request, so accumulating across turns will eventually fail outright.

**Survey pass.** Read all sheets in order. Build a rough timeline and flag every timestamp that
looks like an animation, title card, graphic, chapter transition, or distinctive composition.

**Microscope pass.** Load flagged frames individually at full resolution from `frames\`. Read
on-screen text, describe animation style, note colour and layout. Expect 30–60 frames. Always
microscope the first 45 seconds densely regardless of what the survey flagged, because the hook
decides everything.

Sheets are for structure, not for reading text. Cells downscale to ~520x290 — enough to see that a
title card appeared, not enough to read it. Reading happens in the microscope pass.

Never silently reduce frame counts to save tokens. If a video would produce an unreasonable number
of frames, say the number and let Eddie decide.

## Output schema — NOTES.md

Fixed structure, identical every video, so these compound into a comparable library.
Write findings only. No preamble, no "this video demonstrates", no summary paragraph at the top.
These are reference material, not essays.

1. **Header.** Title, channel, duration, views, upload date, URL, date watched. Note sponsorship if present.

2. **Hook map, 0:00–0:45.** Beat by beat. Each beat: timestamp, what is said verbatim from the
   transcript, what is on screen from the frames, and what the beat is doing (claim, credibility,
   promise, teaser, pattern interrupt).

3. **Animation inventory.** Table: timestamp, duration, type, style description, what narration beat
   it covers, whether text is baked in.

3b. **Reproduction cost.** Immediately after the animation inventory. One row per inventory row.
   See the build-hierarchy section above — get the route right before costing it.
   Columns: timestamp, route (Remotion / Generated / Hybrid), buildable (Yes / Partial / No),
   stills, renders, **estimated credits**, notes. Mark repeating templates as build-once and say
   how many times they are reused. Total the credits at the bottom against the 150–250 per video
   budget, with a verdict: within budget, or X times over. **If over, name the cheapest viable
   subset** — what to build, what to cut, what to substitute with Remotion.
   Separate "can be built" from "is worth building". A 40-still generated montage can be
   technically buildable and still financially wrong. Say both.

4. **Pacing.** Merged shots per minute overall, the per-60s bucket curve (merged only — one table,
   not two), fastest and slowest stretches with timestamps, longest single shot, low-cut sections
   with what is in them, and any true dead zones. Add one line noting raw detection count and that
   high-motion animation inflates it. State if the video is outside the 5–30 band.

5. **On-screen text log.** Timestamp and verbatim text for every readable title, label, caption or
   callout. Note any repeating typographic system at the end.

6. **Segment structure.** Where each section starts and ends, what it delivers, and at what
   percentage of runtime the first real value lands.

7. **Composition mix.** Rough percentage split of talking head, screen recording, b-roll,
   full-frame graphics.

8. **Audio structure.** Findings only: are cuts beat-aligned or not, how many risers-into-breaks and
   where, where the energy actually comes from, music-bed versus dry sections. **No LUFS, LRA or
   median figures — they are noise.**

9. **Packaging.** Thumbnail described from `data\thumbnail.jpg`, the title, and how the two relate.
   Do they extend each other or repeat each other.

10. **Steal list.** Three to six specific, copyable techniques with timestamps. Concrete moves, not
    general praise.

Sections 2, 3, 3b, 7, 9 and 10 are the ones Eddie actually uses. Weight the effort accordingly.

## After every run — update the index

Append one row to `research\INDEX.md`. Never rewrite existing rows.

| Column | Source |
|---|---|
| Title, Channel, Subs, Views, Duration, Uploaded | `data\meta.json` |
| Multiplier | views ÷ subs, one decimal, e.g. `4.4x` |
| Analyzed | today's date |
| Takeaway | one line, the single most transferable finding |

The multiplier is the reason the index exists: it separates videos that outperformed their channel
from videos that merely have a big channel behind them. Sort your attention by it.

## Rules

- Windows paths, Windows commands, no bash-isms, no WSL assumptions.
- If a step fails, show the actual error before proposing a fix. Do not guess at causes.
- Fix problems yourself where you can rather than handing them back.
- If no captions exist at all, stop and say so. Whisper is not part of this pipeline.
