"""Regression tests for the watch-video pipeline.

stdlib unittest, not pytest: the no-new-dependency rule applies to the tests
too. No network and no video downloads - the two tests that need real pixels
synthesise them with ffmpeg and skip if it is absent.

    py -m unittest discover -s tests

Every test here corresponds to a bug that was actually shipped, so a failure
means a specific regression rather than a style violation.
"""
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / '.claude' / 'skills' / 'watch-video' / 'scripts' / 'watch_video.py'

_spec = importlib.util.spec_from_file_location('watch_video', SCRIPT)
wv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wv)

HAVE_FFMPEG = shutil.which('ffmpeg') is not None
NL = '\n'


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def analysis_dir(self, name='vid'):
        d = self.tmp / name
        for sub in ('data', 'frames', 'sheets'):
            (d / sub).mkdir(parents=True, exist_ok=True)
        return d


# --------------------------------------------------------------- captions ----
class TestCaptionParsing(TempDirCase):
    def _vtt(self, body, name='t.vtt'):
        p = self.tmp / name
        p.write_text(body, encoding='utf-8')
        return p

    def test_wrapped_manual_captions_keep_every_line(self):
        """The shipped bug: parse_vtt took only body[-1], which is right for
        rolling auto-subs and threw away half of every wrapped manual
        caption - and the track picker prefers the manual one."""
        p = self._vtt(NL.join([
            'WEBVTT', '',
            '00:00:01.000 --> 00:00:05.000',
            'The single most important thing',
            'is that it never guesses.', '',
        ]))
        cues = wv.parse_vtt(p)
        self.assertEqual(len(cues), 1)
        self.assertEqual(
            cues[0][1], 'The single most important thing is that it never guesses.')

    def test_rolling_auto_subs_are_not_duplicated(self):
        """Auto-subs repeat the previous line in every cue. Joining them
        would double the transcript, so the inline timing tags switch
        parse_vtt back to last-line-only."""
        p = self._vtt(NL.join([
            'WEBVTT', '',
            '00:00:00.030 --> 00:00:02.669',
            'what<00:00:00.389><c> is</c><00:00:00.629><c> up</c>', '',
            '00:00:02.669 --> 00:00:02.679',
            'what is up', '',
            '00:00:02.679 --> 00:00:04.500',
            'what is up',
            'today<00:00:03.000><c> we</c><00:00:03.400><c> build</c>', '',
        ]))
        self.assertEqual([t for _, t in wv.parse_vtt(p)],
                         ['what is up', 'today we build'])

    def test_settle_cues_are_dropped(self):
        p = self._vtt(NL.join([
            'WEBVTT', '',
            '00:00:01.000 --> 00:00:01.005',
            'blink<00:00:01.001><c> me</c>', '',
            '00:00:02.000 --> 00:00:05.000',
            'real<00:00:02.100><c> line</c>', '',
        ]))
        self.assertEqual([t for _, t in wv.parse_vtt(p)], ['real line'])

    def test_ts_to_sec_accepts_two_and_three_parts(self):
        """WebVTT permits MM:SS.mmm; only YouTube always sends three parts."""
        self.assertAlmostEqual(wv.ts_to_sec('01:02:03.500'), 3723.5)
        self.assertAlmostEqual(wv.ts_to_sec('02:03.500'), 123.5)
        with self.assertRaises(ValueError):
            wv.ts_to_sec('nonsense')


# ------------------------------------------------------------ CLI parsing ----
class TestParseTime(unittest.TestCase):
    def test_accepted_forms(self):
        for text, want in (('90', 90.0), ('1:30', 90.0),
                           ('01:02:03', 3723.0), ('1:30.5', 90.5)):
            self.assertAlmostEqual(wv.parse_time(text), want, msg=text)

    def test_rejects_garbage(self):
        import argparse
        with self.assertRaises(argparse.ArgumentTypeError):
            wv.parse_time('abc')


