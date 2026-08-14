# Video Analysis Method

A portable description of how to make an AI usefully study a competitor's video. No code, no file
paths. Everything here is either a design decision with a reason behind it, or a lesson that cost
something to learn.

---

## 1. The problem

Analysing a competitor's video for editing and packaging means answering visual questions. What
animation appears at what moment. What the on-screen text says. How fast the cuts are. How the hook
is built shot by shot. A transcript answers none of that.

An AI model cannot ingest video. It can read a large number of images and text. So the entire job is
converting a video into the densest useful set of images plus text, and analysing that.

**Do not accept the common 100-frame cap.** Existing tools cap around 100 frames at 512px wide to
keep a single API call under a dollar. That cap is exactly why they fail at this. A frame at 512px
cannot be read for on-screen text, and 100 frames cannot cover an 8-minute video's design work.

---

## 2. What the pipeline does

Five mechanical stages, then analysis.

1. **Transcript and metadata**, without downloading the video. Auto-captions parsed into one clean
   line per cue with a timestamp. Title, duration, views, subscriber count, upload date, chapters,
   description and thumbnail URL pulled from the metadata.
2. **Download at 720p.** Deliberate: 1280x720 frames sit under the model's long-edge ceiling and
   cost about 1,229 image tokens each, so on-screen text stays readable without paying for pixels
   that get downscaled away.
3. **Shot detection and frame extraction.** One frame per detected shot, at native resolution, plus
   dense uniform fill in the gaps. Nudge the extraction ~0.1s past the detected boundary so the
   frame lands inside the new shot rather than on the last frame of the outgoing one.
4. **Contact sheets.** 3x3 grids at 1568px on the long edge, with the timestamp and frame index
   burned into each cell. Roughly 3,275 image tokens per sheet carrying 9 frames — about 364 tokens
   per frame, versus 1,229 for a full-resolution read.
5. **Audio energy curve.** Momentary loudness sampled and downsampled to one row per second, joined
   against the shot list.

Then a survey pass over the sheets, a microscope pass on flagged frames at full resolution, and a
fixed-schema write-up.

**Burn timestamps into every cell.** Without them the sheets are useless, because there is no way to
reference when anything happened or to find the corresponding full-resolution frame.

**Sheets are for structure, not for reading.** Cells downscale to roughly 520x290 — enough to see
that a title card appeared, not enough to read it. Reading happens in the microscope pass.

---

## 3. Honest limits

State these before trusting any number.

- **It cannot hear.** It measures loudness, not sound. Music, tone, voice quality and what the score
  is doing are all invisible. Only the shape of the energy curve is available.
- **It cannot see motion.** Frames are stills. Easing, speed ramps, transition style and camera
  movement are invisible except where consecutive frames imply them.
- **It under-counts cuts in screen recordings.** See §4.
- **Cut detection is blind to slow morphing graphics.** See §5.
- **One video per session.** A full analysis is roughly 150–250K tokens of images.

---

## 4. Pacing: fixed threshold, then merge

Run scene detection at a **fixed 0.20**. Then **merge any detections closer than 1.0s into a single
shot**. Report merged shots per minute. Raw detections get one line, never a table.

**Do not tune the threshold to hit a cuts-per-minute target.** That was the original design and it
was wrong — it optimises the measurement to match an expectation instead of measuring. Only lower
the threshold (0.15, then 0.12) if merged shots per minute falls under about 4, which means cuts are
genuinely being missed rather than the video being slow.

A sane band on merged shots is 5–30 per minute. If a video falls outside it, **say so in the report
and move on.** Do not chase it with the threshold.

### Why merging is mandatory

High-motion animation trips scene detection repeatedly *inside a single continuous shot*. A camera
push-in on one still image can register a dozen times. On the first video analysed, a single
three-second watch zoom produced 14 false detections and inflated its minute-bucket from 9 to 44,
which would have been reported as the fastest-cut section of the video. It was one shot.

### The screen-recording bias

Cuts inside screen recordings barely move the pixels — same window chrome, same background, same
layout — so they score below 0.20 and vanish. App switches, layout changes and swapped images all
get missed.

Handle it like this:

1. Decide from the composition mix. If screen recording is roughly a third or more of runtime, treat
   the video as screen-recording heavy.
2. Say explicitly that **merged shots per minute is a floor, not a measurement**, and why.
3. Report a **range**, using a second detection pass at 0.12 as the upper end. Present neither end
   as the true figure.

Compute that second pass **during the run**, while the source still exists. If the source is deleted
afterwards it cannot be recomputed without re-downloading.

### Measured accuracy

On the first video analysed, adjudicating all 17 timestamps where 0.12 and 0.20 disagreed: 11 were
phantoms (camera moves inside one generated shot, or scrolling inside one screen recording) and 6
were real (all screen-recording transitions). True count ~55 shots. The 0.20 pass gave 49, about 11%
low. The 0.12 pass gave 67, about 22% high. **0.20 plus merging is the better estimator, and it errs
low on screen recordings specifically.**

