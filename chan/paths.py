#!/usr/bin/env python3
"""统一路径解析 — 原硬编码 /root/data/backtest 全部收敛到本模块。

优先级(由高到低):
  1. 环境变量 CHAN_DB_PATH    — K线库完整路径(如 /data/kline.db)
  2. 环境变量 CHAN_DATA_DIR   — 数据目录(默认用 <数据目录>/kline.db)
  3. 项目根自动推导           — chan/ 的父目录(仓库 clone 到任何机器都能直接用)

示例:
  CHAN_DB_PATH=/mnt/backtest/kline.db python3 backtest_daily_30min.py 000001.SZ
"""
import os
import sys

# 项目根 = chan/ 目录的父目录(自动推导, 不依赖仓库所在位置)
CHAN_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CHAN_DIR)

# 数据目录: 进度文件/结果等也放这里(可用 CHAN_DATA_DIR 整体挪走)
DATA_DIR = os.environ.get('CHAN_DATA_DIR') or PROJECT_ROOT

# K线库路径(最高优先级: CHAN_DB_PATH 显式指定)
DB_PATH = os.environ.get('CHAN_DB_PATH') or os.path.join(DATA_DIR, 'kline.db')


def data_file(name: str) -> str:
    """数据目录内的文件路径(进度/结果等)。"""
    return os.path.join(DATA_DIR, name)


def add_chan_to_syspath():
    """把项目根插入 sys.path(幂等), 保证 `from chan import ...` 可导入。"""
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
