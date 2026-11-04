"""生成主机共享网关配置；凭据仅写入权限为 0600 的部署副本。"""
import argparse
import os
from pathlib import Path
import re
import pwd
import yaml


def quoted(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('$','\\$')+'"'


def configure(prefix, nodes, listen='8030', runtime_prefix=None):
    if not re.fullmatch(r'(?:[0-9.]+:)?[0-9]{1,5}',listen):
        raise ValueError('监听地址填写端口或 IPv4:端口。')
    prefix=Path(prefix).resolve()
    conf=prefix/'nginx/conf';conf.mkdir(parents=True,exist_ok=True)
    (prefix/'nginx/logs').mkdir(exist_ok=True)
    if runtime_prefix is None:
        raise ValueError('必须指定固定 OpenResty 运行时 --runtime-prefix。')
    runtime=Path(runtime_prefix).resolve()/'lualib'
    lua=runtime/'langslice'
    paths=[str(lua)+'/?.lua',str(runtime)+'/?.lua',str(runtime)+'/?/init.lua']
    native='    lua_package_cpath '+quoted(str(runtime)+'/?.so;;')+';\n'
    for name in ('reader.lua','artifact_io.lua','reader_gate.lua'):
        if not (lua/name).is_file():raise ValueError('OpenResty 运行时缺少 Lua 脚本：'+name)
    blocks=[];seen=set()
    for cwd in nodes:
        file=Path(cwd).resolve()/'config/node.yaml'
        value=yaml.safe_load(file.read_text(encoding='utf8'))['node']
        key=value['node_id'];token=value['token']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',key) or key in seen:
            raise ValueError('节点 ID 无效或重复。')
        if not isinstance(token,str) or not token or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError('节点凭据无效。')
        seen.add(key)
        root=(file.parent/value['data_dir']).resolve()
        # Literal roots and credentials cannot be supplied by a client request.
        blocks.append(f'''
        location ~ ^/media/{key}/internal/v1/jobs/([A-Za-z0-9_-]+)/(.+)$ {{
            set $node_root {quoted(root)};
            set $node_token {quoted(token)};
            set $reader_job $1;
            set $reader_action $2;
            content_by_lua_file {quoted(lua/'reader.lua')};
        }}
        location ~ ^/media/{key}/(jobs/.+\\.[mM][pP]4)$ {{
            alias {quoted(str(root)+'/$1')};
            mp4;
            mp4_start_key_frame on;
            disable_symlinks on;
            mp4_buffer_size 1m;
            mp4_max_buffer_size 128m;
            default_type video/mp4;
            etag off;
            if_modified_since off;
            add_header Cache-Control "no-store" always;
        }}
'''.replace('\\$1','$1'))
    config=f'''user {pwd.getpwuid(os.getuid()).pw_name};
worker_processes 1;
pid logs/node-media.pid;
error_log logs/node-media-error.log notice;
thread_pool artifact_read threads=4 max_queue=128;
events {{ worker_connections 1024; }}
http {{
    access_log logs/node-media-access.log;
    default_type application/octet-stream;
    sendfile on;
    limit_rate 0;
    lua_check_client_abort on;
    lua_package_path {quoted(';'.join(paths)+';;')};
{native}    lua_worker_thread_vm_pool_size 4;
    server {{
        listen {listen};
{''.join(blocks)}
        location / {{ return 404; }}
    }}
}}
'''
    target=conf/'node-media.conf'
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(descriptor,'w',encoding='utf8') as stream:stream.write(config)
    target.chmod(0o600)
    return target


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',required=True)
    parser.add_argument('--node',action='append',required=True,help='节点启动工作目录，可重复')
    parser.add_argument('--listen',default='8030')
    parser.add_argument('--runtime-prefix',required=True,help='固定 OpenResty 运行时目录；配置使用包内 Lua 与原生模块')
    args=parser.parse_args()
    print(configure(args.prefix,args.node,args.listen,args.runtime_prefix))


if __name__=='__main__':
    main()
