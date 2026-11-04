"""Generate an instance-local MP4 Nginx config; current Python runtime."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def configure(data, work, listen, backend, nginx=None, *, storage_dirs=None, media_prefix='/_storage_media/'):
    # Endpoints are deployment settings, never client-supplied paths.
    if not all(re.fullmatch(r'(?:[A-Za-z0-9_.-]+|\[[0-9a-fA-F:]+\]):[0-9]{1,5}',v)
               and 1 <= int(v.rsplit(':',1)[1]) <= 65535 for v in (listen,backend)):
        raise ValueError('地址须为 主机:端口，例如 127.0.0.1:8020。')
    data,work = Path(data).resolve(),Path(work).resolve()
    from news_storage_node.volumes import storage_paths
    paths = storage_paths(data, storage_dirs)
    if not re.fullmatch(r'/[A-Za-z0-9_/-]+/', media_prefix):
        raise ValueError('存储媒体内部前缀无效。')
    if any(re.search(r'["\r\n$;{}]',str(p)) for p in (*paths.values(),work)):
        raise ValueError('目录含 Nginx 配置不支持的字符。')
    if not data.is_dir(): raise ValueError('数据目录必须预先创建。')
    work.mkdir(parents=True,exist_ok=True)
    (work/'logs').mkdir(exist_ok=True)
    (work/'temp').mkdir(exist_ok=True)
    # Override compiled-in build-host paths, including modules unused by this
    # server. The Windows binary requires the temp parent to exist.
    release = Path(getattr(sys,'_news_native_build',{}).get('directory') or Path(__file__).resolve().parents[1])
    command_environment = None
    if nginx is None and (release/'release.json').is_file():
        name = json.loads((release/'release.json').read_text(encoding='utf-8'))['environment_version']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',name) or name in {'.','..'}:
            raise ValueError('非法环境版本。')
        environment = release.parents[3]/'environments'/name
        nginx = environment/'tools/nginx'/('nginx.exe' if os.name=='nt' else 'sbin/nginx')
        if os.name != 'nt':
            command_environment = dict(os.environ,LD_LIBRARY_PATH=str(environment/'tools/lib'))
    flags = ''
    if nginx is not None:
        result = subprocess.run([str(nginx),'-V'],check=True,capture_output=True,text=True,env=command_environment)
        flags = result.stdout+result.stderr
    lua = ''
    if nginx is not None and 'ngx_lua' in flags:
        binary = Path(nginx).resolve()
        runtime = binary.parent if os.name == 'nt' else binary.parents[1]
        # Packaged storage keeps its public executable path and places the
        # OpenResty Lua runtime alongside it; standalone builds use its parent.
        library = runtime/'lualib'
        if not library.is_dir() and (runtime.parent/'lualib').is_dir():
            library = runtime.parent/'lualib'
        if re.search(r'["\r\n$;{}]',str(library)):
            raise ValueError('OpenResty 运行库目录含非法字符。')
        path = library.as_posix()
        lua = ('    lua_package_path "'+path+'/?.lua;'+path+'/?/init.lua;;";\n'
               '    lua_package_cpath "'+path+'/?.'+('dll' if os.name=='nt' else 'so')+';;";\n')
    temp = '    client_body_temp_path temp/client_body_temp;\n'
    for module in ('proxy','fastcgi','uwsgi','scgi'):
        if '--without-http_'+module+'_module' not in flags:
            temp += '    '+module+'_temp_path temp/'+module+'_temp;\n'
    template_file=Path(__file__).parent/'storage.conf.template'
    template = template_file.read_text(encoding='utf-8')
    server = template.replace('${STORAGE_LISTEN}',listen).replace('${STORAGE_BACKEND}',backend)
    location = template[template.index('    location /_storage_media/'):template.rindex('\n}')]
    extra = ''.join(location.replace('/_storage_media/', '/_storage_media/volumes/'+key+'/')
        .replace('${STORAGE_DATA_DIR}', '"'+root.as_posix().rstrip('/')+'/"')
        for key, root in sorted(paths.items()) if key != 'default')
    server = server[:server.rindex('\n}')] + '\n' + extra + server[server.rindex('\n}'):]
    server = server.replace('/_storage_media/', media_prefix)
    server = server.replace('${STORAGE_DATA_DIR}','"'+data.as_posix().rstrip('/')+'/"')
    server = server.replace('    # Compression must stay disabled (the lab build omits the gzip module).',
                            '    limit_rate 0;')
    text = '''worker_processes 1;
pid logs/nginx.pid;
error_log logs/error.log notice;
events { worker_connections 1024; }
http {
    access_log logs/access.log;
    default_type application/octet-stream;
'''+lua+temp+server+'\n}\n'
    target = work/'nginx.conf'
    if target.exists(): raise ValueError('配置已存在；保留旧文件后再生成新配置。')
    target.write_text(text,encoding='utf-8')
    return target


def main():
    parser = argparse.ArgumentParser(description='生成独立存储 Nginx 配置，一个 worker、单请求不限速。')
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--data-dir')
    source.add_argument('--env-file',help='读取与存储服务相同的目录列表和媒体前缀配置')
    parser.add_argument('--work-dir',required=True)
    parser.add_argument('--listen',default='0.0.0.0:8021')
    parser.add_argument('--backend',default='127.0.0.1:8020')
    parser.add_argument('--nginx-bin',help='默认按程序包绑定环境定位；单独使用脚本时填写实际 Nginx')
    args=parser.parse_args()
    options={}
    if args.env_file:
        from news_storage_node.config import StorageSettings
        settings=StorageSettings(_env_file=args.env_file)
        args.data_dir=settings.data_dir
        options=dict(storage_dirs=settings.storage_dirs,media_prefix=settings.nginx_media_prefix)
    print(configure(args.data_dir,args.work_dir,args.listen,args.backend,args.nginx_bin,**options))


if __name__ == '__main__': main()
