#!/bin/bash
# 下载监控 v9: 进程死亡/停滞>2h 自动重启 | 午夜重置 | 下载完成后自动启动全量回测
# 路径基于脚本位置自动推导: data/ 的父目录 = 仓库根 (可用 CHAN_DATA_DIR 整体挪数据目录)
BASE=$(cd "$(dirname "$0")/.." && pwd)
LOG=$BASE/dl_d30_v8.log
PROG=$BASE/data/download_incr_d30.py
BT=$BASE/bt_all_3y.py
BTLOG=$BASE/bt_all_3y.log
BTFLAG=$BASE/bt_all_started.flag
MONLOG=$BASE/monitor.log
while true; do
  sleep 300
  H=$(date +%H)
  if [ "$H" -lt "8" ]; then
    # 午夜重置: 请求配额重置, offset=0
    if ! pgrep -f "download_incr_d30" >/dev/null; then
      nohup python3 $PROG --req-offset 0 >> $LOG 2>&1 &
      echo "$(date '+%H:%M:%S') [monitor] 午夜重置重启" >> $MONLOG
    fi
    continue
  # 回测由用户手动管理(bt_all_3y.py --sleep 0.5); monitor只管下载守护
  fi
  # 回测由用户手动管理(bt_all_3y.py --sleep 0.5); monitor只管下载守护
  if ! pgrep -f "download_incr_d30" >/dev/null && ! pgrep -f "bt_all_3y" >/dev/null; then
    # 下载进程死亡且回测未跑 → 重启下载(读取进度文件续传; DB级断点)
    cd $BASE && nohup python3 $PROG >> $LOG 2>&1 &
    echo "$(date '+%H:%M:%S') [monitor] 进程死亡重启" >> $MONLOG
  elif pgrep -f "download_incr_d30" >/dev/null && ! pgrep -f "bt_all_3y" >/dev/null; then
    AGE=$(( $(date +%s) - $(stat -c %Y $LOG 2>/dev/null || date +%s) ))
    if [ "$AGE" -gt 7200 ]; then
      pkill -f "download_incr_d30"; sleep 2
      cd $BASE && nohup python3 $PROG >> $LOG 2>&1 &
      echo "$(date '+%H:%M:%S') [monitor] 停滞$((AGE/3600))h重启" >> $MONLOG
    fi
  fi
done
