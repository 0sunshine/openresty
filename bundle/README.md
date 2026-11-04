# 分支直接维护的 Nginx 源码

`nginx-1.31.8/` 为本分支直接维护的完整 Nginx 源码。基线来自 OpenResty 1.31.8.1 官方源码包中的 `bundle/nginx-1.31.8/`，已包含该版本的 OpenResty 集成改动，保留原始许可证、文件内容及可执行权限。

官方源码包地址：<https://openresty.org/download/openresty-1.31.8.1.tar.gz>。

SHA-256：`b569c63b420f80a4ac8466f2deffe73ed93ab760026532e039c3658409a997f4`。

业务所需的 MP4 时间轴修改直接提交到 `nginx-1.31.8/src/http/modules/ngx_http_mp4_module.c`。构建准备工具和打包配方均使用此目录中的源码；其他 OpenResty 组件沿用同一版本的官方源码包。
