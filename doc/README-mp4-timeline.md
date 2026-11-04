# MP4 虚拟片段的电影时间轴

本分支以 `v1.31.8.1`（`265baf62de60397452f306d61b168ab2c1809d41`）为基线，修改 Nginx 1.31.8 的 `ngx_http_mp4_module`，构建标识为 `langslice-mp4-timeline-v1`。

## 修复范围

`?start=T&end=E` 使用原 MP4 的 **movie presentation timeline**。模块读取各轨道的 `edts/elst`，将请求映射到轨道媒体时间，保留源音视频的相对偏移。输出编辑列表合并原片偏移、关键帧预卷和不足一个采样周期的余量。匹配数据包的呈现时间满足 `output_pts = source_pts - T`；帧间切点以空编辑保留下一视频帧之前的时间，不把该帧强行提前。

音视频压缩数据直接复制，不转码；源 MP4 不改写。不带时间参数的完整文件、HEAD、字节 Range 和其他查询参数保持静态文件语义。片段保留解码需要的前后样本；`mdhd` 描述保留样本的解码时长，`mvhd/tkhd/elst` 描述请求的呈现区间，二者可能不同。

支持版本 0/1 编辑列表、前导空编辑和一个连续、速率为 1 的媒体编辑，以及有符号 CTTS。STTS/CTTS 按压缩后的 run 表合并扫描，不展开成逐帧表。复杂的重复编辑、变速、停帧、多段可见编辑及损坏编辑列表明确返回错误并记录日志，不静默丢弃偏移。

补丁位于 `patches/nginx/1.31.8/nginx-1.31.8-mp4-timeline.patch`。OpenResty Git 仓库保存打包配方而非生成后的 Nginx 源码；`util/mirror-tarballs` 已登记该补丁。

## 可复现构建

用 Python 准备固定摘要的官方源码包，再应用当前分支补丁：

```sh
python3.12 util/prepare-mp4-bundle.py --work-dir /absolute/build
cd /absolute/build/openresty-1.31.8.1-langslice-<输出的标识>
./configure --prefix=/absolute/runtime --build=langslice-mp4-timeline-v1 \
    --with-http_mp4_module --with-threads --with-http_ssl_module
make -j4
make install
```

准备工具需要 GNU patch，校验官方 OpenResty 1.31.8.1 源码包 SHA-256：`b569c63b420f80a4ac8466f2deffe73ed93ab760026532e039c3658409a997f4`。生成目录按补丁及 Windows 构建脚本内容隔离缓存。

Windows 在原生 MSYS2 MINGW64 中运行生成目录下的 `bash util/build-win32.sh`，使用本版本的 PCRE2 10.49、zlib 1.3.2、OpenSSL 3.5.9。选择面向 Server 2012 的编译器和运行库，禁止把近期 MSYS2 的预编译依赖视为已兼容旧系统。此补丁没有新增平台 API，但 **Windows Server 2012 尚未实机验收**；须验证启动、Lua、MP4、HEAD/Range、SSL 和实际片源后再发布。Windows 不使用 Linux 线程池配置。

## 回归

```sh
python3.12 t/mp4_timeline.py \
    --nginx-bin /absolute/runtime/nginx/sbin/nginx \
    --ffmpeg-bin /absolute/ffmpeg --ffprobe-bin /absolute/ffprobe \
    --work-dir /absolute/isolated-test
```

测试需要带 libx264 的 FFmpeg 6 或更新版本，用它生成测试素材；网关截取不依赖 FFmpeg。测试覆盖音视频延迟、B 帧、开放 GOP、有符号 CTTS、无编辑列表、版本 1、`edts` 位于 `mdia` 后、不同采样时钟、非整帧切点、只有 start/end、HEAD、字节 Range、完整文件一致性、损坏编辑列表和严格解码。

通过 `OPENRESTY_MP4_REAL_SOURCE`、`OPENRESTY_MP4_REAL_START`、`OPENRESTY_MP4_REAL_END` 可增加现场素材的独立副本。测试按压缩数据包 SHA-256 匹配原片与片段，并检查所有匹配包的共同 PTS 平移量，不用播放器主观观感代替时间轴验证。

接入时直接传原片 movie 时间，移除针对旧模块忽略编辑列表的单轨偏移补偿。所有负责 MP4 虚拟截取的网关应使用同一补丁版本。
