"""Generate a private manager gateway with registered, internal-only media upstreams."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


def quoted(value):
    if any(char in value for char in '\r\n\0'):
        raise ValueError('Nginx 配置值不能包含控制字符。')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('$','\\$')+'"'


def address(value, *, loopback=False):
    host,port=value.rsplit(':',1)
    host=host.strip('[]')
    ip=ipaddress.ip_address(host)
    if loopback and not ip.is_loopback:raise ValueError('网管后端只允许监听回环地址。')
    if not 1<=int(port)<=65535:raise ValueError('端口超出范围。')
    return ('['+str(ip)+']' if ip.version==6 else str(ip))+':'+str(int(port))


def configure(prefix,nodes,listen='0.0.0.0:8060',backend='127.0.0.1:18060',*,runtime=None):
    prefix=Path(prefix)
    if not prefix.is_absolute():raise ValueError('Nginx 目录必须为绝对路径。')
    listen=address(listen);backend=address(backend,loopback=True)
    blocks=[];seen=set()
    for node in nodes:
        key=node['id']
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}',key) or key in seen:
            raise ValueError('节点 ID 无效或重复。')
        seen.add(key)
        role=node['role']
        if role not in {'compute','storage'}:raise ValueError('未知节点角色。')
        base=node.get('media_base_url') if role=='compute' else node.get('base_url')
        if not base and role=='compute':continue
        parsed=urlsplit(base or '')
        if (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(char in base for char in '\r\n\0')):
            raise ValueError('节点媒体上游地址无效。')
        token=node.get('token','') if role=='storage' else ''
        if role=='storage' and not token:raise ValueError('存储节点缺少私有凭据。')
        blocks.append(f'''        location ^~ /_manager_media/{key}/ {{
            internal;
            proxy_pass {quoted(base.rstrip('/')+'/')};
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Authorization {quoted('Bearer '+token if token else '')};
            proxy_set_header Accept-Encoding "";
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_max_temp_file_size 0;
            proxy_cache off;
            proxy_read_timeout 300s;
        }}
''')
    library=(Path(runtime) if runtime else prefix/'runtime')/'lualib'
    if not library.is_absolute():raise ValueError('OpenResty 运行时目录必须为绝对路径。')
    lua='    lua_package_path '+quoted(library.as_posix()+'/?.lua;'+library.as_posix()+'/?/init.lua;;')+';\n'
    lua+='    lua_package_cpath '+quoted(library.as_posix()+'/?.so;;')+';\n'
    config='''worker_processes 2;
pid logs/nginx.pid;
error_log logs/error.log warn;
events { worker_connections 512; }
http {
LUA
    default_type application/octet-stream;
    server_tokens off;
    access_log logs/access.log;
    upstream manager_backend { server BACKEND; keepalive 32; }
    server {
        listen LISTEN;
        client_max_body_size 0;
MEDIA        location ^~ /_manager_media/ { internal; deny all; }
        location / {
            proxy_pass http://manager_backend;
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Connection "";
            proxy_set_header Accept-Encoding "";
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_max_temp_file_size 0;
            proxy_cache off;
            proxy_read_timeout 300s;
        }
    }
}
'''.replace('BACKEND',backend).replace('LISTEN',listen).replace('MEDIA',''.join(blocks)).replace('LUA',lua,1)
    (prefix/'conf').mkdir(parents=True,exist_ok=True)
    (prefix/'logs').mkdir(exist_ok=True)
    target=prefix/'conf/manager.conf'
    with os.fdopen(os.open(target,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600),'w',encoding='utf-8') as stream:
        stream.write(config)
    target.chmod(0o600)
    return target


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',required=True)
    parser.add_argument('--runtime',help='OpenResty 安装根目录；默认 PREFIX/runtime')
    parser.add_argument('--nodes-file',required=True,help='现场私有节点清单；存储凭据不写入命令行或仓库')
    parser.add_argument('--listen',default='0.0.0.0:8060')
    parser.add_argument('--backend',default='127.0.0.1:18060')
    options=parser.parse_args(argv)
    print(configure(options.prefix,json.loads(Path(options.nodes_file).read_text(encoding='utf-8')),
                    options.listen,options.backend,runtime=options.runtime))


if __name__=='__main__':
    main()