class TestVideoId(unittest.TestCase):
    def test_extracts_id_from_every_youtube_url_shape(self):
        for url in ('https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                    'https://youtu.be/dQw4w9WgXcQ?t=30',
                    'https://www.youtube.com/watch?list=PL1&v=dQw4w9WgXcQ',
                    'https://www.youtube.com/embed/dQw4w9WgXcQ'):
            self.assertEqual(wv.url_video_id(url), 'dQw4w9WgXcQ', url)

    def test_returns_none_when_not_youtube(self):
        self.assertIsNone(wv.url_video_id('https://vimeo.com/12345'))
        self.assertIsNone(wv.url_video_id(None))


# ----------------------------------------------------------------- shots ----
class TestMergeShots(unittest.TestCase):
    def test_collapses_detections_inside_the_window(self):
        """High-motion animation trips scene detection repeatedly inside one
        continuous shot; merging is what makes the count honest."""
        times = [0.0, 0.2, 0.4, 0.9, 2.0, 2.5, 3.5]
        self.assertEqual(wv.merge_shots(times, window=1.0), [0.0, 2.0, 3.5])

    def test_empty_input(self):
        self.assertEqual(wv.merge_shots([]), [])


class TestResolveWindow(unittest.TestCase):
    def test_full_video(self):
        self.assertEqual(wv.resolve_window({'duration': 600}, None, None),
                         (0.0, 600.0, 600.0))

    def test_end_past_duration_is_clamped(self):
        """Unclamped, this generated fill timestamps past EOF that extracted
        nothing, leaving frames_ok false and the source kept forever."""
        start, end, _ = wv.resolve_window({'duration': 600}, None, 9999)
        self.assertEqual(end, 600.0)

    def test_missing_duration_does_not_explode(self):
        self.assertEqual(wv.resolve_window({}, None, None), (0.0, 0.0, 0.0))

    def test_negative_start_is_floored(self):
        start, _, _ = wv.resolve_window({'duration': 600}, -5, 100)
        self.assertEqual(start, 0.0)


# ------------------------------------------------------------------ font ----
class TestFont(unittest.TestCase):
    def test_auto_resolves_on_this_platform(self):
        font, tried = wv.resolve_font()
        self.assertIsNotNone(font, f'no font found; tried {tried}')

    def test_explicit_font_that_is_missing_never_falls_back(self):
        """Falling back would hide a typo behind a sheet rendered in the
        wrong typeface."""
        font, tried = wv.resolve_font('Z:/definitely/not/here.ttf')
        self.assertIsNone(font)
        self.assertEqual(tried, ['Z:/definitely/not/here.ttf'])

    def test_drive_letter_colon_is_escaped_for_the_filtergraph(self):
        self.assertEqual(wv.ff_font_arg(r'C:\Windows\Fonts\arialbd.ttf'),
                         'C\\:/Windows/Fonts/arialbd.ttf')

    @unittest.skipUnless(HAVE_FFMPEG, 'needs ffmpeg')
    def test_label_renders_through_a_path_containing_spaces(self):
        """The macOS case: /System/Library/Fonts/Supplemental/Arial Bold.ttf.
        A hardcoded Windows path used to kill contact sheets outright."""
        font, _ = wv.resolve_font()
        spaced = self.enterContext(tempfile.TemporaryDirectory()) \
            if hasattr(self, 'enterContext') else tempfile.mkdtemp()
        target = Path(spaced) / 'Some Bold Font.ttf'
        src = Path(font.replace('\\:', ':'))
        shutil.copy(src, target)
        out = Path(spaced) / 'out.jpg'
        fc = (f"color=c=#101010:s=200x100,drawtext="
              f"fontfile='{wv.ff_font_arg(target)}':text='001  00\\:15'"
              f":fontcolor=white:fontsize=18")
        p = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                            '-y', '-f', 'lavfi', '-i', fc,
                            '-frames:v', '1', str(out)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(out.exists() and out.stat().st_size > 0)


