#!/bin/sh
# All Linux gateway roles build the Nginx source tracked in the OpenResty submodule.
set -eu
prefix=${1:?Usage: langslice/build.sh ABSOLUTE_INSTALL_PREFIX}
case "$prefix" in /*) ;; *) echo '安装路径必须为绝对路径' >&2; exit 2;; esac
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
checkout=$(CDPATH= cd -- "$script_dir/.." && pwd)
build_python=${BUILD_PYTHON:-python3}
if [ ! -f "$checkout/util/prepare-mp4-bundle.py" ]; then
    echo '请先执行 git submodule update --init third_party/openresty。' >&2
    exit 2
fi
mkdir -p "$prefix/build"
source_dir=$("$build_python" "$checkout/util/prepare-mp4-bundle.py" --work-dir "$prefix/build")
gateway_version=$(cat "$source_dir/langslice/VERSION")
pcre=pcre2-10.49
if [ ! -s "$prefix/build/$pcre.tar.gz" ]; then
    curl --fail --location --silent --show-error --connect-timeout 15 --max-time 120 --retry 2 \
        --proto '=https' "https://github.com/PCRE2Project/pcre2/releases/download/$pcre/$pcre.tar.gz" \
        -o "$prefix/build/$pcre.tar.gz"
fi
printf '%s  %s\n' 929f0b20e62879252a15886b06c89f1edef61a363cbd5826fb041080a5e557ae \
    "$prefix/build/$pcre.tar.gz" | sha256sum -c - >&2
if [ ! -d "$prefix/build/$pcre" ]; then
    tar -xzf "$prefix/build/$pcre.tar.gz" -C "$prefix/build"
fi
cd "$source_dir"
./configure --prefix="$prefix" --build="langslice-mp4-timeline-v1-$gateway_version" \
    --with-http_mp4_module --with-threads --with-http_ssl_module \
    --with-pcre="$prefix/build/$pcre" --without-http_gzip_module
make -j"${BUILD_JOBS:-2}"
make install
"$build_python" "$source_dir/langslice/install-runtime.py" --prefix "$prefix"
"$prefix/nginx/sbin/nginx" -V
