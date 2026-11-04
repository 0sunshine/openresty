#!/usr/bin/env python3
"""Prepare a checksum-pinned release with this fork's MP4 patch applied.

The Git repository contains OpenResty's bundle recipe and patches, not its
generated nginx source tree.  This shortcut uses the matching official bundle
so Linux and native Windows builds do not need every bundle-maintainer tool.
"""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

VERSION = '1.31.8.1'
SHA256 = 'b569c63b420f80a4ac8466f2deffe73ed93ab760026532e039c3658409a997f4'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def prepare(work):
    root = Path(__file__).resolve().parents[1]
    patch = root/'patches/nginx/1.31.8/nginx-1.31.8-mp4-timeline.patch'
    fingerprint = hashlib.sha256()
    for path in [patch, root/'util/build-win32.sh']:
        fingerprint.update(path.read_bytes())
    identity = fingerprint.hexdigest()
    work.mkdir(parents=True, exist_ok=True)
    destination = work/f'openresty-{VERSION}-langslice-{identity[:16]}'
    marker = destination/'.langslice-mp4-source'
    if marker.is_file() and marker.read_text() == identity:
        return destination
    if destination.exists():
        raise RuntimeError(f'Unrecognized existing build directory: {destination}')

    archive = work/f'openresty-{VERSION}.tar.gz'
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=work, suffix='.download', delete=False) as output:
            temporary = Path(output.name)
            try:
                deadline = time.monotonic()+120
                with urllib.request.urlopen(f'https://openresty.org/download/openresty-{VERSION}.tar.gz',
                                            timeout=30) as response:
                    while block := response.read(1024*1024):
                        if time.monotonic() > deadline:
                            raise TimeoutError('Source download deadline exceeded')
                        output.write(block)
                output.close()
                if digest(temporary) != SHA256:
                    raise RuntimeError('OpenResty release checksum mismatch')
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
    if digest(archive) != SHA256:
        raise RuntimeError('OpenResty release checksum mismatch')

    with tempfile.TemporaryDirectory(prefix='.prepare-mp4-', dir=work) as directory:
        staging = Path(directory)
        with tarfile.open(archive) as source:
            source.extractall(staging, filter='data')
        bundle = staging/f'openresty-{VERSION}'
        subprocess.run(['patch', '--batch', '--forward', '-p1', '-i', str(patch)],
                       cwd=bundle/'bundle/nginx-1.31.8', check=True,
                       stdout=sys.stderr, stderr=sys.stderr)
        (bundle/'util').mkdir(exist_ok=True)
        shutil.copyfile(root/'util/build-win32.sh', bundle/'util/build-win32.sh')
        (bundle/'.langslice-mp4-source').write_text(identity)
        bundle.replace(destination)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.work_dir.resolve()))