---

## 5. Uniform fill matters more than cut detection

Cut detection cannot see slow morphing motion graphics. A 50-second stretch of continuously animating
cards, diagrams and headline reveals registers as a single shot, because no two consecutive frames
differ enough to trip the threshold.

So: **any gap over 15 seconds gets fill frames every 5 seconds.**

Call these **low-cut sections**, not dead zones, and describe what is actually in them. On the first
video analysed, all four flagged gaps turned out to be the *densest design work in the piece* — 31%
of runtime, and the entire motion-graphics argument. Calling them dead would have inverted the
finding completely.

Reserve a real **dead zone** flag for a long gap whose fill frames come back visually near-identical.

**The 5s interval is not arbitrary.** At 10s, the pipeline stepped straight over a five-second
sponsor card. At 5s it caught it. Fill density is the single highest-leverage parameter here.

---

## 6. SSIM cannot adjudicate cuts

**Fast camera motion depresses SSIM identically to a hard cut, so it ranks continuous moving shots
as the most definite cuts.**

This was learned by trusting it. Asked to classify disputed timestamps, SSIM's three most confident
"real cuts" — the lowest similarity scores in the whole set — were all one continuous shot with a
camera push. The metric is not weakly wrong here, it is inverted.

**Adjudicate disputed timestamps by extracting before/after frame pairs and looking at them.**
Extract at roughly 0.45s either side, tile them into labelled BEFORE/AFTER pairs, and read them.

SSIM remains valid for **one** job: the dead-zone check, comparing near-identical static fill frames.
That is a different question — "did anything change at all" — and it answers that correctly.

General lesson: a similarity metric answers "how different are these two images", never "is this an
edit". Those come apart precisely where it matters.

---

## 7. Analysis procedure

Read the data files first — transcript, metadata, pacing, audio summary, low-cut sections. Then the
sheets.

**Maximum 60 images per read batch.** After each batch, append findings to a working file and drop
those images from context. Notes persist; frames do not need to. Accumulating images across turns
eventually fails outright against the per-request image cap.

**Survey pass.** Read all sheets in order. Build a rough timeline. Flag every timestamp that looks
like an animation, title card, graphic, chapter transition or distinctive composition.

**Microscope pass.** Load flagged frames individually at full resolution. Read on-screen text,
describe animation style, note colour and layout. Expect 30–60 frames. **Always microscope the first
45 seconds densely** regardless of what the survey flagged, because the hook decides everything.

**Never silently reduce frame counts to save tokens.** If a video would produce an unreasonable
number of frames, say the number and let the human decide.

---

## 8. Output schema

Fixed structure, identical every video, so analyses compound into a comparable library. Findings
only — no preamble, no "this video demonstrates", no summary paragraph at the top. Reference
material, not essays.

1. **Header.** Title, channel, duration, views, upload date, URL, date watched. Note sponsorship.
2. **Hook map, 0:00–0:45.** Beat by beat: timestamp, what is said verbatim, what is on screen, and
   what the beat is doing (claim, credibility, promise, teaser, pattern interrupt).
3. **Animation inventory.** Timestamp, duration, type, style description, which narration beat it
   covers, whether text is baked in.
3b. **Reproduction cost.** One row per inventory row — see §9.
4. **Pacing.** Merged shots per minute, the per-60s bucket curve (one table, not two), fastest and
   slowest stretches, longest single shot, low-cut sections with what is in them, any true dead
   zones. One line noting raw detections and why they inflate. Flag if outside the band.
5. **On-screen text log.** Timestamp and verbatim text for every readable title, label, caption or
   callout. Note any repeating typographic system at the end.
6. **Segment structure.** Where each section starts and ends, what it delivers, and at what
   percentage of runtime the first real value lands.
7. **Composition mix.** Rough percentage split of talking head, screen recording, b-roll, full-frame
   graphics.
8. **Audio structure.** Findings only: are cuts beat-aligned, how many risers-into-breaks and where,
   where the energy actually comes from, music-bed versus dry sections. **No LUFS, LRA or median
   figures** — they are noise to a human reader.
9. **Packaging.** Thumbnail described, the title, and how the two relate. Do they extend each other
   or repeat each other.
10. **Steal list.** Three to six specific, copyable techniques with timestamps. Concrete moves, not
    general praise.

Sections 2, 3, 3b, 7, 9 and 10 are the ones that get used. Weight the effort accordingly.

### On risers and audio findings

Speech alone swings several loudness units every second, so a naive "rise then drop" detector fires
constantly. An early version reported 37 risers in an 8-minute video. Nearly all were ordinary
sentence dynamics.

A riser worth reporting must be a **smoothed** rise to a peak **above** the loud band, landing in the
**quiet** band. That combination is what a real transition sounds like. Retuned, it found one, and
one was correct.

