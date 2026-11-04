#!/usr/bin/env python3
"""The fixed gateway carries verified scripts through packaging/relocation."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeAssets(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.prefix = self.base/'runtime'
        binary = self.prefix/'nginx/sbin/nginx'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'synthetic binary; media execution tested separately')
        spec = importlib.util.spec_from_file_location('assets', ROOT/'langslice/install-runtime.py')
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.manifest = self.tool.install(self.prefix)

    def test_complete_fixed_assets_survive_relocation(self):
        relocated = self.base/'moved'
        shutil.move(self.prefix, relocated)
        self.assertEqual(self.tool.verify(relocated), self.manifest)
        self.assertEqual((relocated/'lualib/langslice/reader.lua').read_bytes(),
                         (ROOT/'langslice/reader.lua').read_bytes())

    def test_modified_or_missing_script_is_rejected(self):
        target = self.prefix/'lualib/langslice/artifact_io.lua'
        target.write_bytes(b'changed script')
        with self.assertRaisesRegex(ValueError, '脚本缺失或已修改'):
            self.tool.verify(self.prefix)
        target.unlink()
        with self.assertRaisesRegex(ValueError, '脚本缺失或已修改'):
            self.tool.verify(self.prefix)

    def test_version_or_mp4_source_mismatch_is_rejected(self):
        path = self.prefix/'langslice/manifest.json'
        for key in ('version', 'mp4_source_sha256'):
            changed = {**self.manifest, key: 'other build'}
            path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                self.tool.verify(self.prefix)

    def test_binary_version_and_modules_match_the_fixed_assets(self):
        valid = (f'nginx version: openresty/1.31.8.1 (langslice-mp4-timeline-v1-{self.tool.VERSION}) '
                 '--with-http_mp4_module ngx_lua --with-threads')
        self.tool.check_binary(valid, require_threads=True)
        for invalid in (valid.replace(self.tool.VERSION, 'other-build'),
                        valid.replace(self.tool.VERSION, self.tool.VERSION+'0'),
                        valid.replace('--with-threads', ''),
                        valid+' --without-http_proxy_module'):
            with self.assertRaises(ValueError):
                self.tool.check_binary(invalid, require_threads=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
