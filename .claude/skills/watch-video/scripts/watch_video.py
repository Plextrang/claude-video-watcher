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
import re
import shutil
import statistics as st
import subprocess
import sys
from pathlib import Path

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
FONT = r"C\:/Windows/Fonts/arialbd.ttf"
TAG = re.compile(r'<[^>]*>')
PTS = re.compile(r'pts_time:([0-9.]+)')

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
        sys.exit('FATAL: yt-dlp failed.\n' + '\n'.join(tail[-6:] or ['(no output)']))


def ytdlp(args):
    return [sys.executable, '-m', 'yt_dlp', '--js-runtimes', 'node'] + args


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------- transcript ----
def ts_to_sec(t):
    h, m, s = t.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_vtt(path):
    """Line-based. Whitespace-only separators make blank-line splitting unsafe,
    and YouTube emits ~10ms 'settle' cues that duplicate the previous line."""
    lines = Path(path).read_text(encoding='utf-8', errors='ignore').split('\n')
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

    cues = [(c['start'], c['body'][-1]) for c in raw
            if c['end'] - c['start'] >= 0.1 and c['body']]
    out, prev = [], None
    for start, text in cues:
        if text and text != prev:
            out.append((start, text))
            prev = text
    return out


def step_transcript(d, url, vid_hint=None):
    subs = sorted(d.glob('*.en*.vtt'))
    info = next(iter(d.glob('*.info.json')), None)
    if not subs or not info:
        log('[1/6] transcript + metadata')
        run(ytdlp(['--skip-download', '--write-auto-subs', '--write-subs',
                   '--sub-langs', 'en.*', '--sub-format', 'vtt',
                   '--write-info-json', '-o', str(d / '%(id)s'), url]))
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
    if meta.get('thumbnail') and not thumb.exists():
        try:
            import urllib.request
            urllib.request.urlretrieve(meta['thumbnail'], thumb)
        except Exception as e:
            log(f'   ! thumbnail download failed: {e}')

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
               '-o', str(d / 'source.%(ext)s'), url]))
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
    p = subprocess.run(cmd, capture_output=True, text=True, errors='ignore')
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
    p = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(a), '-i', str(b),
                        '-lavfi', 'ssim', '-f', 'null', '-'],
                       capture_output=True, text=True, errors='ignore')
    m = re.search(r'All:([0-9.]+)', p.stderr)
    return float(m.group(1)) if m else None


def step_cuts(d, src, meta, threshold_override, start, end):
    duration = float(meta.get('duration') or 0)
    win_start = start or 0.0
    win_end = end if end else duration
    win_dur = win_end - win_start

    cuts_csv = d / 'data' / 'cuts.csv'
    if cuts_csv.exists():
        log('[3/6] cut detection (cached)')
        rows = list(csv.DictReader(open(cuts_csv, encoding='utf-8')))
        return json.loads((d / 'data' / 'pacing.json').read_text()), rows

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
    if not pacing.get('low_cut_raw'):
        (d / 'data' / 'low_cut_sections.csv').write_text(
            'start_sec,end_sec,start_mmss,end_mmss,length_sec,mean_ssim,verdict\n',
            encoding='utf-8')
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
    with open(d / 'data' / 'low_cut_sections.csv', 'w', newline='', encoding='utf-8') as f:
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
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                        '-ss', f'{seek:.3f}', '-i', str(src),
                        '-frames:v', '1', '-q:v', '3', '-y', str(out)], check=True)
        made += 1
    log(f'[4/6] frames: {made} new, {skipped} reused, {len(rows)} total')
    return made + skipped == len(rows)


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
        subprocess.run(cmd, check=True)
    with open(d / 'data' / 'sheet_index.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['sheet', 'span_mmss', 'cells'])
        w.writerows(index_rows)
    log(f'[5/6] sheets: {n} from {len(files)} frames')


# ---------------------------------------------------------------- audio ------
def step_audio(d, src, rows, start, end):
    mfile = d / 'data' / 'loudness_m.txt'
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
        subprocess.run(cmd, check=True, cwd=str(d / 'data'))
    if not mfile.exists() or mfile.stat().st_size == 0:
        log('[6/6] audio: ebur128 produced nothing, falling back to astats')
        return False

    off = start or 0.0
    txt = mfile.read_text(errors='ignore').split('\n')
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
def slugify(s):
    s = re.sub(r'[^a-zA-Z0-9]+', '-', (s or '').lower()).strip('-')
    return '-'.join(s.split('-')[:7]) or 'video'


def main():
    ap = argparse.ArgumentParser(description='Video -> frames, sheets, transcript, pacing, audio.')
    ap.add_argument('url', nargs='?', help='YouTube URL')
    ap.add_argument('--slug', help='folder name (default: derived from title)')
    ap.add_argument('--root', default=None,
                    help=f'output root (default: {DEFAULT_ROOT}, a sibling of the repo)')
    ap.add_argument('--start', type=float, help='analyse from this second')
    ap.add_argument('--end', type=float, help='analyse until this second')
    ap.add_argument('--hook-only', action='store_true', help='first 60s only, dense')
    ap.add_argument('--threshold', type=float, help='override scene threshold')
    ap.add_argument('--no-download', action='store_true', help='reuse existing source')
    ap.add_argument('--keep-source', action='store_true', help='do not delete source when done')
    a = ap.parse_args()

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
        t = run(ytdlp(['--skip-download', '--print', '%(title)s', a.url])).stdout.strip()
        slug = slugify(t.split('\n')[-1])
    d = root / slug
    for sub in ('frames', 'sheets', 'data'):
        (d / sub).mkdir(parents=True, exist_ok=True)
    log(f'== {slug}\n== {d}')

    meta = step_transcript(d, a.url)
    src = step_download(d, a.url, a.no_download)
    if a.hook_only:
        log('   --hook-only: analysing 0:00-1:00')

    pacing, rows = step_cuts(d, src, meta, a.threshold, start, end)
    frames_ok = step_frames(d, src, rows)
    classify_low_cut(d, pacing, rows)
    step_sheets(d, rows)
    audio_ok = step_audio(d, src, rows, start, end)

    if src and src.exists():
        if frames_ok and audio_ok and not a.keep_source:
            size = src.stat().st_size / 1e6
            src.unlink()
            log(f'   source deleted ({size:.0f} MB) - frames and audio both succeeded')
        elif not a.keep_source:
            log('   source KEPT - a step did not fully succeed')

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
