# 日K + 30min 回测代码包

导出时间: 2026-08-16 | 来源: /root/data/backtest

## 目录结构

```
├── backtest_daily_30min.py   # 主回测脚本(日K+30min, 缠论买卖点回测)
├── ALGORITHM.md              # 策略算法文档(参数表/信号定义, 必读)
├── ITERATION_LOG.md          # 迭代日志
├── test_*.py                 # 审计追踪/首买中枢溯源/性能等价 测试
├── chan/                     # 缠论计算库(回测依赖, 纯标准库)
│   ├── paths.py              # 统一路径解析(DB路径/项目根, 支持环境变量覆盖)
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
├── tools/
│   ├── gen_30min_chart.py    # 30min 图表生成
│   ├── gen_strategy_doc.py   # 策略文档生成
│   └── build_*_audit.py      # 审计报告构建
└── data/                     # 数据下载脚本(依赖 baostock)
    ├── download_incr_d30.py  # 增量下载 日K+30min(断点续传, 推荐)
    ├── download_30min.py / download_both.py
    └── monitor_dl.sh / monitor_and_download.sh
```

## 路径配置(已移除全部硬编码, clone 即用)

所有路径统一收敛到 `chan/paths.py`,自动推导项目根(`chan/` 的父目录),无需手动修改:

| 环境变量 | 作用 | 默认值 |
|---|---|---|
| `CHAN_DB_PATH` | K线库完整路径(最高优先级) | `<项目根>/kline.db` |
| `CHAN_DATA_DIR` | 数据目录(DB/进度/结果都放这) | `<项目根>` |

```bash
# 默认: 直接用仓库目录下的 kline.db
python3 backtest_daily_30min.py 000001.SZ

# 或把数据放在别处(DB + 进度文件都会跟着走)
CHAN_DATA_DIR=/mnt/backtest python3 data/download_incr_d30.py --limit 100

# 或只指定 DB 路径
CHAN_DB_PATH=/mnt/backtest/kline.db python3 backtest_daily_30min.py 000001.SZ
```

注: 原项目中的 `bt/` 批量回测脚本、`charts/`、`results/` 目录未包含在本仓库中,
批量脚本如需要可参照 `backtest_daily_30min.py` 的路径用法(均支持上述环境变量)。

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
