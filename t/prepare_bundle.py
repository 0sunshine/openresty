#!/usr/bin/env python3
"""Exercise source assembly and cache behavior without an external patch tool."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
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
        shutil.copytree(ROOT/'langslice', self.checkout/'langslice',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
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

    def test_modified_cached_windows_build_script_is_rejected(self):
        result = self.prepare()
        (result/'util/build-win32.sh').write_text('unexpected cache edit')
        with self.assertRaisesRegex(RuntimeError, 'Cached Windows build script differs'):
            self.prepare()

    def test_gateway_edits_select_a_new_bundle_and_cache_is_verified(self):
        first = self.prepare()
        source = self.checkout/'langslice/reader.lua'
        source.write_bytes(source.read_bytes()+b'\n-- changed gateway source\n')
        second = self.prepare()
        self.assertNotEqual(first, second)
        cached = second/'langslice/reader.lua'
        self.assertEqual(cached.read_bytes(), source.read_bytes())
        cached.write_bytes(b'bad cached gateway script')
        with self.assertRaisesRegex(RuntimeError, 'Cached gateway source differs'):
            self.prepare()

    def test_in_tree_build_products_do_not_enter_source_bundle(self):
        tree = self.checkout/'bundle'/self.module.NGINX
        (tree/'objs').mkdir()
        (tree/'objs/nginx').write_bytes(b'stale executable')
        (tree/'Makefile').write_text('stale build configuration')
        result = self.prepare()/'bundle'/self.module.NGINX
        self.assertFalse((result/'objs').exists())
        self.assertFalse((result/'Makefile').exists())

    @unittest.skipIf(os.name == 'nt', 'upstream mirror tool requires a Unix shell')
    def test_mirror_uses_clean_direct_source(self):
        tree = self.checkout/'bundle'/self.module.NGINX
        (tree/'objs').mkdir()
        (tree/'objs/nginx').write_bytes(b'stale executable')
        (tree/'Makefile').write_text('stale build configuration')
        shutil.copy2(ROOT/'util/ver', self.checkout/'util/ver')
        patch_dir = self.checkout/'patches/nginx/1.31.8'
        patch_dir.mkdir(parents=True)
        (patch_dir/'nginx-1.31.8-no_pool.patch').write_text('')
        # Stop before dependency downloads; exercise the actual mirror stage.
        prefix = (ROOT/'util/mirror-tarballs').read_text().split('ver=0.65', 1)[0]
        script = self.checkout/'util/mirror-prefix'
        script.write_text(prefix)
        subprocess.run(['bash', str(script)], cwd=self.checkout, check=True,
                       capture_output=True, timeout=30)
        result = self.checkout/f'openresty-{self.module.VERSION}/bundle'/self.module.NGINX
        self.assertFalse((result/'objs').exists())
        self.assertFalse((result/'Makefile').exists())
        for relative, source, _ in self.module.source_files(tree):
            self.assertEqual((result/relative).read_bytes(), source.read_bytes())


if __name__ == '__main__':
    unittest.main(verbosity=2)
