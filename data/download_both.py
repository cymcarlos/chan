#!/usr/bin/env python3
"""增量下载 60min + 30min K线(串行, 无并发) — 前1000只缺30min的股票。

约束: 串行(2.5s间隔) | 请求预算 40000 | 断点续传 | 增量(跳过已有年份)
每只股票: 30min缺失年份 + 60min缺失年份, 每年1次请求(2020-2026)

用法: python3 download_both.py [--limit 1000] [--status]
"""
import sqlite3, time, sys, os, json

# 路径自动推导: 仓库根 = data/ 的父目录 (可用 CHAN_DATA_DIR / CHAN_DB_PATH 覆盖)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from chan.paths import DB_PATH, data_file

try:
    import baostock as bs
except ImportError:
    os.system("pip install baostock --break-system-packages -q")
    import baostock as bs

DB = DB_PATH
PROG = data_file('dl_both_progress.json')
DELAY = 2.5
RELOGIN_EVERY = 40
BATCH_PAUSE = 50
MAX_REQ = 40000
YEARS = [(f"{y}-01-01", f"{y}-12-31") for y in range(2020, 2027)]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_progress():
    if os.path.exists(PROG):
        with open(PROG) as f:
            return json.load(f)
    return {}


def save_progress(p):
    with open(PROG, 'w') as f:
        json.dump(p, f, indent=1)


def missing_years(conn, table, code):
    """该表缺失的年份列表(2020-2026)。"""
    got = set()
    rows = conn.execute(
        f"SELECT DISTINCT substr(datetime,1,4) FROM {table} WHERE code=?", (code,)).fetchall()
    for r in rows:
        if r[0] and len(r[0]) == 4 and r[0].isdigit():
            got.add(r[0])
    return [y[:4] for y, _ in YEARS if y[:4] not in got]  # 返回年份(4位)


def main():
    limit = 1000
    for i, arg in enumerate(sys.argv):
        if arg == '--limit' and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    conn = sqlite3.connect(DB)

    if '--status' in sys.argv:
        p = load_progress()
        done = sum(1 for v in p.values() if v.get('done'))
        print(f"进度: {done}/{len(p)} 完成, 总请求 {sum(v.get('req',0) for v in p.values())}")
        return

    # 前 limit 只缺30min的股票(daily_kline 序)
    all_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM daily_kline ORDER BY code").fetchall()]
    todo = []
    for c in all_codes:
        if conn.execute("SELECT COUNT(*) FROM kline_30min WHERE code=?", (c,)).fetchone()[0] == 0:
            todo.append(c)
        if len(todo) >= limit:
            break

    progress = load_progress()
    log(f"待处理 {len(todo)} 只(缺30min), 已有进度 {len(progress)}")

    lg = bs.login()
    if lg.error_code != '0':
        log(f"登录失败: {lg.error_code}")
        return
    log("登录成功")

    req_count = 0
    stock_done = 0
    t0 = time.time()

    for idx, sym in enumerate(todo):
        if progress.get(sym, {}).get('done'):
            continue

        if sym.endswith('.SH'): code = f"sh.{sym[:6]}"
        elif sym.endswith('.SZ'): code = f"sz.{sym[:6]}"
        else: continue

        if idx > 0 and idx % RELOGIN_EVERY == 0:
            bs.logout(); time.sleep(2)
            lg = bs.login()
            if lg.error_code != '0':
                log(f"重登录失败 @{sym}, 保存退出"); break

        my30 = missing_years(conn, 'kline_30min', sym)
        my60 = missing_years(conn, 'kline_60min', sym)
        if not my30 and not my60:
            progress[sym] = {'done': True, 'req': 0}
            stock_done += 1
            continue

        st = progress.get(sym, {'done': False, 'req': 0, 'years60': [], 'years30': []})
        st_req = st.get('req', 0)
        rows30, rows60 = [], []

        for freq, years, rows, ykey in [('30', my30, rows30, 'years30'),
                                        ('60', my60, rows60, 'years60')]:
            for yy in years:
                if yy in st.get(ykey, []):
                    continue
                if req_count + st_req >= MAX_REQ:
                    log("请求数达上限, 保存退出"); save_progress(progress); conn.close(); return
                try:
                    rs = bs.query_history_k_data_plus(
                        code, "date,time,code,open,high,low,close,volume,amount",
                        start_date=f"{yy}-01-01", end_date=f"{yy}-12-31",
                        frequency=freq, adjustflag="3")
                    req_count += 1; st_req += 1
                    if rs.error_code != '0':
                        err = rs.error_msg[:60]
                        if '10001011' in err or 'blacklist' in err.lower():
                            log(f"!! IP被封 @{sym}, 保存进度退出")
                            save_progress(progress); conn.close(); return
                        continue
                    while rs.next():
                        r = rs.get_row_data()
                        try:
                            dt = f"{r[0]} {r[1][8:10]}:{r[1][10:12]}:00"
                            rows.append((sym, dt, float(r[3] or 0), float(r[4] or 0),
                                         float(r[5] or 0), float(r[6] or 0),
                                         float(r[7] or 0), float(r[8] or 0)))
                        except (ValueError, IndexError):
                            continue
                    st[ykey].append(yy)
                    time.sleep(DELAY)
                except Exception as ex:
                    log(f"  {sym}/{freq}/{yy} 错误: {ex}")
                    time.sleep(5)

        if rows30:
            conn.executemany("INSERT OR REPLACE INTO kline_30min VALUES (?,?,?,?,?,?,?,?)", rows30)
        if rows60:
            conn.executemany("INSERT OR REPLACE INTO kline_60min VALUES (?,?,?,?,?,?,?,?)", rows60)
        conn.commit()

        done = len(st['years30']) >= len(my30) and len(st['years60']) >= len(my60)
        st['done'] = done
        st['req'] = st_req
        progress[sym] = st
        stock_done += 1

        # 每只打印进度(便于监控, 之前50只才打印一次误导为卡住)
        el = time.time() - t0
        eta = el / (idx + 1) * (len(todo) - idx - 1)
        log(f"  [{idx+1}/{len(todo)}] {sym} +30m{len(rows30)} +60m{len(rows60)} "
            f"请求{req_count}次 {el/60:.0f}min ETA:{eta/60:.0f}min")

        if (idx + 1) % BATCH_PAUSE == 0 and idx > 0:
            log(f"  已处理{idx+1}只, 暂停30秒..."); time.sleep(30)

    save_progress(progress)
    conn.close(); bs.logout()
    log(f"完成: {stock_done}只, 请求{req_count}次, {time.time()-t0/60:.0f}min")


if __name__ == '__main__':
    main()
