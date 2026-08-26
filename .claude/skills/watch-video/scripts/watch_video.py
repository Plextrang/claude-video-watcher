"""
watch-video pipeline: URL -> frames, contact sheets, transcript, pacing, audio.

Everything up to analysis. Analysis + NOTES.md schema live in SKILL.md.
Local only: yt-dlp, ffmpeg, Python stdlib. No pip installs, no APIs.

Windows-first. All paths are pathlib objects passed to subprocess as list args,
so spaces in paths never touch a shell.
"""
import argparse
import csv
import datetime
import json
import os
import platform
import re
import shutil
import statistics as st
import subprocess
import sys
from pathlib import Path

# A video title carrying an emoji or a non-Latin script used to kill log()
# outright on a cp1252 console: UnicodeEncodeError, mid-run, no output.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------- tuning ----
BASE_THRESHOLD = 0.20      # fixed. Only lowered if we are genuinely missing cuts.
THRESHOLD_LADDER = [0.15, 0.12]
MERGE_WINDOW = 1.0         # detections closer than this are one shot
MIN_MERGED_PER_MIN = 4.0   # below this we are missing cuts -> lower threshold
BAND = (5.0, 30.0)         # merged shots/min. Reported, never chased.
GAP_LIMIT = 15.0           # gaps longer than this get uniform fill
FILL_EVERY = 5.0
DEAD_SSIM = 0.97           # fills this similar => genuinely static
COLS, ROWS = 3, 3
CW, CH = 522, 294          # 3x3 -> 1566x882, long edge under 1568
PER = COLS * ROWS
# Resolved per platform in main() before anything downloads. Contact sheets
# are the whole point of the tool and drawtext needs a real font file, so a
# missing font must fail at startup rather than after a 300 MB download.
FONT = None
TAG = re.compile(r'<[^>]*>')
CUE_TIMING = re.compile(r'<\d{2}:\d{2}:\d{2}\.\d{3}>')   # auto-subs only
PTS = re.compile(r'pts_time:([0-9.]+)')
YT_ID = re.compile(r'(?:v=|/shorts/|youtu\.be/|/embed/|/live/|/v/)'
                   r'([A-Za-z0-9_-]{11})')

# Populated once in main(), then read by ytdlp(). Mutated in place, never
# rebound, so the module-level name ytdlp() closes over stays correct.
COOKIES = []
EXTRACTOR_ARGS = []
PENDING = '_pending_'   # takeaway placeholder, filled in by Claude
YTDLP_CMD = []     # resolved once by resolve_ytdlp(): module or binary
JS_RUNTIME = []    # ['--js-runtimes', 'node'] when this yt-dlp supports it

# The two yt-dlp failures that are recoverable but not guessable.
BOT_HINTS = ('confirm you', 'not a bot', 'sign in to confirm')
RATE_HINTS = ('429', 'too many requests')
# Explicit, never 'en.*': that pattern also matches YouTube's auto-TRANSLATED
# tracks (en-de-DE, en-fr-FR, ...), one HTTP request each.
SUB_LANGS = 'en,en-orig,en-US,en-GB'
SABR_HINTS = ('sabr', 'requested format is not available',
              'fragment', 'missing a url')

# Every analysis lands in video-research/ inside the repo, so a clone is
# self-contained and there is nothing to set up by hand. That output is kept
# out of version control by .gitignore, NOT by being physically elsewhere -
# one analysis is 8-35 MB of frames and sheets, so if that entry is ever
# removed, the next commit carries the whole corpus.
#   .../<repo>/.claude/skills/watch-video/scripts/watch_video.py
#   parents[4] == <repo>
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ROOT = REPO_ROOT / 'video-research'


def mmss(s):
    return f"{int(s) // 60:02d}:{int(s) % 60:02d}"


def run(cmd, **kw):
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True,
                              errors='ignore', **kw)
    except subprocess.CalledProcessError as e:
        # Show the tool's real error, not a Python traceback.
        tail = [l for l in (e.stderr or e.stdout or '').strip().split('\n') if l.strip()]
        sys.exit('FATAL: yt-dlp failed.\n'
                 + '\n'.join(tail[-6:] or ['(no output)'])
                 + remediation(tail))


def remediation(tail):
    """Turn yt-dlp's two recoverable failures into instructions. Neither is
    guessable under pressure, and both are unrecoverable mid-record."""
    blob = '\n'.join(tail).lower()
    if any(h in blob for h in RATE_HINTS):
        return ('\n\nYouTube rate-limited the request (HTTP 429). Wait a '
                'few minutes and re-run - the run is resumable, so nothing '
                'already downloaded is lost.')
    if any(h in blob for h in SABR_HINTS):
        return ('\n\nThis looks like YouTube SABR streaming. Retry with:\n'
                '  --player-client tv,web_safari,mweb\n'
                'If that still fails, run --update first: yt-dlp ships SABR '
                'fixes faster than anything else.')
    if any(h in blob for h in BOT_HINTS):
        return ('\n\nYouTube threw its bot check. Retry with:\n'
                '  --cookies-from-browser chrome\n'
                'Close Chrome first: it locks the cookie database on Windows.')
    return ''


def resolve_ytdlp():
    """The pip module and the standalone binary are different installs and
    most people have exactly one. This hardcoded `sys.executable -m yt_dlp`,
    which is what this machine has - but brew, winget and pipx all produce a
    binary with no module, so every viewer who followed the usual install
    instructions got 'No module named yt_dlp' on their first run."""
    if YTDLP_CMD:
        return YTDLP_CMD
    p = subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'],
                       capture_output=True, text=True, errors='ignore')
    if p.returncode == 0:
        YTDLP_CMD[:] = [sys.executable, '-m', 'yt_dlp']
    elif shutil.which('yt-dlp'):
        YTDLP_CMD[:] = [shutil.which('yt-dlp')]
    else:
        return None
    # --js-runtimes is recent. Probe rather than parse a version string, so
    # an older yt-dlp degrades instead of dying on 'unrecognized arguments'.
    probe = subprocess.run(YTDLP_CMD + ['--js-runtimes', 'node', '--version'],
                           capture_output=True, text=True, errors='ignore')
    JS_RUNTIME[:] = ['--js-runtimes', 'node'] if probe.returncode == 0 else []
    return YTDLP_CMD


def ytdlp(args):
    cmd = resolve_ytdlp()
    if cmd is None:
        sys.exit('FATAL: yt-dlp is not available.\n  ' + install_hint('yt-dlp'))
    return cmd + JS_RUNTIME + COOKIES + EXTRACTOR_ARGS + args


def install_hint(tool):
    system = platform.system()
    ffmpeg = {'Windows': 'winget install Gyan.FFmpeg',
              'Darwin': 'brew install ffmpeg'}
    table = {
        'ffmpeg': ffmpeg,
        'ffprobe': ffmpeg,
        'yt-dlp': {'Windows': f'"{sys.executable}" -m pip install -U yt-dlp'
                              '   (or: winget install yt-dlp.yt-dlp)',
                   'Darwin': 'brew install yt-dlp   (or: pipx install yt-dlp)'},
        'node': {'Windows': 'winget install OpenJS.NodeJS.LTS',
                 'Darwin': 'brew install node'},
    }
    return table.get(tool, {}).get(
        system, f'install {tool} and put it on PATH')


def preflight(font_override=None):
    """Everything the pipeline shells out to, checked before it is needed.
    Without this a viewer with no ffmpeg got FileNotFoundError: [WinError 2].
    Returns (problems, warnings, resolved_font)."""
    problems, warnings = [], []
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            problems.append((tool, 'not on PATH', install_hint(tool)))
    if resolve_ytdlp() is None:
        problems.append(('yt-dlp', 'neither the Python module nor the binary '
                                   'is available', install_hint('yt-dlp')))
    elif not JS_RUNTIME:
        warnings.append(('yt-dlp', 'too old for --js-runtimes; YouTube may '
                                   'refuse some videos', 'run with --update'))
    if not shutil.which('node'):
        warnings.append(('node', "not on PATH - yt-dlp uses it to solve "
                                 "YouTube's JS challenge; some videos will "
                                 "fail without it", install_hint('node')))
    font, tried = resolve_font(font_override)
    if not font:
        problems.append(('font', f'no bold TTF found ({len(tried)} paths '
                                 f'tried) for the contact sheet labels',
                         'pass --font "path/to/a/bold.ttf"'))
    elif "'" in font:
        problems.append(('font', f"path contains an apostrophe, which "
                                 f"ffmpeg's filtergraph parser cannot "
                                 f"carry: {font}",
                         'copy the font somewhere without one, then --font'))
        font = None
    return problems, warnings, font


def run_soft(cmd, **kw):
    """Run without exiting on failure. yt-dlp returns non-zero when a single
    subtitle variant fails, even though everything actually needed came down
    fine - so the caller decides based on what landed on disk, not on the
    exit code."""
    return subprocess.run(cmd, capture_output=True, text=True,
                          errors='ignore', **kw)


def ff(cmd, what, **kw):
    """Run ffmpeg and surface its real error instead of a Python traceback.
    SKILL.md promises the actual error before any proposed fix; bare
    check=True broke that promise everywhere it was used."""
    p = subprocess.run(cmd, capture_output=True, text=True,
                       errors='ignore', **kw)
    if p.returncode != 0:
        tail = [l for l in (p.stderr or '').strip().split('\n') if l.strip()]
        sys.exit(f'FATAL: ffmpeg failed during {what}.\n'
                 + '\n'.join(tail[-6:] or ['(no output)']))
    return p