# ----------------------------------------------------------- low-cut CSV ----
class TestClassifyLowCut(TempDirCase):
    def test_rerun_does_not_wipe_the_csv(self):
        """The headline bug. A completed run pops low_cut_raw and rewrites
        pacing.json; the old guard read the absent key as 'no sections' and
        overwrote a populated CSV with a bare header."""
        d = self.analysis_dir()
        pacing = {'low_cut_raw': [[10.0, 40.0]]}
        rows = [{'index': 1, 'timestamp_sec': '15.000',
                 'timestamp_mmss': '00:15', 'source': 'fill'}]
        wv.classify_low_cut(d, pacing, rows)
        csv_path = d / 'data' / 'low_cut_sections.csv'
        first = csv_path.read_text(encoding='utf-8')
        self.assertEqual(len(first.strip().splitlines()), 2)

        cached = json.loads((d / 'data' / 'pacing.json').read_text(encoding='utf-8'))
        self.assertNotIn('low_cut_raw', cached)

        wv.classify_low_cut(d, cached, rows)
        self.assertEqual(csv_path.read_text(encoding='utf-8'), first)

    def test_genuinely_empty_writes_a_header(self):
        d = self.analysis_dir()
        wv.classify_low_cut(d, {'low_cut_raw': []}, [])
        text = (d / 'data' / 'low_cut_sections.csv').read_text(encoding='utf-8')
        self.assertEqual(len(text.strip().splitlines()), 1)


# -------------------------------------------------------- cache validity ----
class TestInvalidation(TempDirCase):
    def _seed(self, window, threshold=0.2):
        d = self.analysis_dir()
        (d / 'data' / 'cuts.csv').write_text('index,timestamp_sec\n', encoding='utf-8')
        (d / 'data' / 'pacing.json').write_text(
            json.dumps({'window': window, 'threshold_used': threshold}),
            encoding='utf-8')
        (d / 'frames' / '001_0000.jpg').write_text('x', encoding='utf-8')
        (d / 'sheets' / 'sheet_001.jpg').write_text('x', encoding='utf-8')
        return d

    def test_same_parameters_keep_the_cache(self):
        d = self._seed([0, 600])
        wv.invalidate_if_params_changed(d, 0.0, 600.0, None)
        self.assertTrue((d / 'data' / 'cuts.csv').exists())

    def test_hook_only_after_a_full_run_discards_it(self):
        """Without this, --hook-only silently reported full-run pacing."""
        d = self._seed([0, 600])
        wv.invalidate_if_params_changed(d, 0.0, 60.0, None)
        self.assertFalse((d / 'data' / 'cuts.csv').exists())
        self.assertFalse(list((d / 'frames').glob('*.jpg')))
        self.assertFalse(list((d / 'sheets').glob('*.jpg')))

    def test_threshold_change_discards_it(self):
        d = self._seed([0, 600], threshold=0.2)
        wv.invalidate_if_params_changed(d, 0.0, 600.0, 0.12)
        self.assertFalse((d / 'data' / 'cuts.csv').exists())

    def test_missing_pacing_json_discards_rather_than_crashes(self):
        d = self._seed([0, 600])
        (d / 'data' / 'pacing.json').unlink()
        wv.invalidate_if_params_changed(d, 0.0, 600.0, None)
        self.assertFalse((d / 'data' / 'cuts.csv').exists())


# ----------------------------------------------------------------- index ----
def _rec(**kw):
    base = {'id': 'aaaaaaaaaaa', 'slug': 'a-video', 'title': 'A Video',
            'channel': 'Chan', 'subs': 1000, 'views': 5000,
            'duration_mmss': '10:00', 'upload_date': '20260101',
            'analyzed': '2026-08-14', 'views_per_sub': 5.0,
            'channel_median_views': 1000, 'multiplier_vs_channel': 5.0,
            'takeaway': wv.PENDING}
    base.update(kw)
    return base


