---
name: watch-video
description: Watch a YouTube video properly — extract one frame per shot plus dense uniform fill, build timestamped contact sheets, a clean transcript, pacing and audio-energy data, then answer in one of three modes: a creator breakdown (fixed-schema NOTES.md covering hook map, animation inventory, reproduction cost in credits, pacing, on-screen text, structure, composition mix, audio, packaging and a steal list), a content brief (what the video actually teaches, including the diagrams, code, UI and on-screen text a transcript cannot see), or a direct answer to a specific question with cited timestamps. Trigger on ANY request to watch, look at, study, summarize, explain, break down or learn from a video, including bare phrasings — "analyze this video", "summarize this video", "what does this video teach", "explain this video", "break down this video", "break down this video's hook", "watch this video", "study this video", "what's this video doing", "how is this hook built", "how did they edit this", "what animations does this use", "what can I steal from this", "how is this packaged", "review this competitor video", "what happens at 4:20", "does this video cover X" — or whenever a YouTube URL is pasted in any context about video, editing, animation, hooks, pacing, thumbnails, packaging, tutorials or learning. Also fires on "run the pipeline", "watch-video", or the script name. A bare YouTube URL with no instruction counts. Use this INSTEAD of a transcript-only summary whenever the video has anything on screen worth seeing.
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

## Step 0 — ask before you download. Always.

**Before running anything, use AskUserQuestion.** Two questions, one call. Do this on every
invocation, even when the request looks obvious — inferring silently is how someone asking
"what does this video teach" ends up with an animation inventory and a credit budget.

**Question 1 — what kind of analysis?**

| Option | Produces | For |
|---|---|---|
| Creator breakdown | `NOTES.md`, the fixed 10-section schema | How the video was *made*: hooks, animation, pacing, packaging, what to steal |
| Content brief | `BRIEF.md` | What the video *teaches*: the argument, the steps, the diagrams and code on screen |
| Specific question | An answer in chat, no file | "What happens at 4:20", "does it cover X", "what tool is that" |

**Question 2 — how deep?** Offer `standard` (default), `deep`, `max` — see the depth section.
If they pick `max`, say in the same breath that it must be paired with `--hook-only` or a
`--start/--end` window on anything long, and offer to scope it.

If the user already named a scope ("the hook", "from 2:15 to 2:45"), pass it and say you did.
Take their answers, run the pipeline once with the right flags, and do not ask again.

## Run the pipeline

This skill is project-local. Run it from the repo root:

```
py ".claude\skills\watch-video\scripts\watch_video.py" "<URL>"
```

On macOS use `python3` instead of `py`. Windows and macOS are supported; Linux is untested.

