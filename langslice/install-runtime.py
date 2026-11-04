#!/usr/bin/env python3
"""Install and verify the gateway assets delivered with one OpenResty build."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

TOOLS = ('configure-manager.py', 'configure-node.py', 'configure-storage.py',
         'storage.conf.template', 'install-runtime.py', 'VERSION')
LUA = ('reader.lua', 'artifact_io.lua', 'reader_gate.lua')
FILES = {**{f'langslice/{name}': name for name in TOOLS},
         **{f'lualib/langslice/{name}': name for name in LUA}}
VERSION = (Path(__file__).with_name('VERSION')).read_text(encoding='utf-8').strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_binary(text, *, require_threads=False):
    flags = (f'nginx version: openresty/{VERSION.split("-")[0]} (langslice-mp4-timeline-v1-{VERSION})',
             '--with-http_mp4_module', 'ngx_lua')
    if (not all(flag in text for flag in flags) or '--without-http_proxy_module' in text
            or (require_threads and '--with-threads' not in text)):
        raise ValueError('OpenResty 二进制版本或所需模块与固定网关版本不符。')


def verify(prefix):
    prefix = Path(prefix)
    manifest = json.loads((prefix/'langslice/manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or manifest.get('version') != VERSION or set(manifest.get('files', {})) != set(FILES):
        raise ValueError('OpenResty 网关版本或脚本清单不符，请使用固定构建产物。')
    module = Path(__file__).resolve().parent.parent/'bundle/nginx-1.31.8/src/http/modules/ngx_http_mp4_module.c'
    if module.is_file() and manifest.get('mp4_source_sha256') != digest(module):
        raise ValueError('OpenResty MP4 源码版本与固定构建不符。')
    for name, expected in manifest['files'].items():
        path = prefix/name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError('OpenResty 网关脚本缺失或已修改：'+name)
        if module.is_file() and digest(Path(__file__).resolve().parent/FILES[name]) != expected:
            raise ValueError('OpenResty 网关脚本与固定源码不符：'+name)
    return manifest


def install(prefix):
    prefix = Path(prefix).resolve()
    if not any((prefix/name).is_file() for name in ('nginx/sbin/nginx', 'nginx.exe')):
        raise ValueError('请先编译并安装 OpenResty，再安装配套脚本。')
    source = Path(__file__).resolve().parent
    checksums = {}
    for relative, name in FILES.items():
        target = prefix/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if (source/name).resolve() != target.resolve():
            shutil.copy2(source/name, target)
        checksums[relative] = digest(target)
    module = source.parent/'bundle/nginx-1.31.8/src/http/modules/ngx_http_mp4_module.c'
    manifest = {'format': 1, 'version': VERSION, 'files': checksums,
                'mp4_source_sha256': digest(module)}
    (prefix/'langslice/manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return verify(prefix)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    options = parser.parse_args()
    print(json.dumps((verify if options.verify else install)(options.prefix), ensure_ascii=False))
