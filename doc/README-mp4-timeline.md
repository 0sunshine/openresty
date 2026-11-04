# MP4 虚拟片段的电影时间轴

本分支以 `v1.31.8.1`（`265baf62de60397452f306d61b168ab2c1809d41`）为基线，修改 Nginx 1.31.8 的 `ngx_http_mp4_module`，构建标识为 `langslice-mp4-timeline-v1`。

## 修复范围

`?start=T&end=E` 使用原 MP4 的 **movie presentation timeline**，请求精度沿用模块的毫秒精度。模块读取各轨道的 `edts/elst`，将请求映射到轨道媒体时间，保留源音视频的相对偏移。输出编辑列表合并原片偏移、关键帧预卷和不足一个采样周期的余量。匹配数据包的呈现时间满足 `output_pts = source_pts - T`（允许时钟换算的 tick 误差）。切点落在视频帧的显示区间内时，仅将这张已显示帧的 CTTS 调整到切点，使其在输出时间 0 继续显示；后续帧仍保持共同平移量。真正没有视频画面的前导区间由空编辑保留。

音视频压缩数据直接复制，不转码；源 MP4 不改写。不带时间参数的完整文件、HEAD、字节 Range 和其他查询参数保持静态文件语义。片段保留解码需要的前后样本；`mdhd` 描述保留样本的解码时长，`mvhd/tkhd/elst` 描述请求的呈现区间，二者可能不同。

支持版本 0/1 编辑列表、前导空编辑和一个连续、速率为 1 的媒体编辑，以及有符号 CTTS。空 `edts`、零条目 `elst` 按无偏移处理，纯空编辑允许再次截取。STTS/CTTS 按压缩后的 run 表合并扫描，不展开成逐帧表；核对其样本总数与 STSZ 一致，忽略不包含样本的空 run，并在输出中省略空 run。非空 run 的零解码时长及无法表示的时间值返回错误。复杂的重复编辑、变速、停帧、多段可见编辑及损坏编辑列表明确返回错误并记录日志，不静默丢弃偏移。

实现直接位于 `bundle/nginx-1.31.8/src/http/modules/ngx_http_mp4_module.c`，完整 Nginx 源码随本分支维护，来源见 `bundle/README.md`。功能修改直接提交为 C 文件的差异。`util/mirror-tarballs` 直接使用分支源码树。

## 可复现构建

用 Python 准备构建目录。Nginx 使用当前分支中的完整源码树，其他组件来自固定摘要的官方源码包：

```sh
python3.12 util/prepare-mp4-bundle.py --work-dir /absolute/build
cd /absolute/build/openresty-1.31.8.1-langslice-<输出的标识>
./configure --prefix=/absolute/runtime --build=langslice-mp4-timeline-v1 \
    --with-http_mp4_module --with-threads --with-http_ssl_module
make -j4
make install
```

准备工具校验官方 OpenResty 1.31.8.1 源码包 SHA-256：`b569c63b420f80a4ac8466f2deffe73ed93ab760026532e039c3658409a997f4`，排除其中的 Nginx 目录，复制分支中维护的完整源码。此步骤不调用 patch。生成目录按平台、全部 Nginx 源文件、准备工具及 Windows 构建脚本的内容隔离缓存；命中缓存后也校验 Nginx 文件及 Windows 构建脚本的内容。准备工具和 mirror 均排除 Nginx 源码树内的 `objs/` 和根 `Makefile`，避免混入旧编译产物。Linux 默认构建无需 GNU patch。Windows 准备时省略依赖组件的 `t/` 测试目录，避免其中的 `aux` 等 Windows 保留名；编译所需库文件及完整 Nginx 源码均保留。

Windows 在原生 MSYS2 MINGW64 中运行生成目录下的 `bash util/build-win32.sh`，使用本版本的 PCRE2 10.49、zlib 1.3.2、OpenSSL 3.5.9，依赖构建沿用上游脚本。选择面向 Server 2012 的编译器和运行库，禁止把近期 MSYS2 的预编译依赖视为已兼容旧系统。MP4 修复没有新增平台 API，但 **Windows Server 2012 尚未实机验收**；须验证启动、Lua、MP4、HEAD/Range、SSL 和实际片源后再发布。Windows 不使用 Linux 线程池配置。

## 回归

```sh
OPENRESTY_RELEASE_ARCHIVE=/absolute/openresty-1.31.8.1.tar.gz python3.12 t/prepare_bundle.py
python3.12 t/mp4_timeline.py \
    --nginx-bin /absolute/runtime/nginx/sbin/nginx \
    --ffmpeg-bin /absolute/ffmpeg --ffprobe-bin /absolute/ffprobe \
    --work-dir /absolute/isolated-test
```

测试需要带 libx264 的 FFmpeg 6 或更新版本，用它生成测试素材；网关截取不依赖 FFmpeg。测试覆盖音视频延迟、B 帧、开放 GOP、有符号 CTTS、无编辑列表、版本 1、空编辑盒和空 run、`edts` 位于 `mdia` 后、变帧率长帧、非整帧切点、末帧、多音轨和短视频轨、粗 movie 时钟、64 位头与超过 32 位 tick 的关键帧预卷、只有 start/end、HEAD、字节 Range、完整文件一致性、损坏时间表和严格解码。并发用例检查请求间修改的 CTTS 表互不污染；Linux 另用稀疏文件验证超过 4 GiB 的 CO64 地址和扩展 MDAT 头。

通过 `OPENRESTY_MP4_REAL_SOURCE`、`OPENRESTY_MP4_REAL_START`、`OPENRESTY_MP4_REAL_END` 可增加现场素材的独立副本。测试按压缩数据包 SHA-256 匹配原片与片段，并检查共同 PTS 平移量及上述首张显示帧的裁切例外；长帧用例还比较解码后的首帧摘要。

FFmpeg MOV 解封装有一个单独的兼容边界：它使用编辑区间时长推断最后一个音频包的时长。截取尾部不足一个 AAC 包的极短片段时（例如 21 ms 包中只取最后 10 ms），可能错误丢弃该包。该用例以忽略编辑列表读取的原始包时间和独立解析的编辑列表核对 ISO 时间轴，不能据此宣称 FFmpeg 或所有播放器均能正常播放这种极短音频片段。参见 [FFmpeg MOV 解封装代码](https://github.com/FFmpeg/FFmpeg/blob/n7.1/libavformat/mov.c)。

接入时直接传原片 movie 时间，移除针对旧模块忽略编辑列表的单轨偏移补偿。所有负责 MP4 虚拟截取的网关应使用同一修复版本。