Produces `video-research\<slug>\` inside the repo, containing `frames\`, `sheets\`, `data\`,
and later `NOTES.md`. **That folder is in `.gitignore` and must stay there** — one analysis is
8–35 MB. The script warns if the ignore is ever missing.

Flags:
- `--check` — report every dependency, then exit. Run this first if anything fails.
- `--slug NAME` — folder name (default: derived from title)
- `--root PATH` — output root (default: `video-research\` in the repo)
- `--start T` / `--end T` — analyse a segment only. Accepts `SS`, `MM:SS` or `HH:MM:SS`.
- `--hook-only` — first 60 seconds only
- `--threshold N` — override scene threshold
- `--depth standard|deep|max` — how many frames to **extract**. See below.
- `--no-download` — reuse an already-downloaded source
- `--keep-source` — do not delete the source file when done
- `--font PATH` — bold TTF for sheet labels (default: auto-detected per platform)
- `--no-channel-baseline` — skip the channel median fetch (faster, no channel multiplier)
- `--patterns` — build the cross-video corpus and exit. See the pattern pass below.

**When yt-dlp fails, read the message — it names the fix.** The two recoverable failures:
- `--player-client tv,web_safari,mweb` — SABR streaming, when a download stalls or returns 0 bytes.
  Try this first; it is cheaper and less fragile than cookies.
- `--cookies-from-browser chrome` — the "sign in to confirm you're not a bot" challenge.
  **Close Chrome first**; it locks the cookie database on Windows.
- `--update` — upgrades yt-dlp. The only permitted install. Try it before anything else if
  YouTube behaviour has changed.

Resumable. Existing frames, sheets and data are reused, so re-running is cheap — a completed
video re-runs in about two seconds with no network. A re-run finds its folder by **video id**,
not by title, so a retitled video still lands in its original folder.

The cache is invalidated automatically if `--start`, `--end`, `--threshold` or `--depth` differ
from the run that produced it. It will say what changed and re-detect. That is correct, not a fault.

The script deletes the source video once frames and audio both succeed. Frames, sheets,
CSVs and NOTES.md are what gets kept; the source is disposable and re-downloadable.

Quote every path. These live under paths with spaces in them.

## Depth — choose it before running, it is not free to change your mind

`--depth` controls how many frames are **extracted**. It never touches the detection threshold or
the 1.0s merge window, so **`merged_shots_per_min` is identical at every depth** — the pacing you
report describes the video, never your setting. Verified: standard/deep/max over the same 60s
window all returned `merged 27 (27.0/min)` while frames went 27 → 35 → 57.

| | fill interval | fills gaps over | recovers sub-second cuts |
|---|---|---|---|
| `standard` (default) | 5s | 15s | no |
| `deep` | 3s | 8s | yes |
| `max` | 1s | 2s | yes |

**Two different blind spots, and depth is the only thing that closes either.**

1. **Sub-second cuts.** The merge window keeps a camera push-in from being counted as fourteen
   shots, but it also means nothing shorter than a second ever gets a frame. On a fast-cut edit
   that is a third of all detections — 184 of 584 on one 36-minute video. `deep` and `max`
   extract those frames back, tagged `sub` in `cuts.csv` and marked `<` on the sheets. **They are
   still excluded from the shot count**, so pacing is unaffected.
2. **Dense animation with no long gaps.** Fill only fires inside gaps, so a high-paced video gets
   almost no fill — exactly the videos where the detector is blindest get the least extra coverage.
   Lowering the gap limit is what fixes that.

**When to reach for it:**
- `standard` — the default. Use it unless something below applies.
- `deep` — the request is about *how something was made*: animation inventory, hook construction,
  "how did they edit this", a video that looks fast-cut or motion-graphics heavy.
- `max` — close study of a **specific stretch**. Always pair with `--hook-only` or `--start/--end`;
  on a full-length video it will blow past the frame ceiling. `--hook-only --depth max` is the
  right call for "break down this hook frame by frame".

Changing depth **invalidates the cache** and re-extracts, which on a completed video means
re-downloading the source. Pick the depth before the first run where you can.

In section 4, if you used `deep` or `max`, say so and give the recovered sub-second count as a
separate line. Do not fold it into shots per minute.

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

**Survey pass.** Read all sheets in order and build a rough timeline. **What you flag depends on
the mode chosen in Step 0:**

- **Creator breakdown** — flag every animation, title card, graphic, chapter transition and
  distinctive composition.
- **Content brief** — flag every frame carrying *information*: diagrams, code, terminal output,
  UI states, charts, settings panels, before/after comparisons, any slide with text on it.
  Ignore pure b-roll and talking head unless something is written on screen.
- **Specific question** — flag only what bears on the question, plus enough either side to place
  it in context. You can stop the survey early once you have found it; say that you did.

**Microscope pass.** Load flagged frames individually at full resolution from `frames\`. Read
on-screen text, describe what is shown, note colour and layout. Expect 30–60 frames for a
breakdown or a brief, and as few as 5–10 for a specific question.

For a **creator breakdown**, always microscope the first 45 seconds densely regardless of what the
survey flagged, because the hook decides everything. For a **content brief** that rule does not
apply — the value is usually in the middle, so spend the frames where the teaching happens.

Sheets are for structure, not for reading text. Cells downscale to ~520x290 — enough to see that a
title card appeared, not enough to read it. Reading happens in the microscope pass.

Never silently reduce frame counts to save tokens. If a video would produce an unreasonable number
of frames, say the number and let the user decide.

**Runtime ceiling — measured, around 40 minutes.** A 36:07 video came to 469 frames and 53 sheets,
about 228K image tokens: it fits, with nothing spare. The script prints the projected cost right
after cut detection, and past 500 frames it warns that one session may not hold it. **Read that
line.** If it fires, give the user the number and offer to run `--hook-only` or a
`--start`/`--end` window instead — do not just plough on and run out of context mid-survey.

## Output — pick the one the user chose in Step 0

Three modes. Write **one** of them. Do not write a creator breakdown for someone who asked what a
video teaches, and do not write a whole file for someone who asked a one-line question.

---

## Mode B — content brief, `BRIEF.md`

For "summarize this", "what does this teach", "explain this video", "walk me through this
tutorial". Write to `video-research\<slug>\BRIEF.md`.

**The reason this mode exists at all is the frames.** A transcript-only summary is free and
already possible without this tool; it is also wrong about anything on screen. Your job is to
produce the summary that could only be written by something that *watched* it — every diagram,
code block, command, chart, price, setting and UI state included, with the timestamp.
**If your brief could have been written from the transcript alone, you have wasted the pipeline.**

1. **Header.** Title, channel, duration, upload date, URL, date watched. One line on what kind of
   video it is (tutorial, essay, review, demo, interview).
2. **What it claims or teaches.** The actual argument or lesson, in the video's own order. Three
   to eight points. Quote the load-bearing sentences verbatim with timestamps.
3. **On-screen information.** A table: timestamp, what is shown, and the content read verbatim
   from the frame. Commands, code, diagram labels, chart figures, settings, file paths, prices.
   **This is the section that justifies the tool.** If the video has none, say so explicitly —
   that is itself a finding, and it means a transcript would have served just as well.
4. **Steps, if it is a tutorial.** The actual reproducible procedure with timestamps. Include
   anything performed on screen but never spoken aloud — that is the most common thing a
   transcript loses.
5. **Anything the narration and the screen disagree about.** Where what is said and what is shown
   do not match, including versions, prices, or a demo that fails and is talked past. Often the
   single most useful line in the whole brief. "None found" is a fine answer.
6. **What it does not cover.** Gaps, assumed prerequisites, unanswered questions.
7. **Worth going back to.** Three to six timestamps and why.

---

## Mode C — specific question

The user asked something concrete. **Answer it in chat. Write no file** unless they ask for one.

- Lead with the answer. Do not open with a summary of the video.
- Cite timestamps for every claim, and say whether it came from the frames, the transcript, or both.
- Quote on-screen text verbatim where it settles the question.
- If the frames do not answer it, say so plainly and say what they do show. Do not fill the gap
  from general knowledge — the whole point is that this answer is grounded in the actual video.
- Offer the fuller mode at the end, one line: a brief or a breakdown if they want the rest.

---

## Mode A — creator breakdown, NOTES.md

Fixed structure, identical every video, so these compound into a comparable library.
Write findings only. No preamble, no "this video demonstrates", no summary paragraph at the top.
These are reference material, not essays.

**Every section heading must be written exactly as `## N. Title`**, using the numbers below —
`## 3b.` for reproduction cost, not `### 3b.`. The pattern pass slices sections out of these
files by number, so a heading at the wrong level or with the number omitted drops that section
out of the cross-video corpus.

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

