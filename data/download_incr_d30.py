#!/usr/bin/env python3
"""增量下载 日K + 30min(串行, 无并发) — 45000请求硬上限。

约束(用户): 必须增量(跳过已有年份) | ≤45000请求 | 串行(2.5s间隔) | 断点续传 | 过12点可重置
覆盖: daily_kline 全部股票, 2020-2026 分年
每只: 日K缺失年份(2020-2026) + 30min缺失年份(2020-2026), 每年1请求
用法: python3 download_incr_d30.py [--limit N] [--status]
"""
import sqlite3, time, sys, os, json
import socket
socket.setdefaulttimeout(30)  # socket级超时(单次recv)

# 路径自动推导: 仓库根 = data/ 的父目录 (可用 CHAN_DATA_DIR / CHAN_DB_PATH 覆盖)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from chan.paths import DB_PATH, data_file

# 请求级硬墙钟(2026-08-12, 审查发现): baostock send_msg是 while True: recv(8192) 循环,
# 服务端慢速吐数据时30s超时被逐次重置 → 单请求可挂~170s(日志实测09:54→10:11六请求17分钟)
# signal.alarm 60s 到点强抛, 防单请求无界挂起
import signal

class _ReqTimeout(Exception):
    pass

def _alarm_h(sig, frame):
    raise _ReqTimeout('baostock请求超时(60s硬墙钟)')

signal.signal(signal.SIGALRM, _alarm_h)

try:
    import baostock as bs
except ImportError:
    os.system("pip install baostock --break-system-packages -q")
    import baostock as bs

DB = DB_PATH
PROG = data_file('dl_d30_progress.json')
DELAY = 2.5
RELOGIN_EVERY = 40
BATCH_PAUSE = 50
MAX_REQ = 45000
YEARS = [(f"{y}-01-01", f"{y}-12-31") for y in range(2020, 2027)]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def login_with_retry(retries=5, wait=60):
    """登录重试(2026-08-13, 凌晨baostock登录不稳定): 失败后等待递增重试, 全部失败才返回失败"""
    for i in range(retries):
        lg = bs.login()
        if lg.error_code == '0':
            return lg
        log(f"登录失败({lg.error_code}) 第{i+1}次, {wait}s后重试")
        time.sleep(wait)
        wait *= 2
    return lg


def load_progress():
    if os.path.exists(PROG):
        with open(PROG) as f:
            return json.load(f)
    return {}


def save_progress(p):
    with open(PROG, 'w') as f:
        json.dump(p, f, indent=1)


def missing_years_daily(conn, code):
    """日K缺失年份(2020-2026), date列 YYYYMMDD"""
    got = set()
    for r in conn.execute(
            "SELECT DISTINCT substr(date,1,4) FROM daily_kline WHERE code=?", (code,)).fetchall():
        if r[0] and len(r[0]) == 4 and r[0].isdigit():
            got.add(r[0])
    return [y[:4] for y, _ in YEARS if y[:4] not in got]


def missing_years_30min(conn, code):
    got = set()
    for r in conn.execute(
            "SELECT DISTINCT substr(datetime,1,4) FROM kline_30min WHERE code=?", (code,)).fetchall():
        if r[0] and len(r[0]) == 4 and r[0].isdigit():
            got.add(r[0])
    return [y[:4] for y, _ in YEARS if y[:4] not in got]