class TestIndex(TempDirCase):
    def test_upsert_preserves_takeaway_and_first_analysed_date(self):
        """Re-running the script is not a new analysis. The first version
        stamped today over `analyzed` for the whole corpus."""
        root = self.tmp
        wv.write_index(root, [_rec(takeaway='the real finding',
                                   analyzed='2026-08-14')])
        wv.upsert_index(root, _rec(takeaway=wv.PENDING, analyzed='2026-12-25',
                                   views=9999))
        recs = wv.load_index(root)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]['takeaway'], 'the real finding')
        self.assertEqual(recs[0]['analyzed'], '2026-08-14')
        self.assertEqual(recs[0]['views'], 9999)

    def test_new_video_is_appended(self):
        root = self.tmp
        wv.write_index(root, [_rec()])
        wv.upsert_index(root, _rec(id='bbbbbbbbbbb', slug='b-video'))
        self.assertEqual(len(wv.load_index(root)), 2)

    def test_sort_puts_missing_multiplier_last(self):
        rows = [_rec(id='1', multiplier_vs_channel=None, views_per_sub=9.0),
                _rec(id='2', multiplier_vs_channel=0.5),
                _rec(id='3', multiplier_vs_channel=7.1)]
        order = [r['id'] for r in sorted(rows, key=wv.index_sort_key)]
        self.assertEqual(order, ['3', '2', '1'])

    def test_index_md_is_regenerated_from_json(self):
        root = self.tmp
        wv.write_index(root, [_rec(takeaway='keep me')])
        text = (root / 'INDEX.md').read_text(encoding='utf-8')
        self.assertIn('keep me', text)
        self.assertIn('a-video/NOTES.md', text)

    def test_harvest_recovers_takeaway_and_date_from_an_old_index(self):
        root = self.tmp
        (root / 'INDEX.md').write_text(
            '| Title | Channel |\n|---|---|\n'
            '| [T](old-slug/NOTES.md) | Chan | 1K | 100 | 2x | 5:00 | '
            '2026-01-01 | 2026-08-13 | a hand written takeaway |\n',
            encoding='utf-8')
        got = wv.harvest_index_md(root)
        self.assertEqual(got['old-slug']['takeaway'], 'a hand written takeaway')
        self.assertEqual(got['old-slug']['analyzed'], '2026-08-13')

    def test_human_numbers(self):
        self.assertEqual(wv._human(195000), '195K')
        self.assertEqual(wv._human(5110), '5.1K')
        self.assertEqual(wv._human(1_500_000), '1.5M')
        self.assertEqual(wv._human(None), '-')


class TestExistingSlug(TempDirCase):
    def test_matches_by_id_so_a_retitled_video_reuses_its_folder(self):
        """Found by running it: a creator retitled a video, the slug drifted,
        and a re-run re-downloaded into a second folder."""
        root = self.tmp
        d = root / 'old-title-slug' / 'data'
        d.mkdir(parents=True)
        (d / 'meta.json').write_text(json.dumps({'id': 'MxW-_nZU3jo'}),
                                     encoding='utf-8')
        self.assertEqual(wv.existing_slug_for(root, 'MxW-_nZU3jo'),
                         'old-title-slug')
        self.assertIsNone(wv.existing_slug_for(root, 'zzzzzzzzzzz'))


class TestSlugify(unittest.TestCase):
    def test_keeps_seven_words_and_strips_punctuation(self):
        self.assertEqual(
            wv.slugify('I Copied a $4,000/Month Faceless Food History Channel'),
            'i-copied-a-4-000-month-faceless')

    def test_never_returns_empty(self):
        self.assertEqual(wv.slugify('!!!'), 'video')
        self.assertEqual(wv.slugify(None), 'video')


# ------------------------------------------------------------- thumbnail ----
class TestThumbnail(unittest.TestCase):
    def test_prefers_a_real_jpeg_over_the_top_ranked_webp(self):
        picked = wv.pick_thumbnail_url({
            'thumbnail': 'https://i.ytimg.com/vi/X/maxresdefault.webp',
            'thumbnails': [
                {'url': 'https://i.ytimg.com/vi/X/hqdefault.jpg', 'width': 480},
                {'url': 'https://i.ytimg.com/vi/X/maxresdefault.jpg', 'width': 1280},
                {'url': 'https://i.ytimg.com/vi/X/maxresdefault.webp', 'width': 1280}]})
        self.assertTrue(picked.endswith('maxresdefault.jpg'))

    def test_falls_back_when_no_jpeg_exists(self):
        self.assertEqual(
            wv.pick_thumbnail_url({'thumbnail': 'x.webp',
                                   'thumbnails': [{'url': 'y.webp'}]}),
            'x.webp')


