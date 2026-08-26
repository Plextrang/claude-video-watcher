"""
watch-video pipeline: URL -> frames, contact sheets, transcript, pacing, audio.

Everything up to analysis. Analysis + NOTES.md schema live in SKILL.md.
Local only: yt-dlp, ffmpeg, Python stdlib. No pip installs, no APIs.

Windows-first. All paths are pathlib objects passed to subprocess as list args,
so spaces in paths never touch a shell.
"""
import argparse
import csv
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
YTDLP_CMD = []     # resolved once by resolve_ytdlp(): module or binary
JS_RUNTIME = []    # ['--js-runtimes', 'node'] when this yt-dlp supports it

# The two yt-dlp failures that are recoverable but not guessable.
BOT_HINTS = ('confirm you', 'not a bot', 'sign in to confirm')
SABR_HINTS = ('sabr', 'requested format is not available',
              'fragment', 'missing a url')

# The repo ships the tool only. Every analysis lands in a sibling folder outside
# it, so no analysis output can ever end up inside version control.
#   .../<parent>/<repo>/.claude/skills/watch-video/scripts/watch_video.py
#   parents[4] == <repo>, so the sibling is parents[4].parent / 'video-research'
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ROOT = REPO_ROOT.parent / 'video-research'


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
        run(ytdlp(['--skip-download', '--write-auto-subs', '--write-subs',
                   '--sub-langs', 'en.*', '--sub-format', 'vtt',
                   '--write-info-json', '-o', str(d / '%(id)s'), '--', url]))
        subs = sorted(d.glob('*.en*.vtt'))
        info = next(iter(d.glob('*.info.json')), None)
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

    meta = {k: j.get(k) for k in
            ('id', 'title', 'channel', 'channel_follower_count', 'duration',
             'view_count', 'like_count', 'comment_count', 'upload_date',
             'webpage_url', 'description', 'thumbnail', 'tags', 'chapters',
             'fps', 'width', 'height')}
    meta['duration_mmss'] = mmss(j.get('duration') or 0)
    (d / 'data' / 'meta.json').write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')

    # Thumbnail is required by the packaging section of the schema
    thumb = d / 'data' / 'thumbnail.jpg'
    if not thumb.exists():
        fetch_thumbnail(j, meta, thumb)

    words = sum(len(t.split()) for _, t in cues)
    dur = meta.get('duration') or 1
    log(f'   {len(cues)} cues, {words} words, {words / (dur / 60):.0f} wpm')
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
    return None, tried


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


# ----------------------------------------------------------------- main ------
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
                    help=f'output root (default: {DEFAULT_ROOT}, a sibling of the repo)')
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
    a = ap.parse_args()

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
    # Analysis output must never land inside the repo. Catch a bad --root early,
    # before a download writes 30 MB somewhere it will get committed.
    try:
        inside_repo = root == REPO_ROOT or REPO_ROOT in root.parents
    except Exception:
        inside_repo = False
    if inside_repo:
        sys.exit(f'FATAL: --root {root} is inside the repo ({REPO_ROOT}).\n'
                 f'Analysis output belongs outside version control. '
                 f'Default is {DEFAULT_ROOT}.')
    # REPO_ROOT is parents[4], which assumes the skill still sits at
    # <repo>/.claude/skills/watch-video/scripts/. Copying the skill folder
    # into another project - the most likely thing a viewer does after the
    # video - silently relocates the output root somewhere arbitrary. Warn,
    # do not fail: an explicit --root is a legitimate reason to be here.
    if not a.root and not (REPO_ROOT / 'INDEX.template.md').exists():
        log(f'WARNING: this does not look like the repo root: {REPO_ROOT}\n'
            f'         output will land in {root}\n'
            f'         if that is wrong, pass --root explicitly.')

    created = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    if created:
        log(f'created output root {root}')

    # A fresh clone has no live index. Seed it from the repo's template.
    index = root / 'INDEX.md'
    tmpl = REPO_ROOT / 'INDEX.template.md'
    if not index.exists() and tmpl.exists():
        txt = re.sub(r'\nScaffold only\..*?\n\n', '\n',
                     tmpl.read_text(encoding='utf-8'), flags=re.S)
        index.write_text(txt, encoding='utf-8')
        log(f'seeded {index} from INDEX.template.md')

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
