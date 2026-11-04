-- 仅为文件线程池准入，不持有 SQLite 连接或业务计算名额。
return require('ngx.semaphore').new(4)
