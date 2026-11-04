-- 此模块只在 Nginx 文件线程池执行；不写 SQLite，不调用拆条 Python。
local ffi = require 'ffi'
local json = require 'cjson.safe'
ffi.cdef [[
typedef struct sqlite3 sqlite3;
typedef struct sqlite3_stmt sqlite3_stmt;
int sqlite3_open_v2(const char*, sqlite3**, int, const char*);
int sqlite3_close(sqlite3*);
int sqlite3_busy_timeout(sqlite3*, int);
int sqlite3_prepare_v2(sqlite3*, const char*, int, sqlite3_stmt**, const char**);
int sqlite3_bind_text(sqlite3_stmt*, int, const char*, int, void*);
int sqlite3_step(sqlite3_stmt*);
int sqlite3_finalize(sqlite3_stmt*);
int sqlite3_column_count(sqlite3_stmt*);
const char *sqlite3_column_name(sqlite3_stmt*, int);
const unsigned char *sqlite3_column_text(sqlite3_stmt*, int);
const void *sqlite3_column_blob(sqlite3_stmt*, int);
int sqlite3_column_bytes(sqlite3_stmt*, int);
int sqlite3_column_type(sqlite3_stmt*, int);
char *realpath(const char*, char*);
int open(const char*, int, ...);
int close(int);
long pread(int, void*, unsigned long, long);
long lseek(int, long, int);
]]
local sql = ffi.load('libsqlite3.so.0')
local M = {}
local function fail(status,message) error({status=status,message=message},0) end
local function real(path)
    local buffer=ffi.new('char[4096]')
    if ffi.C.realpath(path,buffer)==nil then return nil end
    return ffi.string(buffer)