**A low riser count is not evidence the detector is under-firing.** Most videos do not use musical
risers at all. The video this was tuned on had eight creator-marked chapters but exactly **one**
musical riser — it carried its other seven transitions *visually*, with a hard graphic wipe and
changes of visual mode, which produce no loudness signature whatever.

So: **never calibrate the riser detector against the chapter count.** They measure different things.
Chapters are structural intent; risers are one specific audio device used to mark a transition, and
plenty of well-made videos never use it. If risers come back at zero or one, the correct conclusion
is usually "this video marks its transitions visually" — go and confirm that in the frames. Do not
loosen the thresholds to make the number match the chapters; that walks the detector straight back
into reporting speech dynamics as structure.

Also check where energy actually comes from. On the first video analysed, every high-energy run was
an embedded product demo playing its own music, not an edit decision. The narration had no music bed
at all. Reporting "high-energy montage sections" would have been wrong.

---

## 9. Reproduction cost: route before you cost

This is what turns an inventory into a build list. For every animation, decide the **route** first.
Cheapest first:

1. **Coded template (Remotion or HTML) — free, 0 credits.** Deterministic text, reusable forever, no
   model drift. The right route for type, cards, diagrams, charts, wipes, lower-thirds, counters,
   timelines, UI mockups, and **anything that repeats**. A repeating card template is a
   *yes, build once* — never a no.
2. **Generated stills plus video — costs credits, non-deterministic.** Correct only for photoreal,
   textured or illustrated b-roll that code cannot make: claymation, felt, 3D renders, cinematic
   scenes, characters.
3. **No Adobe. Ever.** If the answer would be "do it in After Effects", the answer is a coded
   template.

**Competitor motion graphics that look like After Effects are usually coded.** Crisp type at every
scale, pixel-exact card grids, spring easing, live status pills, and one template re-instanced with
different content all point that way. Assume code unless there is real evidence otherwise. Getting
this backwards inverts the entire cost analysis — it marks the free rows as impossible and the
expensive rows as easy.

### Separate assets from composition

Before costing any row, decide which **layer** it belongs to. This is the distinction that matters
most, and getting it backwards is what inverted the analysis the first time.

**Assets** are the individual images and clips *inside* the video — illustrations, characters,
textures, b-roll, generated scenes. These route to generation and **cost credits**.

**Composition** is the edit itself — how cards animate in, how type reveals, transitions, layout,
lower-thirds, the arrangement and timing of everything on screen. This routes to code and **costs
nothing** beyond the one-time build.

**A competitor can use generated assets inside a coded composition, and usually does.** A title card
holding a generated illustration is two layers at once: the illustration is an asset with a credit
cost, the card, its entrance, its type and its timing are composition and are free. Cost only the
asset.

Judge each row on which layer it belongs to *before* costing it. The failure mode is looking at a
polished graphic sequence, deciding it must have been rendered or made in a video tool, and pricing
the whole thing as generation — when the animation is code and only the pictures inside it cost
anything. That single mistake can multiply an estimate several times over.

### Credit rates

| Item | Cost |
|---|---|
| Still image | ~2 credits |
| Kling 3.0, 3s standard | ~15 credits |
| Seedance, 5s 720p standard | ~22.5 credits |
| Coded template | 0 |

Budget: **150–250 credits per video.**

Total every row, compare against budget, and give a verdict — within budget, or X times over.
**If over, name the cheapest viable subset**: what to build, what to cut, what to substitute with a
coded template.

**Separate "can be built" from "is worth building."** A 40-still generated montage can be technically
buildable and financially wrong. Say both.

Two things fall out of this reliably. First, **cost is inverted from screen time** — generated b-roll
is a third of runtime and nearly all of the cost, while graphics are another third and cost nothing
after the build. Second, **a competitor's most expensive sequences are often their least
transferable**, because they exist to demo a product the competitor is being paid to show.

---

## 10. Operating notes

- **One video per session.** 150–250K tokens of images per analysis.
- **Keep the frames and the notes, delete the source.** The video is disposable and re-downloadable;
  the extracted frames and the write-up are the asset.
- **Make it resumable.** These get re-run on the same videos. Skip any stage whose output exists, and
  do not re-download a source that nothing still needs.
- **Keep an index** across videos: title, channel, subscribers, views, duration, date, and one line
  of takeaway. Include **views ÷ subscribers**. That multiplier is what separates a video that
  outperformed its channel from one that merely has a big channel behind it — sort attention by it.
- **Smoke-test any pipeline change end to end before trusting it.** Two real bugs surfaced only on a
  full run: a filter argument that broke on a Windows drive-letter colon, and a shot count that came
  out higher than the raw detection count it derived from.
- **When numbers change after a refactor, adjudicate before adopting.** Do not assume the new code is
  right because it is newer, or the old number is right because it was reported. Go and look at the
  frames.
