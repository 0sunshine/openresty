#!/usr/bin/env python3
"""Real HTTP/MP4 regression, using only Python's standard library and FFmpeg.

Run on Linux or Windows with an isolated, patched OpenResty executable.  No
business database, source transcoding, or model service is used by the tests.
"""
import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request


def command(argv):
    result = subprocess.run([str(x) for x in argv], capture_output=True,
                            timeout=90, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise AssertionError(result.stderr.decode(errors='replace')[-6000:])
    return result


def packets(path, interval=None, raw=False):
    args = [OPTIONS.ffprobe_bin, '-v', 'error']
    if raw:
        args += ['-ignore_editlist', '1']
    if interval:
        args += ['-read_intervals', interval]
    args += ['-show_packets', '-show_data_hash', 'sha256', '-show_entries',
             'packet=stream_index,pts_time,dts_time,data_hash,flags:stream=index,codec_type,time_base:'
             'format=duration', '-of', 'json', path]
    return json.loads(command(args).stdout)


def boxes(data):
    at = 0
    while at < len(data):
        size, kind = struct.unpack_from('>I4s', data, at)
        assert 8 <= size <= len(data)-at
        yield kind, data[at+8:at+size]
        at += size


def box(kind, body):
    return struct.pack('>I4s', len(body)+8, kind)+body


def read_movie(path):
    # A real sample may be gigabytes; seek past mdat instead of reading it.
    with path.open('rb') as stream:
        while True:
            header = stream.read(8)
            assert len(header) == 8, 'Missing moov'
            size, kind = struct.unpack('>I4s', header)
            width = 8
            if size == 1:
                size = struct.unpack('>Q', stream.read(8))[0]
                width = 16
            assert size >= width
            if kind == b'moov':
                assert size-width <= 128*1024*1024
                movie = stream.read(size-width)
                assert len(movie) == size-width
                return movie
            stream.seek(size-width, 1)


def movie_metadata(path):
    movie = read_movie(path)
    header = next(body for kind, body in boxes(movie) if kind == b'mvhd')
    at = 20 if header[0] else 12
    scale = struct.unpack_from('>I', header, at)[0]
    duration = struct.unpack_from('>Q' if header[0] else '>I', header, at+4)[0]
    ends = []
    for kind, track in boxes(movie):
        if kind != b'trak':
            continue
        tkhd = next(body for k, body in boxes(track) if k == b'tkhd')
        end = struct.unpack_from('>Q' if tkhd[0] else '>I', tkhd, 28 if tkhd[0] else 20)[0]
        ends.append(end/scale)
        edit = next((body for k, body in boxes(track) if k == b'edts'), None)
        if edit is None:
            continue
        table = next((body for k, body in boxes(edit) if k == b'elst'), None)
        if table is None:
            continue
        width = 20 if table[0] else 12
        count = struct.unpack_from('>I', table, 4)[0]
        total = sum(struct.unpack_from('>Q' if table[0] else '>I', table, 8+i*width)[0]
                    for i in range(count))
        assert total <= end <= duration
    return duration/scale, ends


def movie_duration(path):
    return movie_metadata(path)[0]


def apply_movie_edits(path, probe):
    """Independent ISO timeline check, without demuxer last-packet heuristics."""
    movie = read_movie(path)
    mvhd = next(body for kind, body in boxes(movie) if kind == b'mvhd')
    movie_scale = struct.unpack_from('>I', mvhd, 20 if mvhd[0] else 12)[0]
    shifts = []
    for kind, track in boxes(movie):
        if kind != b'trak':
            continue
        mdia = next(body for k, body in boxes(track) if k == b'mdia')
        mdhd = next(body for k, body in boxes(mdia) if k == b'mdhd')
        media_scale = struct.unpack_from('>I', mdhd, 20 if mdhd[0] else 12)[0]
        edit = next((body for k, body in boxes(track) if k == b'edts'), b'')
        elst = next((body for k, body in boxes(edit) if k == b'elst'), None)
        shift = 0
        if elst:
            lead = 0
            count = struct.unpack_from('>I', elst, 4)[0]
            for i in range(count):
                duration, origin, integer, fraction = struct.unpack_from(
                    '>Qqhh' if elst[0] else '>Iihh', elst, 8+i*(20 if elst[0] else 12))
                assert (integer, fraction) == (1, 0)
                if origin == -1:
                    lead += duration/movie_scale
                else:
                    shift = lead-origin/media_scale
                    break
        shifts.append(shift)
    _, ends = movie_metadata(path)
    for packet in probe['packets']:
        track = packet['stream_index']
        pts = float(packet['pts_time'])+shifts[track]
        packet['pts_time'] = str(pts)
        packet['flags'] = 'D' if pts < 0 or pts >= ends[track] else ''
    return probe


def change_movie(source, target, variant):
    """Mutate only metadata; adjust chunk addresses when faststart grows."""
    tops = list(boxes(source.read_bytes()))
    movie = next(body for kind, body in tops if kind == b'moov')

    def rewrite(data, adjustment=0):
        output = []
        for kind, body in boxes(data):
            if kind == b'edts' and variant == 'empty_edts':
                body = b''
            elif kind in {b'moov', b'trak', b'edts', b'mdia', b'minf', b'stbl'}:
                body = rewrite(body, adjustment)
            elif kind == b'elst':
                count = struct.unpack_from('>I', body, 4)[0]
                if variant == 'version1':
                    assert body[0] == 0
                    rows = [struct.unpack_from('>Iihh', body, 8+i*12) for i in range(count)]
                    body = b'\1\0\0\0'+struct.pack('>I', count)+b''.join(
                        struct.pack('>Qqhh', *row) for row in rows)
                elif variant == 'rate':
                    body = body[:-4]+struct.pack('>hh', 2, 0)
                elif variant == 'disjoint':
                    body = body[:4]+struct.pack('>I', count+1)+body[8:]+body[-12:]
                elif variant == 'truncated':
                    body = body[:4]+struct.pack('>I', count+1)+body[8:]
                elif variant == 'empty_elst':
                    body = body[:4]+bytes(4)
                elif variant == 'unknown_edit_version':
                    body = b'\2'+body[1:]
                elif variant == 'invalid_edit_origin':
                    body = body[:12]+struct.pack('>i', -2)+body[16:]
            elif kind == b'mdhd' and variant == 'zero_timescale':
                body = bytearray(body)
                struct.pack_into('>I', body, 20 if body[0] else 12, 0)
                body = bytes(body)
            elif kind == b'stts' and variant == 'wrong_stts_count':
                body = bytearray(body)
                struct.pack_into('>I', body, 8, struct.unpack_from('>I', body, 8)[0]+1)
                body = bytes(body)
            elif kind in {b'stts', b'ctts'} and variant == 'zero_runs':
                count = struct.unpack_from('>I', body, 4)[0]
                middle = 8+(count//2)*8
                empty = struct.pack('>II', 0, 0 if kind == b'stts' else 0xffffffff)
                body = body[:4]+struct.pack('>I', count+3)+empty+body[8:middle]+empty+body[middle:]+empty
            elif kind == b'ctts' and variant in {'long_ctts', 'short_ctts'}:
                count = struct.unpack_from('>I', body, 4)[0]
                body = bytearray(body)
                at = 8+(count-1)*8
                struct.pack_into('>I', body, at, struct.unpack_from('>I', body, at)[0]+(50 if variant=='long_ctts' else -1))
                body = bytes(body)
            elif kind == b'stco':
                count = struct.unpack_from('>I', body, 4)[0]
                offsets = struct.unpack_from('>'+str(count)+'I', body, 8)
                body = body[:8]+struct.pack('>'+str(count)+'I', *(x+adjustment for x in offsets))
            elif kind == b'co64':
                count = struct.unpack_from('>I', body, 4)[0]
                offsets = struct.unpack_from('>'+str(count)+'Q', body, 8)
                body = body[:8]+struct.pack('>'+str(count)+'Q', *(x+adjustment for x in offsets))
            output.append((kind, body))
            if kind == b'edts' and variant == 'duplicate_edts':
                output.append((kind, body))
        if variant == 'edit_after_media':
            output.sort(key=lambda row: row[0] == b'edts')
        return b''.join(box(k, v) for k, v in output)

    changed = rewrite(movie)
    delta = len(changed)-len(movie)
    before_data = next(i for i, (k, _) in enumerate(tops) if k == b'moov') < next(
        i for i, (k, _) in enumerate(tops) if k == b'mdat')
    changed = rewrite(movie, delta if before_data else 0)
    target.write_bytes(b''.join(box(k, changed if k == b'moov' else v) for k, v in tops))


class MP4Timeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix='mp4-timeline-', dir=OPTIONS.work_dir)).resolve()
        (cls.work/'logs').mkdir()
        cls.fixtures = []
        for name, vd, ad, bf, flags, edit in [
            ('delayed-video', .593, 0, 2, '+faststart', 1),
            ('delayed-audio', 0, .673, 2, '+faststart', 1),
            ('no-bframes', 0, 0, 0, '+faststart', 1),
            ('signed-ctts', .317, 0, 3, '+faststart+negative_cts_offsets', 1),
            ('open-gop', .173, 0, 2, '+faststart', 1),
            ('no-edit', 0, 0, 2, '+faststart', 0),
            ('long-first-sample', 1.337, 0, 2, '+faststart', 0),
        ]:
            path = cls.work/(name+'.mp4')
            command([OPTIONS.ffmpeg_bin, '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i',
                     'testsrc2=size=160x90:rate=25:duration=8', '-f', 'lavfi', '-i',
                     'anoisesrc=sample_rate=48000:duration=8:seed=731',
                     '-vf', 'setpts=PTS+'+str(vd)+'/TB', '-af', 'asetpts=PTS+'+str(ad)+'/TB',
                     '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '25', '-bf', str(bf),
                     '-x264-params', 'open-gop='+('1' if name == 'open-gop' else '0')+':scenecut=0',
                     '-threads:v', '1', '-fps_mode:v', 'passthrough', '-c:a', 'aac', '-b:a', '128k',
                     '-avoid_negative_ts', 'disabled' if edit else 'make_zero', '-use_editlist', str(edit),
                     '-video_track_timescale', '90000', '-movie_timescale', '90000',
                     '-movflags', flags, path])
            cls.fixtures.append(path)
        for variant in ['version1', 'edit_after_media', 'empty_edts', 'empty_elst', 'zero_runs',
                        'rate', 'disjoint', 'truncated', 'long_ctts', 'short_ctts',
                        'unknown_edit_version', 'invalid_edit_origin', 'zero_timescale',
                        'wrong_stts_count', 'duplicate_edts']:
            target = cls.work/(variant+'.mp4')
            change_movie(cls.fixtures[0], target, variant)
            if variant in {'version1', 'edit_after_media', 'empty_edts', 'empty_elst', 'zero_runs'}:
                cls.fixtures.append(target)

        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.base = 'http://127.0.0.1:'+str(cls.port)+'/'
        root = str(cls.work).replace('\\', '/').replace('"', '\\"')
        lua = ''
        version = command([OPTIONS.nginx_bin, '-V'])
        if b'ngx_lua' in version.stdout+version.stderr:
            binary = Path(OPTIONS.nginx_bin).resolve()
            library = next((parent/'lualib' for parent in list(binary.parents)[:3]
                            if (parent/'lualib').is_dir()), None)
            if library is None:
                raise AssertionError('OpenResty lualib is missing beside the executable/runtime')
            path = library.as_posix().replace('"', '\\"')
            lua = ('lua_package_path "'+path+'/?.lua;'+path+'/?/init.lua;;"; '
                   'lua_package_cpath "'+path+'/?.'+('dll' if os.name == 'nt' else 'so')+';;"; ')
        config = ('worker_processes 1; pid logs/nginx.pid; error_log logs/error.log notice; '
                  'events {worker_connections 128;} http {'+lua+'access_log off; server {listen 127.0.0.1:'
                  +str(cls.port)+'; location / {root "'+root+'"; mp4; mp4_start_key_frame on; '
                  'mp4_max_buffer_size 128m;}}}')
        if os.name != 'nt' and os.geteuid() == 0:
            config = 'user root; '+config
        (cls.work/'nginx.conf').write_text(config)
        cls.args = [OPTIONS.nginx_bin, '-p', str(cls.work)+'/', '-c', 'nginx.conf']
        command([*cls.args, '-t'])
        cls.log = (cls.work/'process.log').open('wb')
        cls.process = subprocess.Popen([*cls.args, '-g', 'daemon off;'], stdout=cls.log, stderr=cls.log,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(100):
            try:
                cls.read(cls.base+cls.fixtures[0].name, method='HEAD')
                break
            except OSError:
                if cls.process.poll() is not None:
                    raise AssertionError((cls.work/'process.log').read_text())
                time.sleep(.05)
        else:
            raise AssertionError('Nginx failed to start')
        print('WORK_DIRECTORY', cls.work, flush=True)

    @classmethod
    def tearDownClass(cls):
        command([*cls.args, '-s', 'quit'])
        try:
            cls.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=10)
        cls.log.close()

    @staticmethod
    def read(url, method='GET', headers=None):
        with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers or {}), timeout=20) as r:
            return r.status, dict(r.headers), r.read()

    def assert_timeline(self, source, clip, start, interval=None, raw=False):
        self.assert_generated_tables(clip)
        original = packets(str(source), interval, raw)
        actual = packets(str(clip), raw=raw)
        if raw:
            original = apply_movie_edits(source, original)
            actual = apply_movie_edits(clip, actual)
        count = collections.Counter(q['data_hash'] for q in original['packets'])
        lookup = {q['data_hash']: q for q in original['packets'] if count[q['data_hash']] == 1}
        kinds = {stream['index']: stream['codec_type'] for stream in original['streams']}
        prior = {}
        for packet in original['packets']:
            pts = float(packet['pts_time'])
            if 'D' not in packet.get('flags', '') and pts < start:
                track = packet['stream_index']
                prior[track] = max(prior.get(track, float('-inf')), pts)
        matched = collections.Counter()
        for q in actual['packets']:
            before = lookup.get(q['data_hash'])
            if before is None:
                continue
            delta = float(before['pts_time'])-float(q['pts_time'])
            if abs(delta-start) > .00005:
                # A video sample already on screen at the cut is clipped to
                # output PTS 0. Other samples keep their common time shift.
                self.assertEqual(kinds[before['stream_index']], 'video',
                                 msg=f'{source.name}: track {before["stream_index"]}, delta {delta}, cut {start}')
                self.assertLess(float(before['pts_time']), start)
                self.assertAlmostEqual(float(q['pts_time']), 0, delta=.00005)
                self.assertAlmostEqual(float(before['pts_time']), prior[before['stream_index']], delta=.00005)
            matched[before['stream_index']] += 1
        self.assertGreater(sum(matched.values()), 0)
        # FFprobe reports a retained media/decode span for some edit lists.
        # Validate the movie interval and edit durations directly instead.
        duration = movie_duration(clip)
        expected_tracks = collections.Counter(q['stream_index'] for q in lookup.values()
            if start <= float(q['pts_time']) < start+duration)
        for track, eligible in expected_tracks.items():
            if eligible > 1:
                self.assertGreater(matched[track], 0, msg=f'Missing visible track {track}: {source}')
        # Unedited media can carry CTS tail packets beyond tkhd's declared
        # movie interval. Only require the source track's presented interval.
        _, source_ends = movie_metadata(source)
        visible = {key for key, q in lookup.items()
                   if start <= float(q['pts_time'])
                   < min(start+duration, source_ends[q['stream_index']])-.000001}
        delivered = {q['data_hash'] for q in actual['packets'] if 'D' not in q.get('flags', '')}
        self.assertFalse(visible-delivered, msg=f'Visible source packets lost: {source}')
        for q in actual['packets']:
            if 'D' not in q.get('flags', ''):
                self.assertLess(float(q['pts_time']), duration+.00005)
        return dict(matched)

    def assert_generated_tables(self, clip):
        for kind, track in boxes(read_movie(clip)):
            if kind != b'trak':
                continue
            mdia = next(body for k, body in boxes(track) if k == b'mdia')
            minf = next(body for k, body in boxes(mdia) if k == b'minf')
            stbl = next(body for k, body in boxes(minf) if k == b'stbl')
            tables = dict(boxes(stbl))
            samples = struct.unpack_from('>I', tables[b'stsz'], 8)[0]
            for name in (b'stts', b'ctts'):
                if name not in tables:
                    continue
                table = tables[name]
                entries = struct.unpack_from('>I', table, 4)[0]
                self.assertEqual(len(table), 8+entries*8)
                counts = [struct.unpack_from('>I', table, 8+i*8)[0] for i in range(entries)]
                self.assertTrue(all(counts), f'Empty run emitted in {name}')
                self.assertEqual(sum(counts), samples, f'Inconsistent sample counts in {name}')

    def test_01_packet_timestamps_and_decoding(self):
        for source in self.fixtures:
            for start, end in [(0, .2), (.137, 2.893), (.641, 3.111), (1.131, 4.131),
                               (2.333, 5.333), (5.713, 7.953)]:
                with self.subTest(source=source.name, start=start):
                    url = self.base+source.name+f'?start={start}&end={end}'
                    _, _, body = self.read(url)
                    clip = self.work/'response.mp4'
                    clip.write_bytes(body)
                    matched = self.assert_timeline(source, clip, start)
                    self.assertAlmostEqual(movie_duration(clip), end-start, delta=.00005)
                    if matched.get(0, 0) and end > 1:
                        command([OPTIONS.ffmpeg_bin, '-v', 'error', '-xerror', '-i', clip,
                                 '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'])

    def test_02_head_range_download_and_original_identity(self):
        source = self.fixtures[0]
        self.assertEqual(self.read(self.base+source.name)[2], source.read_bytes())
        url = self.base+source.name+'?start=1.131&end=4.131'
        status, headers, body = self.read(url)
        self.assertEqual(status, 200)
        self.assertEqual(int(headers['Content-Length']), len(body))
        self.assertEqual(int(self.read(url, method='HEAD')[1]['Content-Length']), len(body))
        status, headers, part = self.read(url, headers={'Range': 'bytes=20-119'})
        self.assertEqual(status, 206)
        self.assertEqual(part, body[20:120])
        self.assertEqual(self.read(url+'&download=1')[2], body)

    def test_03_start_only_end_only_and_edit_box_order(self):
        for source in self.fixtures:
            for query, start in [('?start=2.333', 2.333), ('?end=3.111', 0)]:
                with self.subTest(source=source.name, query=query):
                    clip = self.work/'response.mp4'
                    clip.write_bytes(self.read(self.base+source.name+query)[2])
                    self.assert_timeline(source, clip, start)

    def test_04_unsupported_edits_fail_without_killing_worker(self):
        for name in ['rate', 'disjoint', 'truncated', 'long_ctts', 'short_ctts',
                     'unknown_edit_version', 'invalid_edit_origin', 'zero_timescale',
                     'wrong_stts_count', 'duplicate_edts']:
            with self.subTest(name=name):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self.read(self.base+name+'.mp4?start=1&end=3')
                self.assertEqual(raised.exception.code, 500)
        self.assertIn('unsupported or invalid mp4 edit list', (self.work/'logs/error.log').read_text())
        self.assertEqual(self.read(self.base+self.fixtures[0].name, method='HEAD')[0], 200)

    def test_06_vfr_held_frame_tail_and_coarse_movie_clock(self):
        for bframes in (0, 2):
            source = self.work/f'vfr-{bframes}.mp4'
            command([OPTIONS.ffmpeg_bin, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                     'testsrc2=size=160x90:rate=25:duration=8', '-f', 'lavfi', '-i',
                     'anoisesrc=sample_rate=48000:duration=13:seed=731',
                     '-vf', r'setpts=if(gte(N\,25)\,PTS+5/TB\,PTS)',
                     '-c:v','libx264','-preset','ultrafast','-g','25','-bf',str(bframes),'-threads:v','1',
                     '-fps_mode:v','passthrough','-c:a','aac','-video_track_timescale','90000',
                     '-movie_timescale','90000','-movflags','+faststart',source])
            expected = command([OPTIONS.ffmpeg_bin,'-v','error','-i',source,'-vf','select=eq(n\\,24)',
                                '-frames:v','1','-an','-f','md5','-']).stdout
            for start,end in [(1.131,1.2),(1.131,4.131),(1.131,7),(12.99,13)]:
                with self.subTest(bframes=bframes,start=start,end=end):
                    clip=self.work/'vfr-response.mp4'
                    body=self.read(self.base+source.name+f'?start={start}&end={end}')[2]
                    clip.write_bytes(body);self.assert_timeline(source,clip,start)
                    command([OPTIONS.ffmpeg_bin,'-v','error','-xerror','-i',clip,'-f','null','-'])
                    if start==1.131:
                        frame=command([OPTIONS.ffmpeg_bin,'-v','error','-i',clip,'-frames:v','1','-an','-f','md5','-']).stdout
                        self.assertEqual(frame,expected,'The frame active at the cut must remain visible')
                    if end==1.2:self.assertLess(len(body),source.stat().st_size/2)

    def test_07_empty_track_can_be_cut_again(self):
        source=self.work/'empty-track.mp4'
        source.write_bytes(self.read(self.base+self.fixtures[0].name+'?start=0&end=0.2')[2])
        clip=self.work/'empty-track-response.mp4'
        clip.write_bytes(self.read(self.base+source.name+'?start=0.01&end=0.1')[2])
        self.assertAlmostEqual(movie_duration(clip),.09,delta=.00005)
        self.assert_generated_tables(clip)

    def test_08_multiple_audio_tracks_and_short_video(self):
        source=self.work/'short-video-multiple-audio.mp4'
        command([OPTIONS.ffmpeg_bin,'-v','error','-y','-f','lavfi','-i',
                 'testsrc2=size=160x90:rate=30000/1001:duration=3',
                 '-f','lavfi','-i','anoisesrc=sample_rate=48000:duration=8:seed=731',
                 '-f','lavfi','-i','anoisesrc=sample_rate=44100:duration=8:seed=132',
                 '-filter_complex','[2:a]asetpts=PTS+0.673/TB[delayed]',
                 '-map','0:v','-map','1:a','-map','[delayed]',
                 '-c:v','libx264','-preset','ultrafast','-g','25','-bf','3','-threads:v','1',
                 '-c:a','aac','-video_track_timescale','90000','-movie_timescale','1000',
                 '-movflags','+faststart',source])
        for start,end in [(0,.2),(.137,2.893),(2.99,3),(3.5,4.5),(7.99,8),(8.2,8.5)]:
            with self.subTest(start=start,end=end):
                clip=self.work/f'multiple-audio-response-{start}.mp4'
                clip.write_bytes(self.read(self.base+source.name+f'?start={start}&end={end}')[2])
                # FFmpeg's MOV demuxer substitutes edit duration for the last
                # packet's duration. A 10 ms tail inside a 21 ms AAC packet can
                # then be incorrectly discarded. Check that case directly
                # against raw packet clocks and the ISO edit list instead.
                matched=self.assert_timeline(source,clip,start,raw=(start==7.99))
                if start>.673:self.assertGreater(matched.get(2,0),0)
                command([OPTIONS.ffmpeg_bin,'-v','error','-xerror','-i',clip,
                         '-map','0:v?','-map','0:a?','-f','null','-'])

    def test_09_concurrent_requests_do_not_share_modified_tables(self):
        source = self.fixtures[0]
        urls = [self.base+source.name+f'?start={start}&end={start+2}'
                for start in (.137, .641, 1.131, 2.333)]
        expected = {url: hashlib.sha256(self.read(url)[2]).digest() for url in urls}

        def request(index):
            if index % 5 == 0:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self.read(self.base+'long_ctts.mp4?start=1&end=3')
                self.assertEqual(raised.exception.code, 500)
            else:
                url = urls[index % len(urls)]
                self.assertEqual(hashlib.sha256(self.read(url)[2]).digest(), expected[url])

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(request, range(80)))
        log = (self.work/'logs/error.log').read_text()
        self.assertNotRegex(log, r'exited on signal|AddressSanitizer|runtime error:')

    @unittest.skipIf(os.name == 'nt', 'requires sparse-file support without allocating 4 GiB')
    def test_10_co64_offsets_above_four_gibibytes(self):
        source = self.fixtures[0]
        tops = list(boxes(source.read_bytes()))
        movie = next(body for kind, body in tops if kind == b'moov')
        payload = next(body for kind, body in tops if kind == b'mdat')
        old_offset = 0
        for kind, body in tops:
            if kind == b'mdat':
                old_offset += 8
                break
            old_offset += len(body)+8

        def rewrite(data, adjustment):
            output = []
            for kind, body in boxes(data):
                if kind in {b'moov', b'trak', b'mdia', b'minf', b'stbl'}:
                    body = rewrite(body, adjustment)
                elif kind == b'stco':
                    count = struct.unpack_from('>I', body, 4)[0]
                    offsets = struct.unpack_from('>'+str(count)+'I', body, 8)
                    kind = b'co64'
                    body = body[:8]+struct.pack('>'+str(count)+'Q', *(x+adjustment for x in offsets))
                output.append(box(kind, body))
            return b''.join(output)

        before = b''.join(box(kind, body) for kind, body in tops if kind not in {b'moov', b'mdat'})
        gap = 1 << 32
        new_offset = len(before)+len(box(b'moov', rewrite(movie, 0)))+16+gap
        target = self.work/'large-co64.mp4'
        with target.open('wb') as stream:
            stream.write(before+box(b'moov', rewrite(movie, new_offset-old_offset)))
            stream.write(struct.pack('>I4sQ', 1, b'mdat', 16+gap+len(payload)))
            stream.seek(gap, 1)
            stream.write(payload)
        self.assertGreater(target.stat().st_size, gap)
        clip = self.work/'large-co64-response.mp4'
        clip.write_bytes(self.read(self.base+target.name+'?start=1.131&end=4.131')[2])
        self.assert_timeline(target, clip, 1.131)
        command([OPTIONS.ffmpeg_bin, '-v', 'error', '-xerror', '-i', clip, '-f', 'null', '-'])

    def test_11_keyframe_preroll_above_uint32_ticks(self):
        # A valid video-only long-GOP clock: no large media allocation or
        # real-time wait is needed to exercise a >32-bit keyframe prefix.
        tops = list(boxes(self.fixtures[2].read_bytes()))
        factor = 60000

        def rewrite(data, adjustment=0):
            output = []
            for kind, body in boxes(data):
                if kind == b'trak':
                    mdia = next(b for k, b in boxes(body) if k == b'mdia')
                    handler = next(b for k, b in boxes(mdia) if k == b'hdlr')
                    if handler[8:12] != b'vide':
                        continue
                if kind in {b'moov', b'trak', b'edts', b'mdia', b'minf', b'stbl'}:
                    body = rewrite(body, adjustment)
                elif kind in {b'mvhd', b'mdhd'}:
                    assert body[0] == 0
                    created, modified, scale, duration = struct.unpack_from('>4I', body, 4)
                    body = b'\1'+body[1:4]+struct.pack('>QQIQ', created, modified, scale, duration*factor)+body[20:]
                elif kind == b'tkhd':
                    assert body[0] == 0
                    created, modified, track, reserved, duration = struct.unpack_from('>5I', body, 4)
                    body = b'\1'+body[1:4]+struct.pack('>QQIIQ', created, modified, track, reserved, duration*factor)+body[24:]
                elif kind == b'elst':
                    count = struct.unpack_from('>I', body, 4)[0]
                    rows = [struct.unpack_from('>Iihh', body, 8+i*12) for i in range(count)]
                    body = b'\1'+body[1:8]+b''.join(struct.pack('>Qqhh', duration*factor,
                        origin*factor if origin >= 0 else origin, integer, fraction)
                        for duration, origin, integer, fraction in rows)
                elif kind == b'stts':
                    body = bytearray(body)
                    for at in range(12, len(body), 8):
                        struct.pack_into('>I', body, at, struct.unpack_from('>I', body, at)[0]*factor)
                    body = bytes(body)
                elif kind == b'stco':
                    count = struct.unpack_from('>I', body, 4)[0]
                    offsets = struct.unpack_from('>'+str(count)+'I', body, 8)
                    body = body[:8]+struct.pack('>'+str(count)+'I', *(x+adjustment for x in offsets))
                output.append(box(kind, body))
            return b''.join(output)

        movie = next(body for kind, body in tops if kind == b'moov')
        changed = rewrite(movie)
        changed = rewrite(movie, len(changed)-len(movie))
        source = self.work/'long-gop-clock.mp4'
        source.write_bytes(b''.join(box(kind, changed if kind == b'moov' else body) for kind, body in tops))
        start, end = .953*factor, 1.753*factor
        clip = self.work/'long-gop-clock-response.mp4'
        clip.write_bytes(self.read(self.base+source.name+f'?start={start}&end={end}')[2])
        self.assert_timeline(source, clip, start)
        self.assertAlmostEqual(movie_duration(clip), end-start, delta=.00005)

    @unittest.skipUnless(os.environ.get('OPENRESTY_MP4_REAL_SOURCE'), 'optional real media copy')
    def test_05_real_media(self):
        source = Path(os.environ['OPENRESTY_MP4_REAL_SOURCE']).resolve()
        target = self.work/'real.mp4'
        try:
            os.link(source, target)
        except OSError:
            import shutil
            shutil.copyfile(source, target)
        start = float(os.environ.get('OPENRESTY_MP4_REAL_START', '2047.52'))
        end = float(os.environ.get('OPENRESTY_MP4_REAL_END', '2062.225'))
        clip = self.work/'real-response.mp4'
        clip.write_bytes(self.read(self.base+'real.mp4'+f'?start={start}&end={end}')[2])
        matched = self.assert_timeline(source, clip, start, f'{max(0,start-5)}%+{end-start+10}')
        self.assertGreater(matched.get(0, 0), 10)
        self.assertGreater(matched.get(1, 0), 10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--nginx-bin', required=True)
    parser.add_argument('--ffmpeg-bin', default='ffmpeg')
    parser.add_argument('--ffprobe-bin', default='ffprobe')
    parser.add_argument('--work-dir', required=True)
    OPTIONS, rest = parser.parse_known_args()
    Path(OPTIONS.work_dir).mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[__file__, *rest], verbosity=2)