end
local function within(path,root) return path==root or path:sub(1,#root+1)==root..'/' end
local function normalize(path)
    if path:sub(1,1)~='/' or path:find('\0',1,true) or path:find('\\',1,true) then fail(404,'文件不存在') end
    local parts={}
    for part in path:gmatch('[^/]+') do
        if part=='..' then fail(404,'文件不存在') end
        if part~='.' then parts[#parts+1]=part end
    end
    return '/'..table.concat(parts,'/')
end
local function query(path,statement,values)
    local holder=ffi.new('sqlite3*[1]')
    if sql.sqlite3_open_v2(path,holder,1+32768,nil)~=0 then
        if holder[0]~=nil then sql.sqlite3_close(holder[0]) end
        fail(503,'读取索引暂不可用')
    end
    local db=holder[0]
    sql.sqlite3_busy_timeout(db,100)
    local stmt=ffi.new('sqlite3_stmt*[1]')
    local ok,result=pcall(function()
        if sql.sqlite3_prepare_v2(db,statement,-1,stmt,nil)~=0 then fail(503,'索引格式不支持') end
        for i,value in ipairs(values or {}) do
            if sql.sqlite3_bind_text(stmt[0],i,tostring(value),-1,ffi.cast('void*',-1))~=0 then fail(503,'索引查询失败') end
        end
        local rows={}
        while true do
            local code=sql.sqlite3_step(stmt[0])
            if code==101 then break end
            if code~=100 then fail(503,'读取索引暂不可用') end
            local row={}
            for i=0,sql.sqlite3_column_count(stmt[0])-1 do
                if sql.sqlite3_column_type(stmt[0],i)~=5 then
                    local key=ffi.string(sql.sqlite3_column_name(stmt[0],i))
                    local ptr=sql.sqlite3_column_blob(stmt[0],i)
                    row[key]=ffi.string(ptr,sql.sqlite3_column_bytes(stmt[0],i))
                end
            end
            rows[#rows+1]=row
        end
        return rows
    end)
    if stmt[0]~=nil then sql.sqlite3_finalize(stmt[0]) end
    sql.sqlite3_close(db)
    if not ok then error(result,0) end
    return result
end
local function exists(path) local file=io.open(path,'rb');if file then file:close();return true end;return false end
local function owner(path,root)
    local parent=path:match('^(.*)/[^/]+$')
    while parent and within(parent,root) do
        if exists(parent..'/.artifacts/index.sqlite3') then return parent end
        parent=parent:match('^(.*)/[^/]+$')
    end
    fail(404,'对象索引不存在')
end
local function entry(root,key,by_id)
    if not exists(root..'/.artifacts/index.sqlite3') then fail(404,'对象索引不存在') end
    local format=query(root..'/.artifacts/index.sqlite3','SELECT version FROM format')[1]
    if not format or format.version~='2' then fail(503,'索引格式不支持') end
    local field=by_id and 'object_id' or 'key'
    local row=query(root..'/.artifacts/index.sqlite3',
        'SELECT key,object_id,kind,pack,offset,size,sha256,revision,category FROM entries WHERE '..field..'=?',{key})[1]
    if not row then fail(404,'对象不存在') end
    row.root=root;row.size=tonumber(row.size);row.offset=tonumber(row.offset)
    if not row.size or not row.offset or row.size<0 or row.offset<0 then fail(503,'索引范围无效') end
    return row
end
local function payload(item)
    if not item.pack then
        local row=query(item.root..'/.artifacts/index.sqlite3',
            'SELECT body,revision FROM entries WHERE object_id=?',{item.object_id})[1]
        if not row or row.revision~=item.revision or not row.body or #row.body~=item.size then fail(409,'对象已更新，请重试') end
        return {body=row.body,size=item.size,sha256=item.sha256,key=item.key}
    end
    if not item.pack:match('^[A-Za-z0-9_-]+%.pack$') then fail(503,'数据包路径无效') end
    local path=item.root..'/.artifacts/'..item.pack
    local resolved=real(path)
    if not resolved or not within(resolved,item.root..'/.artifacts') then fail(404,'数据包不存在') end
    local fd=ffi.C.open(resolved,0+131072) -- O_RDONLY | O_NOFOLLOW
    if fd<0 then fail(404,'数据包不存在') end
    local size=tonumber(ffi.C.lseek(fd,0,2))
    if size<item.offset+item.size then ffi.C.close(fd);fail(503,'数据包提前结束') end
    return {fd=fd,offset=item.offset,size=item.size,sha256=item.sha256,key=item.key}
end
local function read_all(item)
    local meta=payload(item)
    if meta.body then return meta.body end
    local chunks={};local offset=0
    local ok,result=pcall(function()
        while offset<meta.size do
            local count=math.min(1024*1024,meta.size-offset)
            local block=ffi.new('char[?]',count)
            local n=tonumber(ffi.C.pread(meta.fd,block,count,meta.offset+offset))
            if n<=0 then fail(503,'对象提前结束') end
            chunks[#chunks+1]=ffi.string(block,n);offset=offset+n
        end
        return table.concat(chunks)
    end)
    ffi.C.close(meta.fd)
    if not ok then error(result,0) end
    return result
end
local evidence_cache={}
local function image_index(data)
    -- 只建立已有图片的时间/路径索引，不读取图片内容；不保留 ASR 或 OCR 正文。
    -- 全天素材可含几十万条 OCR：预览每一秒不应重新遍历整份证据。
    local indexed={preview={},ocr={},review={}}
    local seen={}
    local function add(at,path)
        if type(at)~='number' or at~=at or math.abs(at)==math.huge or type(path)~='string' then return end
        local second=math.floor(at)
        local previous=indexed.preview[second]
        if at>=0 and (not previous or at<previous[1]) then indexed.preview[second]={at,path} end
        local identity=tostring(at)..':'..path
        if not seen[identity] then indexed.review[#indexed.review+1]={at,path};seen[identity]=true end
    end
    -- 与 Python 预览相同：同一时间优先采样表，再考虑 OCR 引用。
    local times,paths=data.ocr_sample_times or {},data.frame_paths or {}
    if #times==#paths then for i,at in ipairs(times) do add(at,paths[i]) end end
    for i,row in ipairs(data.ocr or {}) do
        if type(row.frame_path)=='string' then indexed.ocr[i]=row.frame_path end
        add(row.timestamp,row.frame_path)
    end
    table.sort(indexed.review,function(a,b) return a[1]<b[1] or a[1]==b[1] and a[2]<b[2] end)
    return indexed
end
local function evidence(job,root)
    local options=json.decode(job.options_json) or {}
    local directory=normalize(options.analysis_dir or job.work_dir..'/analysis')
    if not within(directory,job.work_dir) then fail(404,'证据目录无效') end
    local path=directory..'/evidence.json'
    local store=owner(path,root)
    local item=entry(store,path:sub(#store+2))
    local identity=path..':'..item.sha256..':'..item.revision
    if not evidence_cache[identity] then
        local data=json.decode(read_all(item))
        if not data then fail(503,'证据内容无效') end
        -- 每个线程最多保留两个任务证据；更新或重拆后摘要不同，不能复用旧内容。
        local count=0;for _ in pairs(evidence_cache) do count=count+1 end
        if count>=2 then evidence_cache={} end
        evidence_cache[identity]=image_index(data)
    end
    return evidence_cache[identity],item.sha256
end
local allowed={frame_image=true,frame_ocr=true,asr_chunk=true,recognition_state=true,evidence_index=true,source_metadata=true}
local function locate(root,key,action,args)
    root=real(root);if not root then fail(503,'节点目录不可用') end
    local job=query(root..'/jobs.sqlite3','SELECT work_dir,options_json FROM jobs WHERE id=? AND deleted=0',{key})[1]
    if not job then fail(404,'任务不存在') end
    local work=real(job.work_dir)
    if not work or work~=root..'/jobs/'..key then fail(404,'任务目录不存在') end
    job.work_dir=work
    local id=action:match('^objects/([a-f0-9]+)$')
    if id then
        local item=entry(work,id,true)
        if not allowed[item.category] then fail(404,'对象不提供原始读取') end
        return payload(item)
    end
    local data,sha=evidence(job,root)
    local original
    if action=='preview-frame' then
        local second=tonumber(args.second)
        if not second or second<0 or second~=math.floor(second) then fail(422,'预览秒数无效') end
        local row=data.preview[second];original=row and row[2]
    elseif action=='evidence-frame' then
        local index=tonumber(args.index)
        if not index or index<0 or index~=math.floor(index) then fail(422,'图片索引无效') end
        original=data.ocr[index+1]
    elseif action:match('^review%-images/%d+$') then
        if args.fingerprint~=sha then fail(422,'原始证据已改变') end
        local row=data.review[tonumber(action:match('/(%d+)$'))+1];original=row and row[2]
    else fail(404,'读取接口不存在') end
    if type(original)~='string' then fail(404,'图片不存在') end
    original=normalize(original)
    if not within(original,work) and not within(original,root..'/evidence-cache') then fail(404,'图片不属于此任务') end
    local store=owner(original,root)
    local resolved=real(store)
    if not resolved or not within(resolved,root) then fail(404,'图片目录无效') end
    local item=entry(resolved,original:sub(#store+2))
    if item.category~='frame_image' then fail(404,'对象不是采样图片') end
    return payload(item)
end
function M.locate(root,key,action,args)
    local ok,result=pcall(locate,root,key,action,args)
    if ok then return result end
    if type(result)=='table' then return result end
    return {status=503,message='文件读取失败'}
end
function M.read(fd,position,length)
    local buffer=ffi.new('char[?]',length)
    local n=tonumber(ffi.C.pread(fd,buffer,length,position))
    if n<=0 then return nil,'对象提前结束' end
    return ffi.string(buffer,n)
end
return M