def log(msg):
    print(msg, flush=True)


def url_video_id(url):
    """The 11-char YouTube id, parsed locally - no network call."""
    if not url:
        return None
    m = YT_ID.search(url)
    return m.group(1) if m else None


# ------------------------------------------------------------- transcript ----
def ts_to_sec(t):
    """WebVTT permits MM:SS.mmm as well as HH:MM:SS.mmm. YouTube always emits
    three parts, so this only ever bit non-YouTube sources - with a
    ValueError traceback rather than a message."""
    parts = t.strip().split(':')
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = '0', parts[0], parts[1]
    else:
        raise ValueError(f'unparseable VTT timestamp: {t!r}')
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_vtt(path):
    """Line-based. Whitespace-only separators make blank-line splitting unsafe,
    and YouTube emits ~10ms 'settle' cues that duplicate the previous line."""
    text = Path(path).read_text(encoding='utf-8', errors='ignore')

    # Two caption styles that need opposite handling. YouTube auto-subs ROLL:
    # each cue repeats the previous line and appends the new one, so only the
    # last line is new. Manual captions WRAP one sentence across lines, so
    # every line is content - and taking only the last one silently threw
    # half of every wrapped caption away. pick() prefers the manual track, so
    # that was the common case. Inline <00:00:00.000><c> timing tags appear
    # only in the auto format, which makes them the discriminator.
    rolling = bool(CUE_TIMING.search(text))
    lines = text.split('\n')
    raw, cur = [], None
    for line in lines:
        if '-->' in line:
            if cur:
                raw.append(cur)
            a, rest = line.split('-->')
            cur = {'start': ts_to_sec(a.strip()),
                   'end': ts_to_sec(rest.strip().split(' ')[0]),
                   'body': []}
        elif cur is not None:
            t = TAG.sub('', line).strip()
            if t:
                cur['body'].append(t)
    if cur:
        raw.append(cur)

    cues = [(c['start'], c['body'][-1] if rolling else ' '.join(c['body']))
            for c in raw if c['end'] - c['start'] >= 0.1 and c['body']]
    out, prev = [], None
    for start, text in cues:
        if text and text != prev:
            out.append((start, text))
            prev = text
    return out


def pick_thumbnail_url(j):
    """meta['thumbnail'] is whatever yt-dlp ranked highest, which for plenty
    of videos is a .webp - saved unconditionally as thumbnail.jpg, the file
    section 9 of the schema has to open. Prefer a real JPEG, widest first."""
    best = None
    for t in (j.get('thumbnails') or []):
        u = (t.get('url') or '')
        if '.jpg' in u.lower() or '.jpeg' in u.lower():
            key = (t.get('width') or 0, t.get('preference') or 0)
            if best is None or key > best[0]:
                best = (key, u)
    return best[1] if best else j.get('thumbnail')


def fetch_thumbnail(j, meta, thumb):
    url = pick_thumbnail_url(j)
    if not url:
        log('   ! no thumbnail URL in the metadata - section 9 has no image')
        return
    tmp = thumb.with_suffix('.download')
    try:
        import urllib.request
        req = urllib.request.Request(
            url, headers={'User-Agent': 'Mozilla/5.0 (watch-video)'})
        with urllib.request.urlopen(req, timeout=30) as r:
            tmp.write_bytes(r.read())
    except Exception as e:
        log(f'   ! thumbnail download failed: {e}')
        tmp.unlink(missing_ok=True)
        return
    # Trust the bytes, not the extension. A .webp renamed .jpg may simply
    # not open. Normalising costs one cheap ffmpeg call and is a no-op when
    # the file is already a JPEG.
    if tmp.read_bytes()[:3] == b'\xff\xd8\xff':
        tmp.replace(thumb)
        return
    p = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                        '-y', '-i', str(tmp), str(thumb)],
                       capture_output=True, text=True, errors='ignore')
    tmp.unlink(missing_ok=True)
    if p.returncode != 0 or not thumb.exists():
        log(f'   ! thumbnail was not a JPEG and would not convert: '
            f'{(p.stderr or "").strip()[:120]}')
    else:
        log('   thumbnail converted to JPEG')


def step_transcript(d, url, vid_hint=None):
    subs = sorted(d.glob('*.en*.vtt'))
    info = next(iter(d.glob('*.info.json')), None)
    if not subs or not info:
        if not url:
            sys.exit('FATAL: --slug was given with no URL, but this folder '
                     'has no cached transcript or metadata to reuse:\n  '
                     f'{d}\nPass the URL as well.')
        log('[1/6] transcript + metadata')
        # SUB_LANGS is deliberately explicit. 'en.*' also matches YouTube's
        # auto-TRANSLATED tracks (en-de-DE, en-fr-FR, ...), so it fired off
        # ~100 subtitle requests per video and reliably drew HTTP 429 - which
        # then aborted the whole run before info.json was even written.
        p = run_soft(ytdlp(
            ['--skip-download', '--write-auto-subs', '--write-subs',
             '--sub-langs', SUB_LANGS, '--sub-format', 'vtt',
             '--ignore-errors', '--write-info-json',
             '-o', str(d / '%(id)s'), '--', url]))
        subs = sorted(d.glob('*.en*.vtt'))
        info = next(iter(d.glob('*.info.json')), None)
        # Judge on what landed, not on the exit code: a single failed
        # subtitle variant must not kill a run whose captions are all here.
        if not info or not subs:
            tail = [l for l in (p.stderr or '').strip().split('\n') if l.strip()]
            if p.returncode != 0:
                sys.exit('FATAL: yt-dlp could not fetch the transcript or '
                         'metadata.\n' + '\n'.join(tail[-6:] or ['(no output)'])
                         + remediation(tail))
    else:
        log('[1/6] transcript + metadata (cached)')

    if not info:
        sys.exit('FATAL: no info JSON produced. Check the URL.')
    if not subs:
        sys.exit('FATAL: no English captions exist for this video. '
                 'Whisper is not part of this pipeline. Stopping.')

    # Prefer the manual track over the auto one when both exist
    pick = next((s for s in subs if '.en.' in s.name), subs[0])
    cues = parse_vtt(pick)
    (d / 'data' / 'transcript.txt').write_text(
        '\n'.join(f'[{mmss(s)}] {t}' for s, t in cues), encoding='utf-8')

    j = json.loads(info.read_text(encoding='utf-8'))
    return finish_transcript(d, j, url, cues)


def build_meta(j, cues):
    meta = {k: j.get(k) for k in
            ('id', 'title', 'channel', 'channel_follower_count', 'duration',
             'view_count', 'like_count', 'comment_count', 'upload_date',
             'webpage_url', 'description', 'thumbnail', 'tags', 'chapters',
             'fps', 'width', 'height',
             # channel_url feeds the baseline fetch in channel_baseline()
             'channel_id', 'channel_url', 'uploader', 'uploader_url')}
    meta['duration_mmss'] = mmss(j.get('duration') or 0)
    # words/wpm were computed for one log line and thrown away; the index
    # wants them, and recomputing means re-parsing the VTT.
    words = sum(len(t.split()) for _, t in cues)
    dur = j.get('duration') or 0
    meta['words'] = words
    meta['wpm'] = round(words / (dur / 60)) if dur else None
    return meta


def finish_transcript(d, j, url, cues):
    # slugify() keeps the first seven title words, so two different videos
    # with similar titles land in one folder and silently reuse each other's
    # frames, cuts and audio. The live corpus already has near-misses. Refuse
    # rather than merge; the operator picks a slug.
    want = url_video_id(url)
    if want and j.get('id') and j['id'] != want:
        sys.exit(f'FATAL: slug collision. This folder already holds a '
                 f'different video.\n'
                 f'  folder    : {d}\n'
                 f'  cached    : {j.get("id")}  {j.get("title")}\n'
                 f'  requested : {want}\n'
                 f'Re-run with an explicit --slug for the new video.')

    meta = build_meta(j, cues)
    (d / 'data' / 'meta.json').write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')

    # Thumbnail is required by the packaging section of the schema
    thumb = d / 'data' / 'thumbnail.jpg'
    if not thumb.exists():
        fetch_thumbnail(j, meta, thumb)

    log(f'   {len(cues)} cues, {meta["words"]} words, '
        f'{meta["wpm"] if meta["wpm"] is not None else "?"} wpm')
    return meta


# --------------------------------------------------------------- download ----
def find_source(d):
    return next((p for p in sorted(d.glob('source.*'))
                 if p.suffix.lower() not in ('.txt', '.json')), None)


def source_still_needed(d):
    """The source is deleted once frames and audio succeed. On a re-run, only
    re-download if something that needs it is actually missing."""
    cuts = d / 'data' / 'cuts.csv'
    if not cuts.exists() or not (d / 'data' / 'loudness_m.txt').exists():
        return True
    rows = list(csv.DictReader(open(cuts, encoding='utf-8')))
    if not rows:
        return True
    return any(not (d / 'frames' /
                    f"{int(r['index']):03d}_{r['timestamp_mmss'].replace(':', '')}.jpg").exists()
               for r in rows)


