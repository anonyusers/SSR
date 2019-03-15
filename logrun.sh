#!/bin/bash
cd `dirname $0`
python_ver=$(ls /usr/bin|grep -e "^python[23]\.[1-9]\+$"|tail -1)
eval $(ps -ef | grep "[0-9] ${python_ver} server\\.py m" | awk '{print "kill "$2}')
ulimit -n 512000
# nohup: 不挂断的执行命令,与&不同,忽略所有的挂起 (SIGHUP) 信号
# 或者修改用 -p 选项指定的进程来忽略所有的挂起 (SIGHUP) 信号
# 带有日志输出
nohup ${python_ver} server.py m>> ssserver.log 2>&1 &

