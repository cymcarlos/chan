#!/usr/bin/env python3
"""下载 30min K线(串行, 无并发) — 只补缺失的测试股票。

用法: python3 daily_backtest_dl30.py
"""
import sqlite3, time, sys, os

try:
    import baostock as bs
except ImportError:
    os.system("pip install baostock --break-system-packages -q")
    import baostock as bs

DB = '/root/data/backtest/kline.db'
DELAY = 2.5

# 22 只测试股中缺 30min 的 17 只
MISSING = ['001282.SZ','000895.SZ','000566.SZ','000573.SZ','000819.SZ',
           '000902.SZ','000906.SZ','000016.SZ','000028.SZ','000031.SZ',
           '000055.SZ','000488.SZ','000789.SZ','000948.SZ','000995.SZ',
           '001234.SZ','001322.SZ']

conn = sqlite3.connect(DB)

lg = bs.login()
if lg.error_code != '0':
    print(f'登录失败: {lg.error_code} {lg.error_msg}')
    sys.exit(1)
print('登录成功')

total = 0
for i, sym in enumerate(MISSING):
    if sym.endswith('.SH'): code = f"sh.{sym[:6]}"
    elif sym.endswith('.SZ'): code = f"sz.{sym[:6]}"
    else: continue

    rows = []
    try:
        rs = bs.query_history_k_data_plus(
            code, "date,time,code,open,high,low,close,volume,amount",
            start_date="2020-01-01", end_date="2026-12-31",
            frequency="30", adjustflag="3")
        while rs.next():
            r = rs.get_row_data()
            try:
                dt = f"{r[0]} {r[1][8:10]}:{r[1][10:12]}:00"
                rows.append((sym, dt, float(r[3] or 0), float(r[4] or 0),
                             float(r[5] or 0), float(r[6] or 0),
                             float(r[7] or 0), float(r[8] or 0)))
            except (ValueError, IndexError):
                continue
    except Exception as e:
        print(f'  {sym} 错误: {e}')
        time.sleep(5)
        continue

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO kline_30min VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        total += len(rows)
        print(f'  [{i+1}/{len(MISSING)}] {sym}: {len(rows)} 条')
    else:
        print(f'  [{i+1}/{len(MISSING)}] {sym}: 无数据')

    time.sleep(DELAY)  # 串行限速

conn.close()
bs.logout()
print(f'\n完成: {total} 条 30min 数据')