def step_download(d, url, no_download):
    src = find_source(d)
    if src:
        log(f'[2/6] source (cached: {src.name})')
        return src
    if not source_still_needed(d):
        log('[2/6] source not needed - frames and audio already complete')
        return None
    if no_download:
        sys.exit('FATAL: --no-download given but no source file found.')
    log('[2/6] downloading 720p source')
    # av01 first: some 720p renditions are AV1-only and decode slowly (or not
    # at all) in older ffmpeg builds. Final bare 'b' so a video with no <=720p
    # rendition degrades to whatever exists instead of erroring out.
    run(ytdlp(['-f', 'bv*[height<=720][vcodec!*=av01]+ba/'
               'bv*[height<=720]+ba/b[height<=720]/b',
               '-o', str(d / 'source.%(ext)s'), '--', url]))
    src = find_source(d)
    if not src:
        sys.exit('FATAL: download produced no source file.')
    return src


# ----------------------------------------------------------------- cuts ------
def detect(src, threshold, start, dur):
    cmd = ['ffmpeg', '-hide_banner']
    if start:
        cmd += ['-ss', f'{start:.3f}']
    if dur:
        cmd += ['-t', f'{dur:.3f}']
    cmd += ['-i', str(src), '-filter:v',
            f"select='gt(scene,{threshold})',showinfo", '-f', 'null', '-']
    # Must not swallow a failure. A corrupt or half-downloaded source makes
    # ffmpeg exit non-zero and findall return nothing, which used to sail
    # through and report ZERO CUTS as a real measurement. Silently wrong data
    # is worse than a crash for a tool whose whole pitch is honest numbers.
    p = ff(cmd, f'cut detection at threshold {threshold}')
    return sorted({round(float(m) + start, 3) for m in PTS.findall(p.stderr)})


def merge_shots(times, window=MERGE_WINDOW):
    """Collapse detections closer than `window`. High-motion animation trips
    scene detection repeatedly inside one continuous shot; merging is what
    turns raw detections into an honest shot count."""
    if not times:
        return []
    out = [times[0]]
    for t in times[1:]:
        if t - out[-1] >= window:
            out.append(t)
    return out


def ssim_pair(a, b):
    """Dead-zone enrichment only, so a failure degrades rather than exits -
    but it must say so. A silently missing score used to read as 'no score'
    and quietly downgrade a dead-zone verdict."""
    p = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(a), '-i', str(b),
                        '-lavfi', 'ssim', '-f', 'null', '-'],
                       capture_output=True, text=True, errors='ignore')
    m = re.search(r'All:([0-9.]+)', p.stderr)
    if m:
        return float(m.group(1))
    log(f'   ! ssim failed on {a.name} vs {b.name} '
        f'(exit {p.returncode}) - section scored without it')
    return None


def probe_duration(src):
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'default=nk=1:nw=1', str(src)],
                       capture_output=True, text=True, errors='ignore')
    try:
        return float((p.stdout or '').strip())
    except ValueError:
        return 0.0


def resolve_window(meta, start, end, src=None):
    """Metadata duration is absent or zero for livestreams and some
    extractors, and falling through with duration 0 raised ZeroDivisionError
    on merged-shots-per-minute. --end past the real duration was equally bad:
    it generated fill timestamps past EOF that extracted nothing and stranded
    the run with frames_ok false and the source kept forever."""
    duration = float(meta.get('duration') or 0)
    if duration <= 0 and src:
        duration = probe_duration(src)
    win_start = max(0.0, float(start or 0.0))
    win_end = float(end) if end else duration
    if duration > 0:
        win_end = min(win_end, duration)
    return win_start, win_end, duration