def main():
    limit = None
    req_offset = 0
    for i, arg in enumerate(sys.argv):
        if arg == '--limit' and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
        if arg == '--req-offset' and i + 1 < len(sys.argv):
            req_offset = int(sys.argv[i + 1])

    conn = sqlite3.connect(DB)

    if '--status' in sys.argv:
        p = load_progress()
        done = sum(1 for v in p.values() if v.get('done'))
        reqs = sum(v.get('req', 0) for v in p.values())
        print(f"进度: {done}只完成, 累计请求{reqs}, 共{len(p)}只已处理")
        return

    all_codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM daily_kline ORDER BY code").fetchall()]
    if limit:
        all_codes = all_codes[:limit]
    log(f"待处理 {len(all_codes)} 只")

    progress = load_progress()
    # 重启续传: 累计请求从进度文件恢复(防多次重启累计超45000)
    if req_offset == 0 and progress:
        req_offset = sum(v.get('req', 0) for v in progress.values())
    lg = login_with_retry()
    if lg.error_code != '0':
        log(f"登录失败(重试后仍失败): {lg.error_code}")
        return
    log("登录成功")

    req_count = req_offset   # 累计请求(上次运行已用11214)
    stock_done = 0
    t0 = time.time()

    for idx, sym in enumerate(all_codes):
        if progress.get(sym, {}).get('done'):
            continue
        if req_count >= MAX_REQ:
            log("请求数达40000上限, 停止")
            break

        if sym.endswith('.SH'): code = f"sh.{sym[:6]}"
        elif sym.endswith('.SZ'): code = f"sz.{sym[:6]}"
        else: continue

        if idx > 0 and idx % RELOGIN_EVERY == 0:
            bs.logout(); time.sleep(2)
            lg = login_with_retry()
            if lg.error_code != '0':
                log(f"重登录失败(重试后仍失败) @{sym}, 保存退出"); break

        my_d = missing_years_daily(conn, sym)
        my_30 = missing_years_30min(conn, sym)
        if not my_d and not my_30:
            progress[sym] = {'done': True, 'req': 0}
            stock_done += 1
            continue

        st = progress.get(sym, {'done': False, 'req': 0, 'yd': [], 'y30': []})
        st_req = st.get('req', 0)
        rows_d, rows_30 = [], []

        for freq, years, rows, ykey in [('d', my_d, rows_d, 'yd'), ('30', my_30, rows_30, 'y30')]:
            for yy in years:
                if yy in st.get(ykey, []):
                    continue
                if req_count + st_req >= MAX_REQ:
                    log("请求数将超40000, 保存进度退出")
                    save_progress(progress); conn.close(); return
                try:
                    signal.alarm(60)   # 请求级硬墙钟(60s到点强抛, 防recv循环无限挂起)
                    if freq == 'd':
                        rs = bs.query_history_k_data_plus(
                            code, "date,code,open,high,low,close,volume,amount",
                            start_date=f"{yy}-01-01", end_date=f"{yy}-12-31",
                            frequency="d", adjustflag="3")
                    else:
                        rs = bs.query_history_k_data_plus(
                            code, "date,time,code,open,high,low,close,volume,amount",
                            start_date=f"{yy}-01-01", end_date=f"{yy}-12-31",
                            frequency="30", adjustflag="3")
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
                            if freq == 'd':
                                rows.append((sym, r[0], float(r[2] or 0), float(r[3] or 0),
                                             float(r[4] or 0), float(r[5] or 0),
                                             float(r[6] or 0), float(r[7] or 0)))
                            else:
                                dt = f"{r[0]} {r[1][8:10]}:{r[1][10:12]}:00"
                                rows.append((sym, dt, float(r[3] or 0), float(r[4] or 0),
                                             float(r[5] or 0), float(r[6] or 0),
                                             float(r[7] or 0), float(r[8] or 0)))
                        except (ValueError, IndexError):
                            continue
                    st[ykey].append(yy)
                    time.sleep(DELAY)
                except _ReqTimeout as ex:
                    log(f"  {sym}/{freq}/{yy} 超时: {ex}, 重登录恢复连接")
                    try:
                        bs.logout()
                    except Exception:
                        pass
                    try:
                        bs.login()
                    except Exception:
                        pass
                    time.sleep(5)
                except Exception as ex:
                    log(f"  {sym}/{freq}/{yy} 错误: {ex}")
                    time.sleep(5)
                finally:
                    signal.alarm(0)   # 清墙钟

        if rows_d:
            conn.executemany("INSERT OR REPLACE INTO daily_kline VALUES (?,?,?,?,?,?,?,?)", rows_d)
        if rows_30:
            conn.executemany("INSERT OR REPLACE INTO kline_30min VALUES (?,?,?,?,?,?,?,?)", rows_30)
        conn.commit()

        done = len(st['yd']) >= len(my_d) and len(st['y30']) >= len(my_30)
        st['done'] = done
        st['req'] = st_req
        progress[sym] = st
        stock_done += 1

        el = time.time() - t0
        eta = el / (idx + 1) * (len(all_codes) - idx - 1)
        log(f"  [{idx+1}/{len(all_codes)}] {sym} 日K+{len(rows_d)} 30m+{len(rows_30)} "
            f"累计请求{req_count} ETA:{eta/60:.0f}min")

        if (idx + 1) % BATCH_PAUSE == 0:
            log(f"  已处理{idx+1}只, 暂停30秒..."); time.sleep(30)

    save_progress(progress)
    conn.close(); bs.logout()
    log(f"完成: {stock_done}只, 累计请求{req_count}次, {time.time()-t0/60:.0f}min")


if __name__ == '__main__':
    main()
