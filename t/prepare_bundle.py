#!/usr/bin/env python3
"""Exercise source assembly and cache behavior without an external patch tool."""
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = os.environ.get('OPENRESTY_RELEASE_ARCHIVE')


@unittest.skipUnless(ARCHIVE, 'set OPENRESTY_RELEASE_ARCHIVE to the pinned official tarball')
class PrepareBundle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='direct-nginx-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.checkout = self.base/'checkout'
        shutil.copytree(ROOT/'bundle', self.checkout/'bundle')
        (self.checkout/'util').mkdir()
        for name in ['prepare-mp4-bundle.py', 'build-win32.sh']:
            shutil.copy2(ROOT/'util'/name, self.checkout/'util'/name)
        spec = importlib.util.spec_from_file_location('prepare_direct_bundle', self.checkout/'util/prepare-mp4-bundle.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.work = self.base/'work'
        self.work.mkdir()
        shutil.copyfile(ARCHIVE, self.work/f'openresty-{self.module.VERSION}.tar.gz')

    def prepare(self):
        # This also catches accidental calls to git/patch during assembly.
        with patch.dict(os.environ, {'PATH': ''}):
            return self.module.prepare(self.work)

    def test_complete_branch_source_and_cache_reuse(self):
        result = self.prepare()
        files = self.module.source_files(self.checkout/'bundle'/self.module.NGINX)
        self.assertGreater(len(files), 500)
        for relative, source, _ in files:
            with self.subTest(path=str(relative)):
                self.assertEqual((result/'bundle'/self.module.NGINX/relative).read_bytes(), source.read_bytes())
        self.assertEqual((result/'util/build-win32.sh').read_bytes(), (self.checkout/'util/build-win32.sh').read_bytes())
        self.assertTrue(any((result/'bundle').glob('lua-resty-core-*/lib/resty/core.lua')))
        if os.name == 'nt':
            self.assertFalse(any((result/'bundle').glob('lua-resty-openssl-*/t')))
        self.assertEqual(result, self.prepare())

    def test_any_nginx_source_edit_selects_a_new_build_directory(self):
        first = self.prepare()
        relative = Path('src/http/ngx_http_request.c')
        source = self.checkout/'bundle'/self.module.NGINX/relative
        before = source.read_bytes()
        source.write_bytes(before+b'\n/* direct source cache regression */\n')
        second = self.prepare()
        self.assertNotEqual(first, second)
        self.assertEqual((first/'bundle'/self.module.NGINX/relative).read_bytes(), before)
        self.assertEqual((second/'bundle'/self.module.NGINX/relative).read_bytes(), source.read_bytes())

    def test_modified_cached_source_is_rejected(self):
        result = self.prepare()
        cached = result/'bundle'/self.module.NGINX/'src/http/modules/ngx_http_mp4_module.c'
        cached.write_bytes(cached.read_bytes()+b'\n/* unexpected cache edit */\n')
        with self.assertRaisesRegex(RuntimeError, 'Cached Nginx source differs'):
            self.prepare()

    def test_release_archive_checksum_is_required(self):
        archive = self.work/f'openresty-{self.module.VERSION}.tar.gz'
        archive.write_bytes(b'invalid release')
        with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
            self.prepare()


if __name__ == '__main__':
    unittest.main(verbosity=2)
