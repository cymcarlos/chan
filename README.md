# 日K + 30min 回测代码包

导出时间: 2026-08-16 | 来源: /root/data/backtest

## 目录结构

```
├── backtest_daily_30min.py   # 主回测脚本(日K+30min, 缠论买卖点回测)
├── ALGORITHM.md              # 策略算法文档(参数表/信号定义, 必读)
├── ITERATION_LOG.md          # 迭代日志
├── chan/                     # 缠论计算库(回测依赖, 纯标准库)
│   ├── bars.py               # 数据结构 + MACD
│   ├── bi.py                 # 笔构建
│   ├── fractal.py            # 分型
│   ├── zhongshu.py           # 中枢构建
│   ├── state.py              # 状态管理
│   ├── scanner_60min.py      # 60min 扫描
│   ├── confirm_60min.py      # 60min 确认
│   ├── backtest_60min.py     # 60min 回测辅助
│   ├── daily_scan.py         # 日线扫描
│   └── weekly_filter.py      # 周线过滤
├── bt/                       # 批量运行脚本 + 标的池 + 分析报告
│   ├── bt50_r7.py            # 50 标的批量回测(r7)
│   ├── bt100_r7.py / bt200_r7.py / bt50_20260813.py / bt_all_3y.py
│   ├── bt_200_symbols.json   # 标的池
│   └── *.md                  # 胜率分析/代码审查/修复计划
├── tools/
│   ├── gen_30min_chart.py    # 30min 图表生成
│   └── gen_strategy_doc.py   # 策略文档生成
└── data/                     # 数据下载脚本(依赖 baostock)
    ├── download_incr_d30.py  # 增量下载 日K+30min(断点续传, 推荐)
    ├── download_30min.py / download_both.py
    └── monitor_dl.sh / monitor_and_download.sh
```

## 运行前必改的路径(原机硬编码 /root/data)

| 文件 | 常量 | 原值 | 说明 |
|---|---|---|---|
| backtest_daily_30min.py | DB_PATH (L30) | /root/data/backtest/kline.db | K线库路径 |
| backtest_daily_30min.py | sys.path (L19) | /root/data/backtest | chan 包父目录 |
| chan/backtest_60min.py | DB_PATH (L17) | /root/data/backtest/kline.db | 同上 |
| chan/daily_scan.py | DB_PATH (L24) | /root/data/backtest/kline.db | 同上 |
| chan/scanner_60min.py | DB_PATH (L23) | /root/data/backtest/kline.db | 同上 |
| chan/weekly_filter.py | DB_PATH (L25) | /root/data/backtest/kline.db | 同上 |
| bt/bt*_r7.py | DB/POOL/OUT | /root/data/... | DB、标的池、输出路径 |

## 数据要求

SQLite 库需包含以下两张表(下载脚本会自动建):

```sql
CREATE TABLE daily_kline (
    code TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    vol REAL, amount REAL,
    PRIMARY KEY (code, date)
);
CREATE TABLE kline_30min (
    code TEXT NOT NULL, datetime TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM:00'
    open REAL, high REAL, low REAL, close REAL,
    vol REAL, amount REAL,
    PRIMARY KEY (code, datetime)
);
```

## 快速开始

```bash
# 1. 装数据下载依赖(仅下载脚本需要; 回测本身纯标准库)
pip install baostock

# 2. 下载数据(增量, 断点续传)
python3 data/download_incr_d30.py --limit 100

# 3. 跑单标的回测
python3 backtest_daily_30min.py 000001.SZ

# 4. 跑批量回测
cd bt && python3 bt50_r7.py
```

## 依赖

- 回测/缠论计算: 仅 Python3 标准库 + sqlite3, 无第三方依赖
- 数据下载: baostock