Sections 2, 3, 3b, 7, 9 and 10 are the ones that actually get used. Weight the effort accordingly.

## After every run — write the takeaway

**The script writes the index row itself.** It upserts `video-research\index.json` and regenerates
`video-research\INDEX.md` from it, sorted by channel multiplier. Do not hand-edit `INDEX.md` —
it is overwritten on the next run.

Your only job is the one field a script cannot fill. After writing your output, open
`video-research\index.json`, find this video's record, and replace `"takeaway": "_pending_"`
with one line. It survives every future re-run.

What that line should say depends on the mode:
- **Creator breakdown** — the single most transferable finding about how it was made.
- **Content brief** — the single most useful thing the video actually teaches.
- **Specific question** — the answer, in one line, so the index records what was learned.

Prefix a brief's takeaway with `[brief]` so a later reader knows it was not a creator analysis.

**Two multipliers, and they disagree.** `multiplier_vs_channel` is views ÷ the channel's median
recent views — how far this video beat *its own channel*. `views_per_sub` is the cruder views ÷
subscribers. When they disagree, the channel multiplier is the one that means something: in the
current corpus one video reads 0.84x by views/sub and 7.1x against its channel, and it is the
best-performing video in the set. Sort your attention by `multiplier_vs_channel`.

A null `multiplier_vs_channel` means the channel baseline could not be fetched (fewer than 8
usable uploads, or a fetch failure). It is not a low score — say "no baseline", never "0x".

