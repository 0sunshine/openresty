-- HTTP 协程只负责参数、响应头和网络发送，SQLite 与文件读取交给线程池。
local ffi=require 'ffi'
ffi.cdef 'int close(int);'
local json=require 'cjson.safe'
local function reply(code,message)
    ngx.status=code;ngx.header.content_type='application/json; charset=utf-8'
    ngx.say(json.encode({detail=message}))
end
if ngx.req.get_method()~='GET' and ngx.req.get_method()~='HEAD' then return reply(405,'只支持 GET 和 HEAD') end
if ngx.var.http_authorization~='Bearer '..ngx.var.node_token then return reply(401,'节点凭据无效') end
local args,err=ngx.req.get_uri_args(16)
if err then return reply(422,'请求参数过多') end
for _,value in pairs(args) do if type(value)~='string' then return reply(422,'请求参数不能重复') end end
local gate=require 'reader_gate'
if gate:count()<=-128 then return reply(503,'文件读取繁忙，请稍后重试') end
local acquired=gate:wait(15)
if not acquired then return reply(503,'文件读取繁忙，请稍后重试') end
local aborted=false
ngx.on_abort(function() aborted=true end)
local function serve()
local ok,meta=ngx.run_worker_thread('artifact_read','artifact_io','locate',
    ngx.var.node_root,ngx.var.reader_job,ngx.var.reader_action,args)
if not ok then return reply(503,'文件读取繁忙，请稍后重试') end
if meta.status then return reply(meta.status,meta.message) end
-- 文件描述符归当前响应所有；协程异常退出后 GC 也会释放，不缓存到 Lua 全局。
local handle=meta.fd and ffi.gc(ffi.new('int[1]',meta.fd),function(value) ffi.C.close(value[0]) end)
local function close()
    if handle then ffi.C.close(handle[0]);ffi.gc(handle,nil);handle=nil end
end
local start,finish,partial=0,meta.size-1,false
local range=ngx.var.http_range
if range then
    local a,b=range:match('^bytes=(%d*)%-(%d*)$')
    if a and meta.size>0 and (a~='' or b~='') then
        if a=='' then
            local suffix=tonumber(b)
            if suffix>0 then start=math.max(0,meta.size-suffix) else start=meta.size end
        else start=tonumber(a);finish=b~='' and math.min(tonumber(b),finish) or finish end
        partial=true
    else start=meta.size end
    if start>=meta.size or finish<start then
        close();ngx.status=416;ngx.header.content_range='bytes */'..meta.size
        ngx.header.accept_ranges='bytes';return
    end
end
local ext=(meta.key:match('%.([^./]+)$') or ''):lower()
local types={jpg='image/jpeg',jpeg='image/jpeg',png='image/png',webp='image/webp',gif='image/gif',json='application/json'}
ngx.status=partial and 206 or 200
ngx.header.content_type=types[ext] or 'application/octet-stream'
ngx.header.content_length=math.max(0,finish-start+1)
ngx.header.accept_ranges='bytes'
ngx.header['X-Content-SHA256']=meta.sha256
ngx.header['X-Content-Type-Options']='nosniff'
ngx.header.cache_control='private, no-store'
if partial then ngx.header.content_range='bytes '..start..'-'..finish..'/'..meta.size end
if ngx.req.get_method()=='HEAD' then close();return end
local sent,error_message=pcall(function()
    if meta.body then ngx.print(meta.body:sub(start+1,finish+1));return end
    local position=start
    while position<=finish and not aborted do
        local success,block,message=ngx.run_worker_thread('artifact_read','artifact_io','read',
            meta.fd,meta.offset+position,math.min(1024*1024,finish-position+1))
        if not success or not block then error(message or '文件读取失败') end
        if aborted then break end
        ngx.print(block)
        local flushed=ngx.flush(true)
        if not flushed then break end
        position=position+#block
    end
end)
close()
if not sent then error(error_message) end
end
local success,failure=pcall(serve)
gate:post(1)
if not success then ngx.log(ngx.ERR,'对象响应中断: ',failure);return ngx.exit(ngx.ERROR) end