def invalidate_if_params_changed(d, win_start, win_end, threshold_override):
    """cuts.csv used to be reused whenever it existed, with no check that the
    window or threshold matched the run that produced it. So --hook-only after
    a full run silently reported full-run pacing, and a full run after
    --hook-only reported 60 seconds as the whole video. Both flags are
    documented in the README, so both were reachable in a demo.

    Runs before step_download so a discarded cache re-downloads its source."""
    cuts_csv = d / 'data' / 'cuts.csv'
    pacing_file = d / 'data' / 'pacing.json'
    if not cuts_csv.exists():
        return

    reasons = []
    if not pacing_file.exists():
        reasons.append('pacing.json is missing')
    else:
        try:
            prev = json.loads(pacing_file.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            prev, reasons = {}, ['pacing.json is unreadable']
        prev_win = prev.get('window')
        if (not prev_win or [round(float(x), 3) for x in prev_win]
                != [round(win_start, 3), round(win_end, 3)]):
            reasons.append(f'window {prev_win} -> '
                           f'[{round(win_start, 3)}, {round(win_end, 3)}]')
        if (threshold_override is not None
                and prev.get('threshold_used') != threshold_override):
            reasons.append(f'threshold {prev.get("threshold_used")} -> '
                           f'{threshold_override}')
    if not reasons:
        return

    log('   ! cached run does not match this one: ' + '; '.join(reasons))
    log('   ! discarding cached frames, sheets, pacing and audio - re-detecting')
    for p in list((d / 'frames').glob('*.jpg')) + list((d / 'sheets').glob('*.jpg')):
        p.unlink(missing_ok=True)
    for name in ('cuts.csv', 'pacing.json', 'sheet_index.csv',
                 'low_cut_sections.csv', 'audio_energy.csv',
                 'audio_cuts_aligned.csv', 'audio_summary.json',
                 'loudness_m.txt'):
        (d / 'data' / name).unlink(missing_ok=True)


def step_cuts(d, src, meta, threshold_override, start, end):
    win_start, win_end, duration = resolve_window(meta, start, end, src)
    win_dur = win_end - win_start
    if win_dur <= 0:
        sys.exit(f'FATAL: nothing to analyse - the window resolves to '
                 f'{win_start:.1f}s..{win_end:.1f}s '
                 f'(duration {duration:.1f}s).\n'
                 f'Check --start / --end, or the source if this is a '
                 f'livestream with no reported duration.')

    cuts_csv = d / 'data' / 'cuts.csv'
    if cuts_csv.exists():
        log('[3/6] cut detection (cached)')
        rows = list(csv.DictReader(open(cuts_csv, encoding='utf-8')))
        return json.loads((d / 'data' / 'pacing.json').read_text(
            encoding='utf-8')), rows

    log('[3/6] cut detection')

    def pass_at(threshold):
        """Detect, then seed the opening frame before merging so raw and merged
        counts stay consistent. A hard cut at the window start is never detected."""
        r = detect(src, threshold, win_start, win_dur)
        if not r or r[0] > win_start + 1.0:
            r.insert(0, win_start)
        return r, merge_shots(r)

    tried = []
    if threshold_override:
        thr = threshold_override
        raw, merged = pass_at(thr)
        tried.append((thr, len(raw), len(merged)))
    else:
        thr = BASE_THRESHOLD
        raw, merged = pass_at(thr)
        tried.append((thr, len(raw), len(merged)))
        # Only lower if we are genuinely missing cuts. Never to hit a target.
        for nxt in THRESHOLD_LADDER:
            if len(merged) / (win_dur / 60) >= MIN_MERGED_PER_MIN:
                break
            log(f'   {len(merged) / (win_dur / 60):.1f} merged/min < '
                f'{MIN_MERGED_PER_MIN} -> retry at {nxt}')
            thr = nxt
            raw, merged = pass_at(thr)
            tried.append((thr, len(raw), len(merged)))

    # Cuts inside screen recordings score under 0.20 - window chrome and
    # background barely change across them - so those videos under-count.
    # Always compute a 0.12 reference now, while the source still exists, so
    # the analysis can report a range without re-downloading.
    if abs(thr - 0.12) < 1e-6:
        sec_merged = merged
    else:
        _, sec_merged = pass_at(0.12)
    secondary = {'threshold': 0.12,
                 'merged_shots': len(sec_merged),
                 'merged_shots_per_min': round(len(sec_merged) / (win_dur / 60), 1)}

    shots = set(merged)

    # Uniform fill. Cut detection is blind to slow morphing motion graphics,
    # so long gaps are sampled densely rather than trusted as static.
    low_cut, filled = [], list(merged)
    bounds = merged + [win_end]
    for a, b in zip(bounds, bounds[1:]):
        if b - a > GAP_LIMIT:
            low_cut.append([a, b])
            t = a + FILL_EVERY
            while t < b - 1.0:
                filled.append(round(t, 3))
                t += FILL_EVERY
    filled = sorted(set(filled))

    with open(cuts_csv, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['index', 'timestamp_sec', 'timestamp_mmss', 'source'])
        for i, t in enumerate(filled, 1):
            w.writerow([i, f'{t:.3f}', mmss(t), 'shot' if t in shots else 'fill'])
    rows = list(csv.DictReader(open(cuts_csv, encoding='utf-8')))

    per_min = len(merged) / (win_dur / 60)
    gaps = [b - a for a, b in zip(merged, merged[1:])]
    pacing = {
        'threshold_used': thr,
        'threshold_attempts': [{'threshold': t, 'raw': r, 'merged': m} for t, r, m in tried],
        'window': [win_start, win_end],
        'raw_detections': len(raw),
        'merged_shots': len(merged),
        'merged_shots_per_min': round(per_min, 1),
        'raw_detections_per_min': round(len(raw) / (win_dur / 60), 1),
        'secondary_012': secondary,
        'in_band': BAND[0] <= per_min <= BAND[1],
        'band': list(BAND),
        'median_shot_sec': round(st.median(gaps), 2) if gaps else None,
        'mean_shot_sec': round(st.mean(gaps), 2) if gaps else None,
        'longest_shot_sec': round(max(gaps), 1) if gaps else None,
        'total_frames': len(filled),
        'fill_frames': len(filled) - len(merged),
        'low_cut_sections': [],
    }
    buckets = {}
    for t in merged:
        buckets[int((t - win_start) // 60)] = buckets.get(int((t - win_start) // 60), 0) + 1
    praw = {}
    for t in raw:
        praw[int((t - win_start) // 60)] = praw.get(int((t - win_start) // 60), 0) + 1
    pacing['buckets'] = [
        {'bucket': f'{mmss(win_start + b * 60)}-{mmss(min(win_start + (b + 1) * 60, win_end))}',
         'merged': buckets.get(b, 0), 'raw': praw.get(b, 0)}
        for b in range(int(win_dur // 60) + 1)]
    pacing['low_cut_raw'] = low_cut
    (d / 'data' / 'pacing.json').write_text(json.dumps(pacing, indent=2), encoding='utf-8')

    log(f'   threshold {thr}  raw {len(raw)}  merged {len(merged)} '
        f'({per_min:.1f}/min)  fills {len(filled) - len(merged)}  total {len(filled)}')
    log(f'   0.12 reference: {secondary["merged_shots"]} merged '
        f'({secondary["merged_shots_per_min"]}/min) - use as the upper end of the '
        f'range if section 7 shows this is screen-recording heavy')
    if not pacing['in_band']:
        log(f'   ! OUTSIDE BAND {BAND[0]}-{BAND[1]} merged/min. Reported, not chased.')
    return pacing, rows


# Measured, not guessed: a 3x3 sheet at 1568px costs ~3,275 image tokens and
# a full-resolution 720p frame ~1,229. The survey pass reads every sheet; the
# microscope pass reads 30-60 individual frames on top.
TOK_SHEET = 3275
TOK_FRAME = 1229
MICROSCOPE = 45
# Measured, not assumed. The largest run to date: a 36:07 video produced 469
# frames and 53 sheets, costing ~173K image tokens for the survey pass plus
# ~55K for 45 microscope frames - about 228K, which fits but sits at the very
# top of the documented 150-250K band. 500 is just above the proven point;
# past it we are extrapolating, and the warning says so.
FRAME_CEILING = 500


def project_cost(n_frames):
    """State the number and let the human decide - never silently sample down.

    Deliberately does NOT prompt. This script is normally run by Claude
    through a shell, where blocking on stdin hangs the session with no way to
    answer. A loud warning that names the alternative is the useful form."""
    sheets = (n_frames + PER - 1) // PER
    tokens = sheets * TOK_SHEET + MICROSCOPE * TOK_FRAME
    log(f'   projected read cost: {n_frames} frames -> {sheets} sheets '
        f'~= {tokens // 1000}K image tokens for the survey pass, plus '
        f'~{MICROSCOPE} microscope frames')
    if n_frames > FRAME_CEILING:
        log(f'   !! {n_frames} frames is past the largest tested run '
            f'(469 frames / 36:07 / ~228K image tokens). One session may '
            f'not hold this.\n'
            f'   !! Analyse it in parts instead:\n'
            f'   !!   --hook-only              the first 60s\n'
            f'   !!   --start 0 --end 15:00    a fifteen-minute window\n'
            f'   !! Nothing has been reduced. Every frame is on disk if you '
            f'continue anyway.')
    return sheets, tokens


def classify_low_cut(d, pacing, rows):
    """A low-cut section is only a true dead zone if its fill frames come back
    visually near-identical. Otherwise it is dense animation the detector
    cannot see, which is the opposite of dead."""
    out_csv = d / 'data' / 'low_cut_sections.csv'
    header = ('start_sec,end_sec,start_mmss,end_mmss,length_sec,'
              'mean_ssim,verdict\n')

    # Two different states, told apart by key PRESENCE, never by truthiness.
    # A completed run pops low_cut_raw and rewrites pacing.json, so every
    # re-run reads back a cached pacing with no such key. Reading that as
    # "this video has no low-cut sections" overwrote a populated CSV with a
    # bare header - and low_cut_sections.csv is the first file SKILL.md reads.
    if 'low_cut_raw' not in pacing:
        if not out_csv.exists():
            out_csv.write_text(header, encoding='utf-8')
        return

    if not pacing['low_cut_raw']:
        out_csv.write_text(header, encoding='utf-8')
        pacing.pop('low_cut_raw', None)
        (d / 'data' / 'pacing.json').write_text(
            json.dumps(pacing, indent=2), encoding='utf-8')
        return

    frames = d / 'frames'
    out = []
    for a, b in pacing['low_cut_raw']:
        inside = [r for r in rows if a <= float(r['timestamp_sec']) <= b]
        paths = [frames / f"{int(r['index']):03d}_{r['timestamp_mmss'].replace(':', '')}.jpg"
                 for r in inside]
        paths = [p for p in paths if p.exists()]
        scores = [s for s in (ssim_pair(x, y) for x, y in zip(paths, paths[1:]))
                  if s is not None]
        mean = round(st.mean(scores), 4) if scores else None
        verdict = ('dead zone' if mean is not None and mean >= DEAD_SSIM
                   else 'low-cut, visually active')
        out.append([f'{a:.3f}', f'{b:.3f}', mmss(a), mmss(b), f'{b - a:.1f}',
                    mean if mean is not None else '', verdict])
    with open(out_csv, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['start_sec', 'end_sec', 'start_mmss', 'end_mmss',
                    'length_sec', 'mean_ssim', 'verdict'])
        w.writerows(out)
    pacing['low_cut_sections'] = [
        {'start_mmss': r[2], 'end_mmss': r[3], 'length_sec': float(r[4]),
         'mean_ssim': r[5], 'verdict': r[6]} for r in out]
    pacing.pop('low_cut_raw', None)
    (d / 'data' / 'pacing.json').write_text(json.dumps(pacing, indent=2), encoding='utf-8')
    for r in out:
        log(f'   {r[2]}-{r[3]} ({r[4]}s) ssim={r[5]} -> {r[6]}')


# --------------------------------------------------------------- frames ------
def step_frames(d, src, rows):
    frames = d / 'frames'
    frames.mkdir(exist_ok=True)
    made = skipped = 0
    for r in rows:
        t = float(r['timestamp_sec'])
        seek = t + 0.10 if r['source'] == 'shot' else t
        out = frames / f"{int(r['index']):03d}_{r['timestamp_mmss'].replace(':', '')}.jpg"
        if out.exists() and out.stat().st_size > 0:
            skipped += 1
            continue
        ff(['ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-ss', f'{seek:.3f}', '-i', str(src),
            '-frames:v', '1', '-q:v', '3', '-y', str(out)],
           f'frame extraction at {r["timestamp_mmss"]}')
        made += 1
    log(f'[4/6] frames: {made} new, {skipped} reused, {len(rows)} total')
    return made + skipped == len(rows)


# ---------------------------------------------------------------- font -------
def font_candidates():
    """Bold sans, per platform, most-likely-present first. Windows comes from
    %WINDIR% rather than a hardcoded C: so a non-C: install still works."""
    system = platform.system()
    win = Path(os.environ.get('WINDIR') or 'C:/Windows') / 'Fonts'
    windows = [str(win / n) for n in
               ('arialbd.ttf', 'segoeuib.ttf', 'calibrib.ttf', 'arial.ttf')]
    # Real single-face .ttf first; Helvetica.ttc is a collection and drawtext
    # handles collections less predictably, so it is the last resort.
    mac = ['/System/Library/Fonts/Supplemental/Arial Bold.ttf',
           '/Library/Fonts/Arial Bold.ttf',
           '/System/Library/Fonts/Supplemental/Verdana Bold.ttf',
           '/System/Library/Fonts/Supplemental/Tahoma Bold.ttf',
           '/System/Library/Fonts/Helvetica.ttc']
    # Best-effort only. Linux is not a supported platform for this tool.
    linux = ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
             '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
             '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf']
    return {'Windows': windows + mac + linux,
            'Darwin': mac + linux + windows}.get(system, linux + mac + windows)


# Where each platform keeps fonts. Scanning a directory is a far safer guess
# than naming a file: the explicit list above is filenames on machines we
# cannot test, this is the directory they live in, which barely changes.
FONT_DIRS = {
    'Darwin': ['/System/Library/Fonts/Supplemental', '/Library/Fonts',
               '/System/Library/Fonts', '~/Library/Fonts'],
    'Linux': ['/usr/share/fonts', '/usr/local/share/fonts',
              '~/.local/share/fonts', '~/.fonts'],
    'Windows': [],   # %WINDIR%\Fonts is already covered explicitly
}
# Ranked. Keeps the scan off symbol and display faces - a bold Wingdings
# would "resolve" and then render the timestamps as gibberish.
FONT_FAMILIES = ('arial', 'helvetica', 'verdana', 'tahoma', 'liberation',
                 'dejavu', 'roboto', 'notosans', 'segoe', 'inter', 'lato',
                 'opensans', 'sourcesans', 'freesans', 'nimbussans')


def scan_font_dirs(system=None):
    """Walk the platform's font directories for a bold face from a known text
    family. This is what makes a Mac with an unexpected font set still work
    instead of failing on a filename guess."""
    hits = []
    for raw in FONT_DIRS.get(system or platform.system(), []):
        base = Path(raw).expanduser()
        if not base.is_dir():
            continue
        try:
            files = [f for ext in ('ttf', 'otf') for f in base.rglob('*.' + ext)]
        except OSError:
            continue
        for f in files:
            name = f.name.lower()
            if 'bold' not in name:
                continue
            flat = re.sub(r'[^a-z]', '', name)
            rank = next((i for i, fam in enumerate(FONT_FAMILIES)
                         if fam in flat), None)
            if rank is not None:
                hits.append((rank, str(f)))
    hits.sort()
    return [p for _, p in hits]


def ff_font_arg(path):
    """drawtext's fontfile= sits inside a filtergraph, where ':' separates
    options - hence the escaped drive-letter colon. Forward slashes and the
    surrounding single quotes in step_sheets cover backslashes and spaces."""
    return str(path).replace('\\', '/').replace(':', r'\:')


def resolve_font(explicit=None):
    """Returns (ffmpeg-ready path, list of paths tried). None if none exist.

    An explicit --font that does not exist is an error, never a reason to
    fall back to a platform default: silently ignoring the flag would hide
    a typo behind a sheet that renders in the wrong typeface."""
    if explicit:
        if Path(explicit).is_file():
            return ff_font_arg(explicit), [explicit]
        return None, [explicit]
    tried = []
    for cand in font_candidates():
        tried.append(cand)
        if Path(cand).is_file():
            return ff_font_arg(cand), tried
    # Named files all missed. Fall back to scanning the font directories,
    # which is the guess that survives an unfamiliar machine.
    for found in scan_font_dirs():
        return ff_font_arg(found), tried + [f'(scan found {found})']
    return None, tried + [f'(scanned {FONT_DIRS.get(platform.system(), [])})']


# --------------------------------------------------------------- sheets ------
def esc(s):
    return s.replace(':', r'\:')


def step_sheets(d, rows):
    sheets = d / 'sheets'
    sheets.mkdir(exist_ok=True)
    frames = d / 'frames'
    files = []
    for r in rows:
        p = frames / f"{int(r['index']):03d}_{r['timestamp_mmss'].replace(':', '')}.jpg"
        if p.exists():
            files.append((p, int(r['index']), r['timestamp_mmss'], r['source']))

    n = (len(files) + PER - 1) // PER
    index_rows = []
    for s in range(n):
        out = sheets / f'sheet_{s + 1:03d}.jpg'
        chunk = files[s * PER:(s + 1) * PER]
        index_rows.append([out.name, f'{chunk[0][2]}-{chunk[-1][2]}',
                           ' '.join(f'{i:03d}={t}' for _, i, t, _ in chunk)])
        if out.exists() and out.stat().st_size > 0:
            continue
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
        for p, _, _, _ in chunk:
            cmd += ['-i', str(p)]
        for _ in range(PER - len(chunk)):
            cmd += ['-f', 'lavfi', '-i', f'color=c=#101010:s={CW}x{CH}']
        parts = []
        for i in range(PER):
            if i < len(chunk):
                _, idx, ts, srctype = chunk[i]
                tag = f'{idx:03d}  {ts}' + ('  ~' if srctype == 'fill' else '')
                parts.append(
                    f'[{i}:v]scale={CW}:{CH}:force_original_aspect_ratio=decrease,'
                    f'pad={CW}:{CH}:(ow-iw)/2:(oh-ih)/2:color=#101010,'
                    f"drawtext=fontfile='{FONT}':text='{esc(tag)}':fontcolor=white:"
                    f'fontsize=21:box=1:boxcolor=black@0.65:boxborderw=5:x=7:y=7[v{i}]')
            else:
                parts.append(f'[{i}:v]scale={CW}:{CH}[v{i}]')
        layout = '|'.join(f'{(i % COLS) * CW}_{(i // COLS) * CH}' for i in range(PER))
        fc = ';'.join(parts) + ';' + ''.join(f'[v{i}]' for i in range(PER)) + \
            f'xstack=inputs={PER}:layout={layout}[out]'
        cmd += ['-filter_complex', fc, '-map', '[out]', '-frames:v', '1',
                '-q:v', '3', '-y', str(out)]
        ff(cmd, f'contact sheet {out.name}')
    with open(d / 'data' / 'sheet_index.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['sheet', 'span_mmss', 'cells'])
        w.writerows(index_rows)
    log(f'[5/6] sheets: {n} from {len(files)} frames')


# ---------------------------------------------------------------- audio ------
def has_audio(src):
    """ebur128 hard-fails on a video with no audio stream. Ask first."""
    p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a',
                        '-show_entries', 'stream=index', '-of', 'csv=p=0',
                        str(src)], capture_output=True, text=True, errors='ignore')
    return bool(p.stdout.strip())


def step_audio(d, src, rows, start, end):
    mfile = d / 'data' / 'loudness_m.txt'
    summary_file = d / 'data' / 'audio_summary.json'

    if not mfile.exists() and src and not has_audio(src):
        # Section 8 is one section, not the deliverable. Complete the run.
        log('[6/6] audio: no audio stream in the source - section 8 unavailable')
        summary_file.write_text(json.dumps(
            {'risers': [], 'beat_alignment_ratio': None,
             'beat_verdict': 'no audio stream in the source',
             'energy_runs': []}, indent=2), encoding='utf-8')
        return True

    if not mfile.exists() and not src:
        log('[6/6] audio: no loudness data and no source to compute it from')
        return False

    if not mfile.exists():
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
        if start:
            cmd += ['-ss', f'{start:.3f}']
        if end:
            cmd += ['-t', f'{end - (start or 0):.3f}']
        # ffmpeg's filter parser splits options on ':', so an absolute Windows
        # path (C:\...) cannot go in file=. Run from data\ and use a bare name.
        cmd += ['-i', str(src), '-af',
                'ebur128=metadata=1,ametadata=print:key=lavfi.r128.M:file=loudness_m.txt',
                '-f', 'null', '-']
        ff(cmd, 'audio loudness (ebur128)', cwd=str(d / 'data'))
    if not mfile.exists() or mfile.stat().st_size == 0:
        log('[6/6] audio: ebur128 produced no loudness data - '
            'section 8 unavailable, source kept for a retry')
        return False

    off = start or 0.0
    txt = mfile.read_text(encoding='utf-8', errors='ignore').split('\n')
    samples, t = [], None
    for line in txt:
        m = re.match(r'frame:\d+\s+pts:\S+\s+pts_time:([0-9.]+)', line)
        if m:
            t = float(m.group(1)) + off
            continue
        m = re.match(r'lavfi\.r128\.M=(-?[0-9.]+)', line)
        if m and t is not None:
            samples.append((t, max(float(m.group(1)), -70.0)))
            t = None
    if not samples:
        log('[6/6] audio: loudness file held no parseable r128 samples - '
            'section 8 unavailable, source kept for a retry')
        return False

    per_sec = {}
    for tt, v in samples:
        per_sec.setdefault(int(tt), []).append(v)
    curve = [(s, round(st.mean(vs), 2)) for s, vs in sorted(per_sec.items())]
    with open(d / 'data' / 'audio_energy.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['timestamp_sec', 'lufs'])
        w.writerows(curve)

    vals = [v for _, v in curve]
    med = st.median(vals)
    lut = dict(curve)

    # Risers: smoothed rise to a high peak, landing in near-silence. Speech
    # alone swings several LU per second, so an unsmoothed test fires constantly.
    sm = []
    for i, (s_, _) in enumerate(curve):
        win = [v for _, v in curve[max(0, i - 1):i + 2]]
        sm.append((s_, st.mean(win)))
    risers, i = [], 1
    while i < len(sm) - 3:
        j = i
        while j < len(sm) - 1 and sm[j + 1][1] >= sm[j][1] - 0.3:
            j += 1
        rise_len, rise_amt = sm[j][0] - sm[i][0], sm[j][1] - sm[i][1]
        if rise_len >= 2 and rise_amt >= 5 and sm[j][1] >= med + 1.5:
            after = [v for s_, v in sm if sm[j][0] < s_ <= sm[j][0] + 3]
            if after and min(after) <= med - 4 and sm[j][1] - min(after) >= 6:
                risers.append({'peak_mmss': mmss(sm[j][0]),
                               'rise_lu': round(rise_amt, 1),
                               'drop_lu': round(sm[j][1] - min(after), 1)})
            i = j + 1
            continue
        i = j + 1 if j > i else i + 1

    raw = {round(tt, 1): v for tt, v in samples}

    def swing(tt):
        a, b = raw.get(round(tt - 0.4, 1)), raw.get(round(tt + 0.4, 1))
        return abs(b - a) if a is not None and b is not None else None

    base = [x for x in (swing(round(tt, 1)) for tt, _ in samples) if x is not None]
    all_delta = st.mean(base) if base else 0.0
    shots = [r for r in rows if r['source'] == 'shot']
    cd, aligned = [], []
    for r in shots:
        tt = float(r['timestamp_sec'])
        dv = swing(round(tt, 1))
        if dv is not None:
            cd.append(dv)
        aligned.append([r['index'], f'{tt:.3f}', r['timestamp_mmss'],
                        lut.get(int(tt), ''), round(dv, 2) if dv is not None else ''])
    with open(d / 'data' / 'audio_cuts_aligned.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['index', 'timestamp_sec', 'timestamp_mmss', 'lufs', 'abs_delta_lufs'])
        w.writerows(aligned)
    ratio = (st.mean(cd) / all_delta) if cd and all_delta else 0.0

    runs, cur = [], None
    for s_, v in curve:
        lab = 'low' if v < med - 3 else ('high' if v > med + 2 else 'mid')
        if cur and cur['label'] == lab:
            cur['end'] = s_
        else:
            if cur and cur['end'] - cur['start'] >= 5:
                runs.append(cur)
            cur = {'label': lab, 'start': s_, 'end': s_}
    if cur and cur['end'] - cur['start'] >= 5:
        runs.append(cur)

    summary = {
        'risers': risers,
        'beat_alignment_ratio': round(ratio, 2),
        'beat_verdict': ('cuts land on loudness inflections (beat-cut)' if ratio >= 1.35
                         else 'cuts are NOT loudness-aligned' if ratio <= 1.1
                         else 'weak / partial alignment'),
        'energy_runs': [{'label': r['label'], 'start_mmss': mmss(r['start']),
                         'end_mmss': mmss(r['end']), 'len_sec': r['end'] - r['start']}
                        for r in runs if r['label'] != 'mid'],
    }
    (d / 'data' / 'audio_summary.json').write_text(
        json.dumps(summary, indent=2), encoding='utf-8')
    log(f'[6/6] audio: {len(risers)} risers, beat ratio {ratio:.2f} '
        f'-> {summary["beat_verdict"]}')
    return True


# ------------------------------------------------------- channel baseline ----
BASELINE_N = 30
BASELINE_MIN_ROWS = 8


def channel_baseline(d, meta, skip=False):
    """Median view count of the channel's recent uploads, excluding this one.

    views/subs is not the number that reasons about performance. A video with
    4.1K views on 710 subs reads as 5.8x by views/subs and 19.5x against its
    own channel's baseline; those disagree by a factor of three and only the
    second means anything.

    Enrichment, never a reason to fail the run: returns None on any problem.
    Deliberately does not use run(), which exits the process."""
    cache = d / 'data' / 'channel_baseline.json'
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    if skip:
        return None
    url = meta.get('channel_url') or meta.get('uploader_url')
    if not url:
        log('   ! no channel URL in the metadata - no channel multiplier')
        return None
    try:
        # The /videos tab specifically: Shorts live under /shorts and are
        # excluded from it, which removes the biggest source of median
        # distortion without any duration filtering.
        p = subprocess.run(
            ytdlp(['--flat-playlist', '--playlist-end', str(BASELINE_N),
                   '--print', '%(id)s|%(view_count)s',
                   '--', url.rstrip('/') + '/videos']),
            capture_output=True, text=True, errors='ignore', timeout=180)
        rows = []
        for line in (p.stdout or '').splitlines():
            vid, _, views = line.strip().partition('|')
            if vid == meta.get('id'):
                continue
            try:
                rows.append(int(views))
            except ValueError:
                continue
        if len(rows) < BASELINE_MIN_ROWS:
            log(f'   ! channel baseline: only {len(rows)} usable uploads '
                f'(need {BASELINE_MIN_ROWS}) - no channel multiplier')
            return None
        out = {'channel_median_views': int(st.median(rows)),
               'sample_size': len(rows),
               'fetched': datetime.date.today().isoformat()}
        cache.write_text(json.dumps(out, indent=2), encoding='utf-8')
        log(f'   channel baseline: median {out["channel_median_views"]:,} '
            f'views over {out["sample_size"]} uploads')
        return out
    except Exception as e:
        log(f'   ! channel baseline failed ({e}) - no channel multiplier')
        return None


# ---------------------------------------------------------------- index ------
def load_index(root):
    p = root / 'index.json'
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        log('   ! index.json is unreadable - starting a new one')
        return []


def build_record(slug, meta, pacing, audio, baseline, analyzed):
    subs = meta.get('channel_follower_count') or 0
    views = meta.get('view_count') or 0
    med = (baseline or {}).get('channel_median_views')
    lows = pacing.get('low_cut_sections') or []
    return {
        'id': meta.get('id'),
        'slug': slug,
        'title': meta.get('title'),
        'channel': meta.get('channel'),
        'channel_id': meta.get('channel_id'),
        'subs': subs,
        'views': views,
        'duration_sec': meta.get('duration'),
        'duration_mmss': meta.get('duration_mmss'),
        'upload_date': meta.get('upload_date'),
        'analyzed': analyzed,
        'views_per_sub': round(views / subs, 2) if subs else None,
        'channel_median_views': med,
        'multiplier_vs_channel': round(views / med, 1) if med else None,
        'merged_shots_per_min': pacing.get('merged_shots_per_min'),
        'secondary_012_per_min': (pacing.get('secondary_012') or {})
                                 .get('merged_shots_per_min'),
        'in_band': pacing.get('in_band'),
        'median_shot_sec': pacing.get('median_shot_sec'),
        'longest_shot_sec': pacing.get('longest_shot_sec'),
        'low_cut_count': len(lows),
        'low_cut_total_sec': round(
            sum(float(s.get('length_sec') or 0) for s in lows), 1),
        'dead_zone_count': sum(1 for s in lows
                               if s.get('verdict') == 'dead zone'),
        'riser_count': len((audio or {}).get('risers') or []),
        'beat_alignment_ratio': (audio or {}).get('beat_alignment_ratio'),
        'beat_verdict': (audio or {}).get('beat_verdict'),
        'words': meta.get('words'),
        'wpm': meta.get('wpm'),
        'total_frames': pacing.get('total_frames'),
        'fill_frames': pacing.get('fill_frames'),
        'takeaway': PENDING,
    }


def index_sort_key(r):
    """Channel multiplier descending. Records without one sort last, ordered
    by views/sub, because a missing baseline is not a low score."""
    m = r.get('multiplier_vs_channel')
    return (0 if m is not None else 1, -(m or 0.0),
            -(r.get('views_per_sub') or 0.0))


def upsert_index(root, record):
    recs = load_index(root)
    for i, r in enumerate(recs):
        if r.get('id') and r.get('id') == record.get('id'):
            # Never clobber a takeaway that has already been written, and
            # keep the date the video was FIRST analysed - re-running the
            # script is not a new analysis, and stamping today over it lost
            # the real dates for the whole corpus the first time this ran.
            record['takeaway'] = r.get('takeaway') or PENDING
            record['analyzed'] = r.get('analyzed') or record['analyzed']
            recs[i] = record
            break
    else:
        recs.append(record)
    write_index(root, recs)
    return recs


def _human(n):
    if not n:
        return '-'
    for div, suf in ((1_000_000, 'M'), (1_000, 'K')):
        if n >= div:
            v = n / div
            return f'{v:.0f}{suf}' if v >= 10 or v == int(v) else f'{v:.1f}{suf}'
    return str(n)


def _date(d8):
    s = str(d8 or '')
    return f'{s[:4]}-{s[4:6]}-{s[6:8]}' if len(s) == 8 else (s or '-')


def _cell(s):
    return str(s if s is not None else '-').replace('|', r'\|').replace('\n', ' ')


MD_ROW = re.compile(r'^\|\s*\[.*?\]\(([^)/]+)/NOTES\.md\)\s*\|(.*)\|\s*$')


def harvest_index_md(root):
    """Recover the two fields a script cannot reproduce from an existing
    INDEX.md before it is regenerated: the hand-written takeaway, and the
    date the video was FIRST analysed. Takeaway is the last cell and analyzed
    the one before it in both the old and the new column layouts."""
    out = {}
    p = root / 'INDEX.md'
    if not p.exists():
        return out
    for line in p.read_text(encoding='utf-8', errors='ignore').splitlines():
        m = MD_ROW.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split('|')]
        if len(cells) < 2:
            continue
        out[m.group(1)] = {
            'takeaway': cells[-1] if cells[-1] not in ('', '-') else None,
            'analyzed': cells[-2] if re.fullmatch(r'\d{4}-\d{2}-\d{2}',
                                                  cells[-2] or '') else None,
        }
    return out


def backfill_index(root):
    """Fold analyses that predate index.json into it, so the corpus is not
    silently reset to n=1 the first time this runs."""
    known = {r.get('slug') for r in load_index(root)}
    folders = [f for f in sorted(root.iterdir())
               if f.is_dir() and (f / 'data' / 'meta.json').exists()
               and f.name not in known]
    if not folders:
        return
    prior = harvest_index_md(root)
    recs = load_index(root)
    for f in folders:
        try:
            meta = json.loads((f / 'data' / 'meta.json').read_text(encoding='utf-8'))
            # Older meta.json predates words/wpm and the channel fields.
            # The info.json is still on disk, so rebuild from it.
            info = next(iter(f.glob('*.info.json')), None)
            if info and meta.get('words') is None:
                cues = []
                tx = f / 'data' / 'transcript.txt'
                if tx.exists():
                    cues = [(0, l.split('] ', 1)[-1])
                            for l in tx.read_text(encoding='utf-8').splitlines() if l]
                meta = build_meta(json.loads(info.read_text(encoding='utf-8')), cues)
                (f / 'data' / 'meta.json').write_text(
                    json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
            pacing = _read_json(f / 'data' / 'pacing.json') or {}
            audio = _read_json(f / 'data' / 'audio_summary.json') or {}
            base = _read_json(f / 'data' / 'channel_baseline.json')
            was = prior.get(f.name) or {}
            analyzed = was.get('analyzed') or datetime.date.fromtimestamp(
                (f / 'data' / 'meta.json').stat().st_mtime).isoformat()
            rec = build_record(f.name, meta, pacing, audio, base, analyzed)
            rec['takeaway'] = was.get('takeaway') or PENDING
            recs.append(rec)
        except Exception as e:
            log(f'   ! could not backfill {f.name}: {e}')
    write_index(root, recs)
    kept = sum(1 for f in folders if (prior.get(f.name) or {}).get('takeaway'))
    log(f'backfilled {len(folders)} existing analyses into index.json '
        f'({kept} takeaways preserved)')


def _read_json(p):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return None


def write_index(root, recs):
    """index.json is the source of truth; INDEX.md is a generated view of it,
    sorted by channel multiplier. The old rule was append-never-rewrite,
    which existed to protect hand-written takeaways - now that the takeaway
    lives in index.json, regenerating is safe and sorting is possible."""
    (root / 'index.json').write_text(
        json.dumps(recs, indent=2, ensure_ascii=False), encoding='utf-8')

    head = ['# Video Analysis Index', '',
            'Generated from `index.json` after every run - do not hand-edit '
            'this file, edit the',
            '`takeaway` field in `index.json` instead. Sorted by channel '
            'multiplier, descending.', '',
            '**Mult (chan) is views / the channel\'s median recent views** - '
            'how far a video beat its',
            'own channel. **Views/Sub** is the cruder views / subscribers. '
            'When they disagree, the',
            'channel multiplier is the one that means something.', '',
            '| Title | Channel | Subs | Views | Mult (chan) | Views/Sub | '
            'Duration | Uploaded | Analyzed | Takeaway |',
            '|---|---|---|---|---|---|---|---|---|---|']
    for r in sorted(recs, key=index_sort_key):
        mult = (f'**{r["multiplier_vs_channel"]}x**'
                if r.get('multiplier_vs_channel') is not None else '-')
        vps = (f'{r["views_per_sub"]}x'
               if r.get('views_per_sub') is not None else '-')
        head.append(
            f'| [{_cell(r.get("title"))}]({r.get("slug")}/NOTES.md) '
            f'| {_cell(r.get("channel"))} | {_human(r.get("subs"))} '
            f'| {(r.get("views") or 0):,} | {mult} | {vps} '
            f'| {_cell(r.get("duration_mmss"))} | {_date(r.get("upload_date"))} '
            f'| {_cell(r.get("analyzed"))} | {_cell(r.get("takeaway"))} |')
    (root / 'INDEX.md').write_text('\n'.join(head) + '\n', encoding='utf-8')


# --------------------------------------------------------------- patterns ----
# Hook map, reproduction cost, segment structure, composition mix, packaging.
# The five that carry transferable design decisions. Deliberately NOT the
# whole NOTES.md: a twelve-video corpus of full notes does not fit in a
# usable context, and the other five sections are per-video reference rather
# than cross-video signal.
PATTERN_SECTIONS = ['2', '3b', '6', '7', '9']
MIN_CORPUS = 6
PROVISIONAL_CORPUS = 12
SECTION_RE = re.compile(r'^#{2,4}\s*(\d+b?)\.\s', re.I)


def slice_notes(text, wanted):
    """Return {section_number: verbatim text} for the wanted sections.

    Real NOTES.md files vary more than the schema implies: '### 3b.' appears
    alongside '## 3b.', titles differ in case, and some files have no
    section 1 at all. So match on the NUMBER and ignore the title. A section
    runs until the next numbered heading; unnumbered '###' sub-headings stay
    inside it, which is what we want."""
    lines = text.splitlines()
    marks = [(i, m.group(1).lower())
             for i, line in enumerate(lines)
             if (m := SECTION_RE.match(line))]
    out = {}
    for idx, (start, num) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        if num in wanted and num not in out:
            out[num] = '\n'.join(lines[start:end]).strip()
    return out


PATTERN_COLS = [
    ('slug', 'slug'), ('multiplier_vs_channel', 'mult'),
    ('views_per_sub', 'v/sub'), ('views', 'views'), ('subs', 'subs'),
    ('duration_sec', 'dur_s'), ('merged_shots_per_min', 'shots/min'),
    ('secondary_012_per_min', '0.12/min'), ('median_shot_sec', 'med_shot'),
    ('longest_shot_sec', 'longest'), ('low_cut_count', 'lowcut_n'),
    ('low_cut_total_sec', 'lowcut_s'), ('dead_zone_count', 'dead'),
    ('riser_count', 'risers'), ('beat_alignment_ratio', 'beat'),
    ('wpm', 'wpm'), ('words', 'words'), ('total_frames', 'frames'),
    ('fill_frames', 'fill'),
]


def build_patterns_input(root):
    """Assemble the corpus into one text file, mechanically. Synthesis is a
    reasoning task and belongs to SKILL.md, not to this script."""
    recs = [r for r in load_index(root) if r.get('slug')]
    complete, incomplete = [], []
    for r in recs:
        (complete if (root / r['slug'] / 'NOTES.md').exists()
         else incomplete).append(r)

    n = len(complete)
    if n < MIN_CORPUS:
        sys.exit(f'FATAL: PATTERNS needs at least {MIN_CORPUS} analysed '
                 f'videos. Found {n}.\n'
                 f'Patterns from fewer than {MIN_CORPUS} are noise dressed '
                 f'as findings.')

    complete.sort(key=index_sort_key)
    out = ['# Patterns input', '',
           f'Corpus of {n} analysed videos, assembled from index.json and '
           f'each NOTES.md.', '']

    if n < PROVISIONAL_CORPUS:
        out += [f'> CORPUS WARNING: {n} videos. Treat every finding as '
                f'provisional.',
                f'> Nothing here is a rule until n >= {PROVISIONAL_CORPUS}.',
                '']

    uploads = sorted(_date(r.get('upload_date')) for r in complete
                     if r.get('upload_date'))
    analyses = sorted(r.get('analyzed') for r in complete if r.get('analyzed'))
    mults = [r['multiplier_vs_channel'] for r in complete
             if r.get('multiplier_vs_channel') is not None]
    no_mult = [r['slug'] for r in complete
               if r.get('multiplier_vs_channel') is None]
    out += ['## Corpus', '',
            f'- videos: {n}',
            f'- uploaded: {uploads[0]} to {uploads[-1]}' if uploads else
            '- uploaded: unknown',
            f'- analysed: {analyses[0]} to {analyses[-1]}' if analyses else
            '- analysed: unknown',
            (f'- channel multiplier: {min(mults)}x to {max(mults)}x '
             f'across {len(mults)} videos' if mults else
             '- channel multiplier: none available')]
    if no_mult:
        out += [f'- NO channel multiplier ({len(no_mult)}), excluded from any '
                f'tercile split: ' + ', '.join(no_mult)]
    out += ['']

    out += ['## Comparison table', '',
            'Sorted by channel multiplier, descending. Videos without one '
            'sort last.', '',
            '| ' + ' | '.join(h for _, h in PATTERN_COLS) + ' |',
            '|' + '---|' * len(PATTERN_COLS)]
    for r in complete:
        out.append('| ' + ' | '.join(
            _cell(r.get(k)) for k, _ in PATTERN_COLS) + ' |')
    out += ['']

    out += ['## Per-video extracts', '',
            'Sections ' + ', '.join(PATTERN_SECTIONS) + ' only, verbatim.', '']
    for r in complete:
        mult = (f'{r["multiplier_vs_channel"]}x'
                if r.get('multiplier_vs_channel') is not None else 'no mult')
        out += ['---', '',
                f'### {r.get("title")}',
                f'`{r["slug"]}` - {r.get("channel")} - {mult} - '
                f'{r.get("views"):,} views - {r.get("duration_mmss")}', '',
                f'**Takeaway:** {r.get("takeaway")}', '']
        found = slice_notes(
            (root / r['slug'] / 'NOTES.md').read_text(
                encoding='utf-8', errors='ignore'), PATTERN_SECTIONS)
        for num in PATTERN_SECTIONS:
            out += [found.get(num, f'_(section {num} missing from NOTES.md)_'),
                    '']

    if incomplete:
        out += ['---', '', '## Incomplete analyses', '',
                'In index.json but with no NOTES.md, so excluded above:', '']
        out += [f'- `{r["slug"]}`' for r in incomplete] + ['']

    p = root / 'PATTERNS-INPUT.md'
    p.write_text('\n'.join(out) + '\n', encoding='utf-8')
    log(f'wrote {p}')
    log(f'  {n} videos, {len(mults)} with a channel multiplier'
        + (f', {len(incomplete)} incomplete' if incomplete else ''))
    if n < PROVISIONAL_CORPUS:
        log(f'  CORPUS WARNING: {n} videos - findings are provisional until '
            f'n >= {PROVISIONAL_CORPUS}')


# ----------------------------------------------------------------- main ------
def gitignored(root):
    """Is this output root actually excluded from version control? Prefer
    git's own answer; fall back to reading .gitignore when git is absent."""
    p = subprocess.run(['git', '-C', str(REPO_ROOT), 'check-ignore', '-q',
                        str(root)], capture_output=True)
    if p.returncode in (0, 1):
        return p.returncode == 0
    gi = REPO_ROOT / '.gitignore'
    if not gi.exists():
        return False
    for line in gi.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and line.strip('/') == root.name:
            return True
    return False


def existing_slug_for(root, vid):
    """Find a folder already holding this video id.

    Creators retitle videos. slugify() derives the folder name from the
    CURRENT title, so a retitled video resolved to a brand-new slug, silently
    re-downloaded, and re-analysed from scratch into a second folder - while
    the README promises a completed video re-runs in under a second. Caught
    by re-running a real analysis: the title had drifted and it took a minute
    and 35 MB instead of being a no-op."""
    if not vid or not root.exists():
        return None
    for meta in sorted(root.glob('*/data/meta.json')):
        try:
            if json.loads(meta.read_text(encoding='utf-8')).get('id') == vid:
                return meta.parent.parent.name
        except (ValueError, OSError):
            continue
    return None


def parse_time(value):
    """SS, MM:SS or HH:MM:SS, with optional decimals. Raw seconds only meant
    reaching for a calculator to name a moment you are looking at."""
    s = str(value).strip()
    parts = s.split(':')
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(
        f'cannot parse time {value!r} - use SS, MM:SS or HH:MM:SS')


def slugify(s):
    s = re.sub(r'[^a-zA-Z0-9]+', '-', (s or '').lower()).strip('-')
    return '-'.join(s.split('-')[:7]) or 'video'


def main():
    ap = argparse.ArgumentParser(description='Video -> frames, sheets, transcript, pacing, audio.')
    ap.add_argument('url', nargs='?', help='YouTube URL')
    ap.add_argument('--slug', help='folder name (default: derived from title)')
    ap.add_argument('--root', default=None,
                    help='output root (default: video-research/ inside the '
                         'repo, which .gitignore excludes)')
    ap.add_argument('--start', type=parse_time, metavar='T',
                    help='analyse from here (SS, MM:SS or HH:MM:SS)')
    ap.add_argument('--end', type=parse_time, metavar='T',
                    help='analyse until here (SS, MM:SS or HH:MM:SS)')
    ap.add_argument('--hook-only', action='store_true', help='first 60s only, dense')
    ap.add_argument('--threshold', type=float, help='override scene threshold')
    ap.add_argument('--no-download', action='store_true', help='reuse existing source')
    ap.add_argument('--keep-source', action='store_true', help='do not delete source when done')
    ap.add_argument('--cookies-from-browser', metavar='BROWSER',
                    help='pass cookies from a local browser to yt-dlp '
                         '(chrome, edge, firefox). Use when YouTube throws '
                         'the bot check. Close the browser first.')
    ap.add_argument('--player-client', metavar='LIST',
                    help='yt-dlp youtube:player_client override, e.g. '
                         'tv,web_safari,mweb. Use when downloads stall or '
                         'return 0 bytes (SABR streaming).')
    ap.add_argument('--update', action='store_true',
                    help='upgrade yt-dlp before running (the only permitted install)')
    ap.add_argument('--font', metavar='PATH',
                    help='bold TTF for contact sheet labels '
                         '(default: auto-detected per platform)')
    ap.add_argument('--check', action='store_true',
                    help='report whether every dependency is present, then exit')
    ap.add_argument('--no-channel-baseline', action='store_true',
                    help='skip the channel median fetch (faster, no multiplier)')
    ap.add_argument('--patterns', action='store_true',
                    help='build PATTERNS-INPUT.md across every analysis, '
                         'then exit. No URL, no network.')
    a = ap.parse_args()

    # Standalone, offline, and needs neither ffmpeg nor a font - so it runs
    # before the dependency gate rather than being blocked by it.
    if a.patterns:
        root = (Path(a.root).resolve() if a.root else DEFAULT_ROOT)
        if not root.exists():
            sys.exit(f'FATAL: no analyses at {root}.')
        backfill_index(root)
        build_patterns_input(root)
        return

    # Before any network or disk work. Everything the pipeline shells out to,
    # plus the font, checked while a fix is still cheap.
    problems, warnings, font = preflight(a.font)

    if a.check:
        log(f'platform : {platform.system()}')
        log(f'python   : {sys.version.split()[0]}  {sys.executable}')
        log(f'yt-dlp   : {" ".join(YTDLP_CMD) if YTDLP_CMD else "NOT FOUND"}')
        log(f'font     : {font or "NOT FOUND"}')
        for tool, what, hint in warnings:
            log(f'WARN  {tool}: {what}\n      fix: {hint}')
        for tool, what, hint in problems:
            log(f'FAIL  {tool}: {what}\n      fix: {hint}')
        if problems:
            sys.exit(f'\n{len(problems)} problem(s). Fix the above and re-run '
                     f'--check.')
        log('\nready.')
        return

    for tool, what, hint in warnings:
        log(f'WARNING: {tool} {what}\n         fix: {hint}')
    if problems:
        sys.exit('FATAL: missing dependencies.\n'
                 + '\n'.join(f'  {t}: {w}\n    fix: {h}'
                             for t, w, h in problems)
                 + '\nRun with --check to re-test.')

    global FONT
    FONT = font

    # Mutate in place: ytdlp() reads these module-level lists on every call.
    if a.cookies_from_browser:
        COOKIES[:] = ['--cookies-from-browser', a.cookies_from_browser]
    if a.player_client:
        EXTRACTOR_ARGS[:] = ['--extractor-args',
                             f'youtube:player_client={a.player_client}']

    if a.update:
        log('upgrading yt-dlp')
        # check=False deliberately: a failed upgrade must not stop a run that
        # would otherwise work (offline, no pip, locked site-packages).
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp'],
                       check=False)

    start, end = a.start, a.end
    if a.hook_only:
        start, end = 0.0, 60.0

    root = (Path(a.root).resolve() if a.root else DEFAULT_ROOT)

    # Nothing may write into git or skill metadata. Checked first, so a root
    # that is about to be refused does not also draw a warning.
    for forbidden in ('.git', '.claude'):
        if forbidden in root.parts:
            sys.exit(f'FATAL: --root {root} is inside {forbidden}/. '
                     f'Pick somewhere else.')

    # Output inside the repo means .gitignore is the ONLY thing keeping tens
    # of MB per video out of a commit. Check it rather than assume it: this
    # replaced a guard that made the separation physical.
    if (REPO_ROOT / '.git').exists() and not gitignored(root):
        log(f'WARNING: {root.name}/ is not covered by .gitignore, and one '
            f'analysis is 8-35 MB.\n'
            f'         Add this line to {REPO_ROOT / ".gitignore"}\n'
            f'           /{root.name}/')

    # REPO_ROOT is parents[4], which assumes the skill still sits at
    # <repo>/.claude/skills/watch-video/scripts/. Copying the skill folder
    # into another project - the most likely thing a viewer does after the
    # video - silently relocates the output root somewhere arbitrary. Warn,
    # do not fail: an explicit --root is a legitimate reason to be here.
    if not a.root and not (REPO_ROOT / '.claude' / 'skills' / 'watch-video').is_dir():
        log(f'WARNING: this does not look like the repo root: {REPO_ROOT}\n'
            f'         output will land in {root}\n'
            f'         if that is wrong, pass --root explicitly.')

    created = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    if created:
        log(f'created output root {root}')

    # INDEX.md is now generated from index.json, so there is no template to
    # seed - but analyses made before index.json existed still have to end up
    # in the corpus, or the pattern pass would see one video.
    backfill_index(root)

    slug = a.slug
    if not slug:
        if not a.url:
            sys.exit('FATAL: need --slug when no URL is given.')
        # Match on video id before falling back to the title, so a retitled
        # video re-runs into its existing folder. Also skips a network call.
        slug = existing_slug_for(root, url_video_id(a.url))
        if slug:
            log(f'matched an existing analysis by video id: {slug}')
        else:
            t = run(ytdlp(['--skip-download', '--print', '%(title)s',
                           '--', a.url])).stdout.strip()
            slug = slugify(t.split('\n')[-1])
    d = root / slug
    for sub in ('frames', 'sheets', 'data'):
        (d / sub).mkdir(parents=True, exist_ok=True)
    log(f'== {slug}\n== {d}')

    meta = step_transcript(d, a.url)

    # Before the download, so a discarded cache re-fetches what it needs.
    prov_start, prov_end, prov_dur = resolve_window(meta, start, end)
    if prov_dur > 0:
        invalidate_if_params_changed(d, prov_start, prov_end, a.threshold)
    else:
        log('   ! metadata reports no duration - skipping the cache '
            'parameter check; the window resolves after download')

    src = step_download(d, a.url, a.no_download)
    if a.hook_only:
        log('   --hook-only: analysing 0:00-1:00')

    # Authoritative window: src is available now, so a missing metadata
    # duration can fall back to ffprobe and --end can be clamped to it.
    win_start, win_end, _ = resolve_window(meta, start, end, src)

    pacing, rows = step_cuts(d, src, meta, a.threshold, win_start, win_end)
    project_cost(len(rows))
    frames_ok = step_frames(d, src, rows)
    classify_low_cut(d, pacing, rows)
    step_sheets(d, rows)
    audio_ok = step_audio(d, src, rows, win_start, win_end)

    if src and src.exists():
        if frames_ok and audio_ok and not a.keep_source:
            size = src.stat().st_size / 1e6
            src.unlink()
            log(f'   source deleted ({size:.0f} MB) - frames and audio both succeeded')
        elif not a.keep_source:
            failed = [n for n, ok in (('frames', frames_ok), ('audio', audio_ok))
                      if not ok]
            log(f'   source KEPT - {" and ".join(failed)} did not fully '
                f'succeed. Re-run to retry; the source will not re-download.')

    baseline = channel_baseline(d, meta, skip=a.no_channel_baseline)
    audio = _read_json(d / 'data' / 'audio_summary.json') or {}
    rec = build_record(slug, meta, pacing, audio, baseline,
                       datetime.date.today().isoformat())
    upsert_index(root, rec)
    log(f'   index: {root / "index.json"}  '
        f'(takeaway is {rec["takeaway"]!r})')

    log('\n---- READY FOR ANALYSIS ----')
    log(f'sheets  : {d / "sheets"}')
    log(f'frames  : {d / "frames"}')
    log(f'pacing  : merged {pacing["merged_shots"]} shots, '
        f'{pacing["merged_shots_per_min"]}/min '
        f'(raw detections {pacing["raw_detections"]})')
    if not pacing['in_band']:
        log(f'          OUTSIDE band {BAND[0]}-{BAND[1]}/min - report it, do not chase it')


if __name__ == '__main__':
    main()