# -------------------------------------------------------------- patterns ----
NOTES_TEMPLATE = NL.join([
    '# {title}', '',
    '## 1. Header', 'header body', '',
    '## 2. Hook map, 0:00-0:45', 'hook body for {title}', '',
    '## 3. Animation inventory', 'inventory body', '',
    '{h3b}. Reproduction cost', 'cost body for {title}', '',
    '### A sub-heading that must stay inside 3b', 'still cost body', '',
    '## 4. Pacing', 'pacing body', '',
    '## 6. Segment structure', 'segment body', '',
    '## 7. Composition mix', 'mix body', '',
    '## 9. Packaging', 'packaging body', '',
    '## 10. Steal list', 'steal body', '',
])


class TestSliceNotes(unittest.TestCase):
    def test_slices_wanted_sections_and_keeps_sub_headings(self):
        text = NOTES_TEMPLATE.format(title='T', h3b='## 3b')
        got = wv.slice_notes(text, ['2', '3b', '6', '7', '9'])
        self.assertEqual(set(got), {'2', '3b', '6', '7', '9'})
        self.assertIn('hook body for T', got['2'])
        self.assertIn('still cost body', got['3b'])
        self.assertNotIn('pacing body', got['3b'])

    def test_matches_h3_and_uppercase_variants(self):
        """Real files use '### 3b.' and full uppercase titles, so matching
        must key off the number, never the title."""
        text = NOTES_TEMPLATE.format(title='T', h3b='### 3b')
        self.assertIn('cost body', wv.slice_notes(text, ['3b'])['3b'])
        upper = '## 2. HOOK MAP - 0:00 to 0:45' + NL + 'body' + NL
        self.assertIn('body', wv.slice_notes(upper, ['2'])['2'])


class TestPatternsCorpusGuard(TempDirCase):
    def _corpus(self, n, with_notes=True):
        root = self.tmp
        recs = []
        for i in range(n):
            slug = f'video-{i}'
            recs.append(_rec(id=f'id{i}', slug=slug, title=f'Video {i}'))
            (root / slug).mkdir(parents=True, exist_ok=True)
            if with_notes:
                (root / slug / 'NOTES.md').write_text(
                    NOTES_TEMPLATE.format(title=f'Video {i}', h3b='## 3b'),
                    encoding='utf-8')
        wv.write_index(root, recs)
        return root

    def test_refuses_under_the_floor_and_writes_nothing(self):
        root = self._corpus(wv.MIN_CORPUS - 1)
        with self.assertRaises(SystemExit) as cm:
            wv.build_patterns_input(root)
        self.assertIn('at least', str(cm.exception))
        self.assertFalse((root / 'PATTERNS-INPUT.md').exists())

    def test_builds_with_a_warning_banner_below_twelve(self):
        root = self._corpus(wv.MIN_CORPUS)
        wv.build_patterns_input(root)
        text = (root / 'PATTERNS-INPUT.md').read_text(encoding='utf-8')
        self.assertIn('CORPUS WARNING', text)
        self.assertIn('hook body for Video 0', text)
        self.assertIn('packaging body', text)
        # Sections outside the wanted five must not be carried over.
        self.assertNotIn('steal body', text)
        self.assertNotIn('pacing body', text)

    def test_no_banner_at_or_above_twelve(self):
        root = self._corpus(wv.PROVISIONAL_CORPUS)
        wv.build_patterns_input(root)
        self.assertNotIn('CORPUS WARNING',
                         (root / 'PATTERNS-INPUT.md').read_text(encoding='utf-8'))

    def test_folders_without_notes_are_listed_not_dropped(self):
        root = self._corpus(wv.MIN_CORPUS + 1)
        (root / 'video-0' / 'NOTES.md').unlink()
        wv.build_patterns_input(root)
        text = (root / 'PATTERNS-INPUT.md').read_text(encoding='utf-8')
        self.assertIn('Incomplete analyses', text)
        self.assertIn('`video-0`', text)


if __name__ == '__main__':
    unittest.main()
