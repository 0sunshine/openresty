#!/usr/bin/env python3
"""Prepare OpenResty using the Nginx source maintained directly in this branch.

The matching official archive supplies the other OpenResty components. Its
Nginx tree is excluded: the complete branch-owned tree is the build input.
"""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import time
import urllib.request

VERSION = '1.31.8.1'
NGINX = 'nginx-1.31.8'
SHA256 = 'b569c63b420f80a4ac8466f2deffe73ed93ab760026532e039c3658409a997f4'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def source_files(directory):
    if directory.is_symlink() or not (directory/'src/core/nginx.h').is_file():
        raise RuntimeError(f'Missing branch-owned Nginx source: {directory}')
    files = []
    for path in sorted(directory.rglob('*')):
        relative = path.relative_to(directory)
        if relative.parts[0] == 'objs' or relative.as_posix() == 'Makefile':
            continue
        if path.is_symlink():
            raise RuntimeError(f'Nginx source must be a regular file: {path}')
        if path.is_file():
            files.append((relative, path, digest(path)))
    return files


def prepare(work):
    root = Path(__file__).resolve().parents[1]
    files = source_files(root/'bundle'/NGINX)
    fingerprint = hashlib.sha256()
    fingerprint.update(f'{VERSION}\0{os.name}\0'.encode())
    inputs = [(Path('bundle')/NGINX/relative, checksum) for relative, _, checksum in files]
    inputs += [(Path('util')/name, digest(root/'util'/name))
               for name in ['prepare-mp4-bundle.py', 'build-win32.sh']]
    for name, checksum in inputs:
        fingerprint.update(name.as_posix().encode()+b'\0'+checksum.encode()+b'\0')
    identity = fingerprint.hexdigest()
    work.mkdir(parents=True, exist_ok=True)
    destination = work/f'openresty-{VERSION}-langslice-{identity[:16]}'
    marker = destination/'.langslice-mp4-source'
    if marker.is_file() and marker.read_text() == identity:
        for relative, _, checksum in files:
            cached = destination/'bundle'/NGINX/relative
            if not cached.is_file() or digest(cached) != checksum:
                raise RuntimeError(f'Cached Nginx source differs from branch: {cached}')
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
            excluded = f'openresty-{VERSION}/bundle/{NGINX}'
            members = []
            for member in source.getmembers():
                if member.name.rstrip('/') == excluded or member.name.startswith(excluded+'/'):
                    continue
                parts = PurePosixPath(member.name).parts
                # Dependency test fixtures contain DOS device names such as
                # t/openssl/aux. They are not inputs to native Windows builds.
                if os.name == 'nt' and len(parts) > 3 and parts[1] == 'bundle' and 't' in parts[3:]:
                    continue
                members.append(member)
            source.extractall(staging, members=members, filter='data')
        bundle = staging/f'openresty-{VERSION}'
        for relative, path, checksum in files:
            target = bundle/'bundle'/NGINX/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            if digest(target) != checksum:
                raise RuntimeError(f'Nginx source changed while preparing: {path}')
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
