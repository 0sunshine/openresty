# 固定版本媒体网关

本目录是网管、拆条和存储网关脚本的唯一维护位置。版本为 `VERSION` 中的 `1.31.8.1-langslice.1`，包含 OpenResty 1.31.8.1、直接维护的 Nginx 1.31.8、MP4 时间轴修复、Lua、SSL、PCRE2 10.49 和 Linux 线程池。MP4 修复范围见[源码说明](../doc/README-mp4-timeline.md)。

同一目标平台和系统兼容基线只编译一次，后续各角色复用固定产物；Linux 与 Windows 分别构建。脚本随网关版本发布，业务程序更新不重新编译或替换网关。修改网关 C 代码、Lua 或配置生成规则时更新本版本并重新验收。

## 构建与交付

Linux 制包机准备 Python 3.12、GCC、Make、Perl、curl、tar、sha256sum 和 OpenSSL 开发依赖，在本 OpenResty 仓库根目录执行：

```sh
sh langslice/build.sh /absolute/openresty-runtime
python3 langslice/install-runtime.py --prefix /absolute/openresty-runtime --verify
```

`BUILD_PYTHON`、`BUILD_JOBS` 可指定解释器和并行数。构建校验官方源码摘要，直接使用本分支的 Nginx 源码，不调用 MP4 补丁。运行时包含：

```text
nginx/sbin/nginx              Linux 二进制；Windows 为 nginx.exe
lualib/                      OpenResty Lua 与原生模块
lualib/langslice/*.lua        拆条节点鉴权、准入、SQLite 和对象读取
luajit/lib/                  Linux LuaJIT 动态库
langslice/                   配置生成器、存储模板、版本和 manifest.json
```

`manifest.json` 固定配套脚本的 SHA-256 及 MP4 源码摘要。制包器核对版本和脚本摘要，复制完整运行时；禁止混用另一版本的脚本。运行时不得携带现场凭据、配置或编译目录。

Windows 在准备好的源码目录运行 `BUILD_PYTHON=python3 bash util/build-win32.sh`；安装同时收集配套脚本。依赖构建和 Server 2012 限制见[MP4 源码说明](../doc/README-mp4-timeline.md)。**Server 2012 尚未实机验收**。

## 角色配置

`configure-manager.py` 生成网管内部媒体转发配置，默认从 `PREFIX/runtime/lualib` 加载 Lua，也可传 `--runtime`。

`configure-node.py` 读取部署节点的 `config/node.yaml`，生成共享主机网关。必须传 `--runtime-prefix`，Lua 直接从固定运行时的 `lualib/langslice` 加载，配置目录不再复制 Lua，节点程序包也不交付 Lua。

`configure-storage.py` 和 `storage.conf.template` 维护存储内部 MP4 映射。存储生成器使用业务程序提供的目录校验及配置读取能力。

业务程序的 `--configure-gateway` 入口在编译时仅引用本目录源码，生成器摘要纳入构建身份与开发侧源码快照。现场只保存实例配置，凭据不提交仓库。配置变更后先执行 `nginx -t`，再按现场手动启停规则加载。

`start/end` 使用原片 movie presentation timeline，前端不减去单独的视频轨编辑偏移。部署验证须覆盖 MP4、Lua/SQLite 读取、HEAD、Range、鉴权、节点隔离及网管内部转发。