## The pattern pass — across videos, not within one

Triggers: "find the patterns", "what do the winners do", "run the pattern pass", "compare the
analyses", "what's working across these".

1. Run `py ".claude\skills\watch-video\scripts\watch_video.py" --patterns`. It writes
   `video-research\PATTERNS-INPUT.md` — the numeric comparison table plus sections 2, 3b, 6, 7
   and 9 of every `NOTES.md`. No URL, no network, no images.
2. Read it. Text only, one pass, no batching.
3. Write `video-research\PATTERNS.md` to the schema below.
4. **Never analyse a fresh video in the same session as a pattern pass.** The one-video-per-session
   rule still holds for image-heavy work.

The script refuses to build under 6 analyses, and stamps a corpus warning under 12. If the warning
is there, carry it into `PATTERNS.md` and treat every finding as provisional. Do not argue with it.

**Only creator breakdowns feed the pattern pass.** It slices sections out of `NOTES.md`, so a
folder holding only a `BRIEF.md` is listed separately as a content brief and excluded - correctly,
because a brief has no hook map or reproduction cost to compare. If the corpus is short, that is
the first place to look for videos worth re-running as breakdowns.

`PATTERNS.md` schema:

1. **Corpus.** n, upload date range, multiplier range, how many lack a channel multiplier, and the
   corpus warning verbatim if n < 12.
2. **Top tercile vs bottom tercile.** Split by `multiplier_vs_channel`, excluding videos without
   one. For each of: merged shots/min, median shot length, low-cut seconds as a percentage of
   runtime, risers per 10 minutes, beat alignment ratio, words per minute, runtime, first-value
   timestamp as a percentage of runtime (section 6), composition mix (section 7), animation count
   and total credits (section 3b) — give the top-third figure, the bottom-third figure, and
   whether the gap is large enough to mean anything at this n.
3. **Hook patterns.** Top tercile only: what beats appear, in what order, how long the hook runs,
   what is on screen during each. Name the beats present in most of the top and absent from most
   of the bottom.
4. **Packaging patterns.** From section 9: do title and thumbnail extend each other or repeat each
   other, and does that split along the multiplier line.
5. **Contradictions.** Anything the corpus does *not* support, explicitly including cases where the
   data contradicts a belief the user already holds or has written down. Mandatory. "None found"
   is acceptable only after actually looking.
6. **What this means for the next video.** Concrete targets with numbers: shots per minute,
   runtime, first-value timestamp, animation count and credit budget, hook beat order.
7. **Confidence.** What n supports, what it does not, and which single dimension would benefit most
   from more analyses.

Section 5 is the one that earns the tool its keep. A synthesis that only confirms what is already
believed has produced nothing.

## Rules

- Windows and macOS. On Windows use `py` and backslash paths; on macOS use `python3` and forward
  slashes. No WSL assumptions. Linux may work but is untested.
- If a step fails, show the actual error before proposing a fix. Do not guess at causes.
  The script surfaces ffmpeg's and yt-dlp's real stderr — quote it rather than paraphrasing.
- Run `--check` before diagnosing anything. It names the missing dependency and its install command.
- Fix problems yourself where you can rather than handing them back.
- If no captions exist at all, stop and say so. Whisper is not part of this pipeline, deliberately:
  no API keys, no accounts, nothing to pay for.
- Never hand-edit `INDEX.md` or `PATTERNS-INPUT.md`. Both are generated. Edit `index.json`.
