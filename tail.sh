#!/bin/bash
# 监控日志
cd `dirname $0`
tail -f ssserver.log
