#!/bin/bash
# 监控Baostock IP解封, 解封后自动启动30min下载
# 每30分钟检查一次
# 路径基于脚本位置自动推导: data/ 的父目录 = 仓库根
BASE=$(cd "$(dirname "$0")/.." && pwd)

LOG="/tmp/dl60_monitor.log"

echo "[$(date)] 开始监控Baostock IP状态..." | tee -a $LOG

while true; do
    result=$(python3 -c "
import baostock as bs
lg = bs.login()
print(lg.error_code)
bs.logout()
" 2>/dev/null)

    if [ "$result" = "0" ]; then
        echo "[$(date)] IP已解封! 启动下载..." | tee -a $LOG
        nohup python3 -u $BASE/data/download_30min.py > /tmp/dl60_v6_stdout.log 2>&1 &
        echo "   PID: $!" | tee -a $LOG
        exit 0
    else
        echo "[$(date)] 仍在黑名单(code=$result), 30分钟后重试" | tee -a $LOG
    fi
    sleep 1800
done
