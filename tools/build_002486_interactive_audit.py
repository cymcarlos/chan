#!/usr/bin/env python3
"""Build a standalone interactive audit for 002486.SZ without mutating inputs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import backtest_daily_30min as engine  # noqa: E402
from chan.bars import macd, macd_area  # noqa: E402


SYMBOL = "002486.SZ"
SIGNAL_DATE = "2024-06-07"
STRUCTURE_SNAPSHOT_AT: str | None = None
STRUCTURE_ASOF_DATE: str | None = None
STRUCTURE_SNAPSHOT_BASIS: str | None = None
ENTRY_DATE = "2024-06-19"
OUTPUT = PROJECT / "bt/audits/002486_interactive_full_20260816"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S" if value.time() else "%Y-%m-%d")
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def query_bars(conn: sqlite3.Connection, table: str, time_col: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {time_col},open,high,low,close,vol,amount FROM {table} WHERE code=? ORDER BY {time_col}",
        (SYMBOL,),
    ).fetchall()
    closes = [float(row[4]) for row in rows]
    diff, dea, hist = macd(closes)
    return [
        {
            "at": iso(row[0]),
            "o": round(float(row[1]), 6),
            "h": round(float(row[2]), 6),
            "l": round(float(row[3]), 6),
            "c": round(float(row[4]), 6),
            "v": round(float(row[5] or 0), 2),
            "dif": round(float(diff[i]), 8),
            "dea": round(float(dea[i]), 8),
            "macd": round(float(hist[i]), 8),
        }
        for i, row in enumerate(rows)
    ]


def pack_pen(pen: Any, index: int) -> dict[str, Any]:
    return {
        "id": f"bi-{index:03d}",
        "direction": pen.direction,
        "start": iso(pen.start_dt),
        "end": iso(pen.end_dt),
        "confirm": iso(getattr(pen, "confirm_at", "") or pen.end_dt),
        "start_price": round(float(pen.start_price), 6),
        "end_price": round(float(pen.end_price), 6),
        "high": round(float(pen.high), 6),
        "low": round(float(pen.low), 6),
    }


def centers_with_members(bis: list[Any]) -> list[dict[str, Any]]:
    centers: list[dict[str, Any]] = []
    i = 0
    while i < len(bis) - 2:
        seed = bis[i:i + 3]
        highs = [float(x.high) for x in seed]
        lows = [float(x.low) for x in seed]
        zg, zd = min(highs), max(lows)
        if zg <= zd:
            i += 1
            continue
        j = i + 3
        while j < len(bis):
            bj = bis[j]
            nzg = min(highs + [float(bj.high)])
            nzd = max(lows + [float(bj.low)])
            if nzg <= nzd:
                break
            highs.append(float(bj.high))
            lows.append(float(bj.low))
            j += 1
        entry = bis[i - 1] if i > 0 else bis[i]
        center = {
            "id": f"zs-{len(centers):02d}",
            "direction": "down" if entry.direction == "down" else "up",
            "start": iso(bis[i].start_dt),
            "end": iso(bis[j - 1].end_dt),
            "occur": iso(bis[i + 2].end_dt),
            "confirm": iso(getattr(bis[i + 2], "confirm_at", "") or bis[i + 2].end_dt),
            "initial_ZD": round(max(float(x.low) for x in seed), 6),
            "initial_ZG": round(min(float(x.high) for x in seed), 6),
            "ZD": round(max(lows), 6),
            "ZG": round(min(highs), 6),
            "DD": round(min(lows), 6),
            "GG": round(max(highs), 6),
            "member_ids": [f"bi-{k:03d}" for k in range(i, j)],
            "member_count": j - i,
            "member_start_idx": i,
            "member_end_idx": j - 1,
            "is_complete": j < len(bis),
            "complete_at": (
                iso(getattr(bis[j], "confirm_at", "") or bis[j].end_dt)
                if j < len(bis) else None),
            "eligible": bool(min(highs) > max(lows) * 1.003),
        }
        centers.append(center)
        i = j
    return centers


def diagnose_pairs(
    centers: list[dict[str, Any]], bars: list[Any], diff: list[float], dea: list[float]
) -> list[dict[str, Any]]:
    center_pos = {row["id"]: i for i, row in enumerate(centers)}
    d_idx = {row.dt.strftime("%Y-%m-%d"): i for i, row in enumerate(bars)}
    diagnostics = []
    for order, (a, b) in enumerate(zip(centers, centers[1:])):
        if (a["direction"] != "down" or b["direction"] != "down"
                or not a.get("eligible") or not b.get("eligible")):
            continue
        a_s0 = d_idx[a["start"][:10]]
        hi = a_s0
        for k in range(a_s0 - 1, max(0, a_s0 - 60), -1):
            if bars[k].high > bars[hi].high:
                hi = k
        b_s, b_e = d_idx[b["start"][:10]], d_idx[b["end"][:10]]
        b_center_i = center_pos[b["id"]]
        if b_center_i + 1 < len(centers):
            next_start = d_idx[centers[b_center_i + 1]["start"][:10]]
            c_sub = bars[b_e + 1:max(b_e + 1, next_start)]
        else:
            c_sub = bars[b_e + 1:]
        if not c_sub:
            continue
        min_bar = min(c_sub, key=lambda x: x.low)
        c_i = b_e + 1 + c_sub.index(min_bar)
        area_a = float(macd_area(diff, dea, hi, a_s0, "down"))
        area_c = float(macd_area(diff, dea, b_e + 1, c_i, "down"))
        len_a, len_c = max(a_s0 - hi, 1), max(c_i - b_e, 1)
        avg_a, avg_c = abs(area_a) / len_a, abs(area_c) / len_c
        downshift = b["ZG"] < a["ZD"]
        valid_areas = area_a < 0 and area_c < 0
        ratio = avg_c / avg_a if avg_a else None
        divergence = valid_areas and ratio is not None and ratio < 0.9
        new_low = float(min_bar.low) < b["DD"] * 0.999
        signal = min_bar.dt.strftime("%Y-%m-%d") if new_low else min(
            bars[b_s:b_e + 1], key=lambda x: x.low
        ).dt.strftime("%Y-%m-%d")
        complete = bool(a.get("is_complete") and b.get("is_complete"))
        generated = complete and downshift and new_low and divergence
        diagnostics.append({
            "id": f"pair-{order:02d}",
            "center1": a["id"],
            "center2": b["id"],
            "center1_range": [a["ZD"], a["ZG"]],
            "center2_range": [b["ZD"], b["ZG"]],
            "center1_dates": [a["start"], a["end"]],
            "center2_dates": [b["start"], b["end"]],
            "downshift": downshift,
            "strict_isolation": b["GG"] < a["DD"],
            "adjacent_in_raw_center_sequence": True,
            "both_complete": complete,
            "A_start": bars[hi].dt.strftime("%Y-%m-%d"),
            "A_end": bars[a_s0].dt.strftime("%Y-%m-%d"),
            "A_area": round(area_a, 10),
            "A_length": len_a,
            "C_start": bars[b_e + 1].dt.strftime("%Y-%m-%d"),
            "C_end": min_bar.dt.strftime("%Y-%m-%d"),
            "C_low": round(float(min_bar.low), 6),
            "C_area": round(area_c, 10),
            "C_length": len_c,
            "force_ratio": round(ratio, 6) if ratio is not None else None,
            "new_low": new_low,
            "signal_date": signal,
            "generated": generated,
            "reason": (
                "generated"
                if generated
                else "center not complete" if not complete
                else "ZG not downshifted" if not downshift
                else "C did not make a new low" if not new_low
                else "MACD segment invalid" if not valid_areas
                else f"no divergence: C/A={ratio:.3f} >= 0.9"
            ),
        })
    same_signal = [row for row in diagnostics if row["generated"] and row["signal_date"] == SIGNAL_DATE]
    if same_signal:
        same_signal[-1]["selected_by_engine"] = True
        for row in same_signal[:-1]:
            row["rejected_duplicate_key"] = True
    if diagnostics:
        diagnostics[-1]["latest_local_pair"] = True
    return diagnostics


def is_structure_snapshot_scan(bars: list[Any]) -> bool:
    return bool(
        STRUCTURE_ASOF_DATE
        and bars
        and bars[-1].dt.strftime("%Y-%m-%d") == STRUCTURE_ASOF_DATE
    )


def collect() -> dict[str, Any]:
    if not STRUCTURE_SNAPSHOT_AT or not STRUCTURE_ASOF_DATE:
        raise RuntimeError(
            "structure snapshot is unset; configure first_seen_at/decision_at and its visible daily cutoff"
        )
    capture: dict[str, Any] = {}
    original_scan = engine.scan_daily_on_bis

    def wrapped_scan(*args: Any, **kwargs: Any):
        output = original_scan(*args, **kwargs)
        bis, bars, diff, dea = args[:4]
        if is_structure_snapshot_scan(bars) and not capture:
            pens = [pack_pen(pen, i) for i, pen in enumerate(bis)]
            centers = centers_with_members(list(bis))
            diagnostics = diagnose_pairs(centers, list(bars), list(diff), list(dea))
            capture.update({
                "asof": STRUCTURE_ASOF_DATE,
                "pens": pens,
                "centers": centers,
                "pair_diagnostics": diagnostics,
            })
        return output

    engine.scan_daily_on_bis = wrapped_scan
    engine.clear_all()
    engine.clear_daily_cache()
    engine.clear_30min_cache()
    engine._FRESH_B1_CACHE.clear()
    database = PROJECT / "kline.db"
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    try:
        replay = engine.backtest_one(SYMBOL, "20240101", "20260805", conn=conn, audit_trace=True)
        daily = query_bars(conn, "daily_kline", "date")
        m30 = query_bars(conn, "kline_30min", "datetime")
    finally:
        conn.close()
        engine.scan_daily_on_bis = original_scan

    if not capture:
        raise RuntimeError(
            f"failed to capture daily structure visible at {STRUCTURE_SNAPSHOT_AT} "
            f"(daily cutoff {STRUCTURE_ASOF_DATE})"
        )
    trade = next(row for row in replay["trades"] if row.get("entry") == ENTRY_DATE)
    audit = trade["audit"]
    candidate_map = {row["candidate_id"]: row for row in replay["audit_trace"]["candidates"]}
    adopted = next(
        (
            row for row in audit.get("entry_predicates", [])
            if str(row.get("actual_reason", "")).startswith("adopted by priority")
        ),
        None,
    )
    adopted_id = (adopted or {}).get("candidate_id") or audit.get("candidate_id")
    child = candidate_map.get(adopted_id)
    source_id = (child or {}).get("source_b1_id") or audit.get("source_b1_id")
    source = candidate_map.get(source_id)
    formal = ((source or {}).get("structure") or {}).get("formal_first_buy") or {}
    formal_a = (formal.get("centers") or {}).get("A") or {}
    formal_b = (formal.get("centers") or {}).get("B") or {}
    if formal_a and formal_b:
        for row in capture["pair_diagnostics"]:
            row.pop("selected_by_engine", None)
        selected = next((
            row for row in capture["pair_diagnostics"]
            if row.get("center1_dates", [None])[0][:10] == str(formal_a.get("start_at"))[:10]
            and row.get("center2_dates", [None])[0][:10] == str(formal_b.get("start_at"))[:10]
        ), None)
        if selected is None:
            raise RuntimeError("frozen report could not bind the exact formal first-buy center pair")
        seg_a = (formal.get("segments") or {}).get("A") or {}
        seg_c = (formal.get("segments") or {}).get("C") or {}
        selected.update({
            "selected_by_engine": True,
            "A_start": seg_a.get("start_at"), "A_end": seg_a.get("end_at"),
            "A_area": seg_a.get("area"), "A_length": seg_a.get("length"),
            "C_start": seg_c.get("start_at"), "C_end": seg_c.get("end_at"),
            "C_low": seg_c.get("low"), "C_low_confirm_at": seg_c.get("low_confirm_at"),
            "C_area": seg_c.get("area"), "C_length": seg_c.get("length"),
            "force_ratio": formal.get("average_force_ratio_C_to_A"),
            "downshift": bool((formal.get("invariants") or {}).get(
                "center_interval_downshift_ZG2_lt_ZD1")),
            "strict_isolation": bool((formal.get("diagnostics") or {}).get(
                "strong_full_range_isolation_GG2_lt_DD1")),
            "new_low": bool((formal.get("invariants") or {}).get(
                "C_makes_new_low_below_B_DD")),
            "generated": all((formal.get("invariants") or {}).values()),
            "reason": "exact engine formal-first-buy evidence",
            "provenance_id": formal.get("provenance_id"),
        })
    ids = {source_id, adopted_id}
    events = [
        row for row in replay["audit_trace"]["candidate_events"]
        if row.get("candidate_id") in ids
    ]
    trace_pens = audit.get("structure", {}).get("m30_bis", [])
    m30_pens = [{
        "id": row.get("bi_id"),
        "direction": row.get("direction"),
        "start": row.get("start_at"),
        "end": row.get("end_at"),
        "confirm": row.get("confirm_at"),
        "start_price": row.get("start_price"),
        "end_price": row.get("end_price"),
        "high": row.get("high"),
        "low": row.get("low"),
    } for row in trace_pens]

    selected_pair = next(
        (row for row in capture["pair_diagnostics"] if row.get("selected_by_engine")),
        None,
    )
    local_pair = capture["pair_diagnostics"][-1] if capture["pair_diagnostics"] else None
    center_map = {row["id"]: row for row in capture["centers"]}
    for row in capture["centers"]:
        row["role"] = "normal"
    if selected_pair:
        for center_id in (selected_pair["center1"], selected_pair["center2"]):
            if center_id in center_map:
                center_map[center_id]["role"] = "selected-stale"
    if local_pair:
        for center_id in (local_pair["center1"], local_pair["center2"]):
            if center_id in center_map:
                center_map[center_id]["role"] = "latest-local"

    buy_name = str(trade.get("name") or "买点")
    source_signal = (source or child or {}).get("signal_at") or SIGNAL_DATE
    daily_signal_label = "源一买候选" if source else f"{buy_name}候选"

    return {
        "schema": "render-chan-interactive-report/raw-v1",
        "symbol": SYMBOL,
        "generated_at": datetime.now().astimezone().isoformat(),
        "structure_snapshot_at": STRUCTURE_SNAPSHOT_AT,
        "structure_snapshot_basis": STRUCTURE_SNAPSHOT_BASIS,
        "structure_asof_date": STRUCTURE_ASOF_DATE,
        "daily": daily,
        "m30": m30,
        "daily_pens": capture["pens"],
        "m30_pens": m30_pens,
        "centers": capture["centers"],
        "pair_diagnostics": capture["pair_diagnostics"],
        "trade": {key: value for key, value in trade.items() if key != "audit"},
        "trade_audit": audit,
        "source": source,
        "child": child,
        "events": events,
        "selected_pair_id": selected_pair["id"] if selected_pair else None,
        "local_pair_id": local_pair["id"] if local_pair else None,
        "verdict": {
            "classification": "REVIEW_REQUIRED",
            "tag": "交互证据",
            "title": "结构证据已生成，结论待审查",
            "summary": "本页只冻结引擎结构与交易因果链，不把图形外观直接当作严格缠论结论。",
        },
        "markers_daily": [
            {"at": source_signal, "label": daily_signal_label, "kind": "signal"},
            {"at": ENTRY_DATE, "label": f"{buy_name}入场", "kind": "entry"},
            {"at": trade["exit"], "label": "止损离场", "kind": "exit"},
        ],
        "markers_m30": [
            {"at": audit.get("occur_at"), "label": f"{buy_name}发生", "kind": "signal"},
            {"at": audit.get("decision_at"), "label": "入场决策", "kind": "decision"},
            {"at": audit.get("fill_at"), "label": "入场成交", "kind": "entry"},
            {"at": audit.get("exit_fill_at"), "label": "止损成交", "kind": "exit"},
        ],
    }


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>002486.SZ 全证据交互审查</title>
<style>
:root{--bg:#07101e;--panel:#101a2d;--panel2:#15223a;--ink:#e9f1ff;--muted:#91a3be;--line:#2a3a58;--red:#fb7185;--green:#34d399;--blue:#60a5fa;--gold:#facc15;--purple:#a78bfa;--orange:#fb923c}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(150deg,#07101e,#101a30 58%,#08111f);color:var(--ink);font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1540px;margin:auto;padding:24px}h1{font-size:30px;margin:0 0 4px}h2{font-size:21px;margin:0 0 10px}h3{font-size:16px;margin:0 0 8px}.muted{color:var(--muted)}.hero,.section{background:rgba(16,26,45,.96);border:1px solid var(--line);border-radius:14px;padding:18px;margin:16px 0}.hero{border-left:5px solid var(--red)}.hero-title{font-size:22px;font-weight:700;margin:8px 0}.badge{display:inline-block;padding:3px 9px;border-radius:999px;background:#502434;color:#ffbdc7;margin-right:6px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:9px;margin-top:14px}.metric{background:var(--panel2);padding:10px;border-radius:8px;border:1px solid var(--line)}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{font-size:17px}.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0 10px}.controls button,.controls select{border:1px solid var(--line);background:#172640;color:var(--ink);padding:6px 10px;border-radius:7px;cursor:pointer}.controls button:hover{background:#203453}.controls label{display:inline-flex;align-items:center;gap:5px;color:#c8d6ec}.chart-shell{position:relative;border:1px solid var(--line);border-radius:10px;background:#0a1324;overflow:hidden}.chart-shell canvas{display:block;width:100%;height:560px;cursor:crosshair}.tooltip{display:none;position:absolute;z-index:5;pointer-events:none;max-width:320px;background:#07101eee;border:1px solid #52627d;border-radius:8px;padding:8px 10px;color:var(--ink);font-size:12px;white-space:nowrap}.legend{display:flex;gap:15px;flex-wrap:wrap;color:var(--muted);margin:6px 0}.sw{display:inline-block;width:12px;height:9px;margin-right:5px;vertical-align:middle}.sw.stale{background:#fb718566;border:1px solid var(--red)}.sw.local{background:#34d39944;border:1px solid var(--green)}.sw.other{background:#a78bfa33;border:1px solid var(--purple)}.sw.causal{background:#facc1544;border:2px solid var(--gold)}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:#b8c7df;background:#142139;position:sticky;top:0}tr[data-focus]{cursor:pointer}tr[data-focus]:hover{background:#1b2b47}.status-good{color:var(--green)}.status-bad{color:var(--red)}.status-warn{color:var(--gold)}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.explain{padding:12px 14px;background:#251d13;border-left:4px solid var(--gold);border-radius:7px}.event-line{display:grid;grid-template-columns:160px 120px 1fr;gap:9px;padding:7px 0;border-bottom:1px solid var(--line)}details{background:#0b1425;border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:10px}summary{cursor:pointer;font-weight:700}pre{white-space:pre-wrap;word-break:break-all;max-height:440px;overflow:auto;color:#c9d6ea}.foot{color:var(--muted);font-size:12px;margin:16px 2px}@media(max-width:820px){main{padding:12px}.grid2{grid-template-columns:1fr}.chart-shell canvas{height:460px}.event-line{grid-template-columns:1fr}.hero-title{font-size:19px}}
</style></head><body><main>
<h1>002486.SZ 全证据交互审查</h1>
<div class="muted">结构按引擎首次看到候选（缺失时按决策时点）的可见数据冻结；滚轮缩放、拖动平移、悬停查看K线与中枢，点击表格行定位结构。</div>
<section class="hero"><div><span class="badge">CODE_DEFECT</span><span class="badge">假一买源链</span></div><div class="hero-title" id="verdict-title"></div><div id="verdict-summary"></div><div class="metrics"><div class="metric"><span>实际采用中枢</span><strong>2020—2021旧结构</strong></div><div class="metric"><span>最近两中枢C/A力度比</span><strong class="status-bad">2.436</strong></div><div class="metric"><span>背驰门槛</span><strong>&lt; 0.900</strong></div><div class="metric"><span>交易结果</span><strong class="status-bad">-14.84%</strong></div></div></section>

<section class="section"><h2>日K：中枢、笔与候选配对</h2><div class="controls" id="daily-controls"><button data-range="context">默认全链</button><button data-range="local">最近结构</button><button data-range="signal">信号窗口</button><button data-range="full">全部数据</button><label><input type="checkbox" data-layer="pens" checked>笔</label><label><input type="checkbox" data-layer="centers" checked>中枢</label><label><input type="checkbox" data-layer="markers" checked>事件</label><label><input type="checkbox" data-layer="macd" checked>MACD</label></div><div class="legend"><span><i class="sw stale"></i>交易实际采用的陈旧中枢对</span><span><i class="sw local"></i>证据快照最近中枢对</span><span><i class="sw other"></i>其他中枢</span></div><div class="chart-shell"><canvas id="daily-chart" aria-label="002486日K交互图"></canvas><div class="tooltip" id="daily-tip"></div></div></section>

<section class="grid2"><div class="section"><h2>所有向下中枢</h2><div class="table-wrap"><table><thead><tr><th>中枢</th><th>时间</th><th>ZD—ZG</th><th>角色</th></tr></thead><tbody id="center-rows"></tbody></table></div></div><div class="section"><h2>相邻中枢对与背驰</h2><div class="table-wrap"><table><thead><tr><th>中枢对</th><th>下移</th><th>C/A力度</th><th>结果</th></tr></thead><tbody id="pair-rows"></tbody></table></div></div></section>

<section class="section"><h2>为什么这是陈旧中枢假一买</h2><div class="explain"><b>程序实际绑定：</b><span id="selected-pair-text"></span><br><b>当时最近结构：</b><span id="local-pair-text"></span><br><b>关键差异：</b>最近两中枢虽然下移且C段创新低，但C段平均下跌力度是A段的2.436倍，不是背驰。旧候选因为扫描顺序和 <span class="mono">(买点类型, 信号日期)</span> 去重键先占位，成为二买的源一买。</div></section>

<section class="section"><h2>30分钟：二买形成、成交与失效</h2><div class="controls" id="m30-controls"><button data-range="setup">形成过程</button><button data-range="position">持仓过程</button><button data-range="full">全部数据</button><label><input type="checkbox" data-layer="pens" checked>笔</label><label><input type="checkbox" data-layer="markers" checked>事件</label><label><input type="checkbox" data-layer="macd" checked>MACD</label></div><div class="legend"><span><i class="sw causal"></i>候选确认时实际采用的30分钟回抽笔</span><span><i class="sw other"></i>后来延伸后的完整30分钟笔</span></div><div class="chart-shell"><canvas id="m30-chart" aria-label="002486三十分钟交互图"></canvas><div class="tooltip" id="m30-tip"></div></div></section>

<section class="section"><h2>候选生命周期</h2><div id="events"></div><details><summary>完整交易谓词与证据</summary><pre id="raw-evidence"></pre></details></section>
<div class="foot">紫色普通中枢和笔均按引擎证据快照冻结；历史 signal_at 只作事件标记，不决定结构边界。中枢带只覆盖真实起止日期，不再横铺整张图。所有成交核对使用数据库原始30分钟K线。</div>
</main><script type="application/json" id="audit-data">__DATA__</script>
<script>
const DATA=JSON.parse(document.getElementById('audit-data').textContent);
const COLORS={bg:'#0a1324',grid:'#263650',text:'#8fa2bd',up:'#34d399',down:'#fb7185',pen:'#60a5fa',center:'#a78bfa',stale:'#fb7185',local:'#34d399',signal:'#facc15',entry:'#38bdf8',exit:'#fb923c',invalid:'#ef4444',decision:'#c084fc'};
function t(v){let s=String(v??'').trim();if(/^\d{4}-\d{2}-\d{2} \d{6}$/.test(s))s=s.slice(0,13)+':'+s.slice(13,15)+':'+s.slice(15,17);let x=Date.parse(s.replace(' ','T'));return Number.isFinite(x)?x:NaN}
function fmt(v,n=2){return Number(v).toFixed(n)}
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function nearestIndex(rows,time){let lo=0,hi=rows.length-1,x=t(time);while(lo<hi){let m=(lo+hi)>>1;if(t(rows[m].at)<x)lo=m+1;else hi=m}if(lo>0&&Math.abs(t(rows[lo-1].at)-x)<Math.abs(t(rows[lo].at)-x))return lo-1;return lo}
class KChart{
 constructor(canvas,tip,rows,pens,centers,markers,ranges,causalPens=[]){this.c=canvas;this.ctx=canvas.getContext('2d');this.tip=tip;this.rows=rows;this.pens=pens||[];this.causalPens=causalPens||[];this.centers=centers||[];this.markers=(markers||[]).filter(x=>x.at);this.ranges=ranges;this.layers={pens:true,centers:true,markers:true,macd:true};this.start=0;this.end=rows.length;this.drag=false;this.cross=null;this.selected=new Set();this.bind();new ResizeObserver(()=>this.draw()).observe(canvas.parentElement)}
 range(name){let r=this.ranges[name];if(!r){this.start=0;this.end=this.rows.length}else{this.start=Math.max(0,nearestIndex(this.rows,r[0]));this.end=Math.min(this.rows.length,nearestIndex(this.rows,r[1])+1)}if(this.end-this.start<20)this.end=Math.min(this.rows.length,this.start+20);this.draw()}
 focus(a,b,ids=[]){let s=nearestIndex(this.rows,a),e=nearestIndex(this.rows,b),pad=Math.max(12,Math.round((e-s+1)*.35));this.start=Math.max(0,s-pad);this.end=Math.min(this.rows.length,e+pad);this.selected=new Set(ids);this.draw()}
 bind(){this.c.addEventListener('wheel',e=>{e.preventDefault();let r=this.c.getBoundingClientRect(),p=(e.clientX-r.left)/r.width,n=this.end-this.start,newN=Math.max(20,Math.min(this.rows.length,Math.round(n*(e.deltaY>0?1.18:.84)))),anchor=this.start+p*n;this.start=Math.round(anchor-p*newN);this.end=this.start+newN;this.clamp();this.draw()},{passive:false});this.c.addEventListener('pointerdown',e=>{this.drag=true;this.lastX=e.clientX;this.c.setPointerCapture(e.pointerId)});this.c.addEventListener('pointerup',()=>this.drag=false);this.c.addEventListener('pointerleave',()=>{this.drag=false;this.cross=null;this.tip.style.display='none';this.draw()});this.c.addEventListener('pointermove',e=>{let r=this.c.getBoundingClientRect();if(this.drag){let step=(r.width-76)/(this.end-this.start),shift=Math.round((this.lastX-e.clientX)/Math.max(step,.01));if(shift){this.start+=shift;this.end+=shift;this.lastX=e.clientX;this.clamp();this.draw()}return}let idx=Math.floor(this.start+(e.clientX-r.left-58)/Math.max((r.width-76)/(this.end-this.start),.01));idx=Math.max(this.start,Math.min(this.end-1,idx));this.cross=idx;this.showTip(e,idx);this.draw()})}
 clamp(){let n=this.end-this.start;if(this.start<0){this.start=0;this.end=n}if(this.end>this.rows.length){this.end=this.rows.length;this.start=Math.max(0,this.end-n)}}
 showTip(e,i){let row=this.rows[i],inside=this.centers.filter(z=>t(z.start)<=t(row.at)&&t(row.at)<=t(z.end));this.tip.innerHTML=`<b>${esc(row.at)}</b><br>O ${fmt(row.o)}　H ${fmt(row.h)}　L ${fmt(row.l)}　C ${fmt(row.c)}<br>MACD ${fmt(row.macd,4)}${inside.length?'<br>中枢 '+inside.map(z=>esc(z.id+' ['+z.ZD.toFixed(2)+','+z.ZG.toFixed(2)+']')).join(', '):''}`;let shell=this.c.parentElement,rr=shell.getBoundingClientRect();this.tip.style.display='block';this.tip.style.left=Math.min(e.clientX-rr.left+14,rr.width-330)+'px';this.tip.style.top=Math.max(8,e.clientY-rr.top-80)+'px'}
 draw(){let box=this.c.getBoundingClientRect(),dpr=devicePixelRatio||1,w=Math.max(320,box.width),h=box.height;this.c.width=w*dpr;this.c.height=h*dpr;let x=this.ctx;x.setTransform(dpr,0,0,dpr,0,0);x.fillStyle=COLORS.bg;x.fillRect(0,0,w,h);let L=58,R=18,T=18,B=28,macdH=this.layers.macd?115:0,priceB=h-B-macdH,plotW=w-L-R,n=Math.max(1,this.end-this.start),visible=this.rows.slice(this.start,this.end),lo=Math.min(...visible.map(d=>d.l)),hi=Math.max(...visible.map(d=>d.h)),pad=Math.max((hi-lo)*.06,.001);lo-=pad;hi+=pad;let xx=i=>L+(i-this.start+.5)*plotW/n,yy=v=>T+(hi-v)/(hi-lo)*(priceB-T);x.font='12px system-ui';x.textBaseline='middle';for(let k=0;k<5;k++){let v=lo+(hi-lo)*k/4,y=yy(v);x.strokeStyle=COLORS.grid;x.beginPath();x.moveTo(L,y);x.lineTo(w-R,y);x.stroke();x.fillStyle=COLORS.text;x.fillText(v.toFixed(2),5,y)}
 if(this.layers.centers)for(let z of this.centers){let s=nearestIndex(this.rows,z.start),e=nearestIndex(this.rows,z.end);if(e<this.start||s>=this.end)continue;let color=z.role==='selected-stale'?COLORS.stale:z.role==='latest-local'?COLORS.local:COLORS.center;x.globalAlpha=z.role==='normal'?.14:.25;x.fillStyle=color;x.fillRect(Math.max(L,xx(s)-3),yy(z.ZG),Math.max(3,Math.min(w-R,xx(e)+3)-Math.max(L,xx(s)-3)),Math.max(2,yy(z.ZD)-yy(z.ZG)));x.globalAlpha=1;x.strokeStyle=color;x.lineWidth=this.selected.has(z.id)?3:1;x.strokeRect(Math.max(L,xx(s)-3),yy(z.ZG),Math.max(3,Math.min(w-R,xx(e)+3)-Math.max(L,xx(s)-3)),Math.max(2,yy(z.ZD)-yy(z.ZG)));if(n<700){x.fillStyle=color;x.fillText(z.id,Math.max(L+2,xx(s)),Math.max(T+8,yy(z.ZG)-7))}}
 let bw=Math.max(.6,Math.min(7,plotW/n*.62));for(let i=this.start;i<this.end;i++){let d=this.rows[i],px=xx(i),yo=yy(d.o),yc=yy(d.c),yh=yy(d.h),yl=yy(d.l);x.strokeStyle=d.c>=d.o?COLORS.up:COLORS.down;x.fillStyle=x.strokeStyle;x.beginPath();x.moveTo(px,yh);x.lineTo(px,yl);x.stroke();x.fillRect(px-bw/2,Math.min(yo,yc),bw,Math.max(1,Math.abs(yc-yo)))}
 if(this.layers.pens){let pts=[];for(let p of this.pens){for(let q of [[p.start,p.start_price],[p.end,p.end_price]]){let i=nearestIndex(this.rows,q[0]);if(i>=this.start-1&&i<=this.end)pts.push([i,Number(q[1])])}}pts.sort((a,b)=>a[0]-b[0]);x.strokeStyle=COLORS.pen;x.lineWidth=2;x.beginPath();pts.forEach((p,j)=>{let X=xx(p[0]),Y=yy(p[1]);j?x.lineTo(X,Y):x.moveTo(X,Y)});x.stroke()}
 if(this.layers.pens)for(let p of this.causalPens){let s=nearestIndex(this.rows,p.start),e=nearestIndex(this.rows,p.end);if(e<this.start||s>=this.end)continue;x.strokeStyle=COLORS.signal;x.lineWidth=4;x.setLineDash([7,4]);x.beginPath();x.moveTo(xx(s),yy(Number(p.start_price)));x.lineTo(xx(e),yy(Number(p.end_price)));x.stroke();x.setLineDash([]);x.fillStyle=COLORS.signal;x.beginPath();x.arc(xx(e),yy(Number(p.end_price)),4,0,Math.PI*2);x.fill()}
 if(this.layers.markers)for(let m of this.markers){let i=nearestIndex(this.rows,m.at);if(i<this.start||i>=this.end)continue;let color=COLORS[m.kind]||COLORS.signal,px=xx(i);x.strokeStyle=color;x.setLineDash([5,4]);x.beginPath();x.moveTo(px,T);x.lineTo(px,priceB);x.stroke();x.setLineDash([]);x.fillStyle=color;x.save();x.translate(px+4,T+8);x.rotate(Math.PI/2);x.fillText(m.label,0,0);x.restore()}
 if(this.layers.macd){let top=priceB+16,bot=h-B,zero=(top+bot)/2,max=Math.max(...visible.map(d=>Math.abs(d.macd)),1e-9);x.strokeStyle=COLORS.grid;x.beginPath();x.moveTo(L,zero);x.lineTo(w-R,zero);x.stroke();for(let i=this.start;i<this.end;i++){let d=this.rows[i],hh=Math.abs(d.macd)/max*(bot-top)/2;x.fillStyle=d.macd>=0?COLORS.up:COLORS.down;x.fillRect(xx(i)-Math.max(.5,bw/2),d.macd>=0?zero-hh:zero,Math.max(1,bw),Math.max(.5,hh))}}
 for(let k=0;k<5;k++){let i=Math.min(this.end-1,Math.round(this.start+(n-1)*k/4));x.fillStyle=COLORS.text;x.fillText(this.rows[i].at.slice(0,10),Math.min(w-88,Math.max(L,xx(i)-28)),h-10)}if(this.cross!=null&&this.cross>=this.start&&this.cross<this.end){x.strokeStyle='#dbeafe88';x.setLineDash([3,3]);x.beginPath();x.moveTo(xx(this.cross),T);x.lineTo(xx(this.cross),h-B);x.stroke();x.setLineDash([])}}
}
document.getElementById('verdict-title').textContent=DATA.verdict.title;document.getElementById('verdict-summary').textContent=DATA.verdict.summary;
const dailyRanges={context:['2020-08-01','2024-08-31'],local:['2023-02-01','2024-07-31'],signal:['2024-04-15','2024-06-30']};
const m30Ranges={setup:['2024-05-20 10:00:00','2024-06-20 15:00:00'],position:['2024-06-17 10:00:00','2024-06-25 15:00:00']};
const daily=new KChart(document.getElementById('daily-chart'),document.getElementById('daily-tip'),DATA.daily,DATA.daily_pens,DATA.centers,DATA.markers_daily,dailyRanges,[]);daily.range('context');
const m30=new KChart(document.getElementById('m30-chart'),document.getElementById('m30-tip'),DATA.m30,DATA.m30_pens,[],DATA.markers_m30,m30Ranges,DATA.causal_m30_pens||[]);m30.range('setup');
function bindControls(id,chart){let root=document.getElementById(id);root.querySelectorAll('button[data-range]').forEach(b=>b.onclick=()=>chart.range(b.dataset.range));root.querySelectorAll('input[data-layer]').forEach(c=>c.onchange=()=>{chart.layers[c.dataset.layer]=c.checked;chart.draw()})}bindControls('daily-controls',daily);bindControls('m30-controls',m30);
const centers=Object.fromEntries(DATA.centers.map(z=>[z.id,z]));let down=DATA.centers.filter(z=>z.direction==='down');document.getElementById('center-rows').innerHTML=down.map(z=>`<tr data-focus="${z.id}"><td>${esc(z.id)}</td><td>${z.start.slice(0,10)} → ${z.end.slice(0,10)}</td><td>${fmt(z.ZD)}—${fmt(z.ZG)}</td><td class="${z.role==='selected-stale'?'status-bad':z.role==='latest-local'?'status-good':''}">${z.role==='selected-stale'?'实际采用旧结构':z.role==='latest-local'?'最近结构':'普通'}</td></tr>`).join('');
document.querySelectorAll('#center-rows tr').forEach(tr=>tr.onclick=()=>{let z=centers[tr.dataset.focus];daily.focus(z.start,z.end,[z.id])});
document.getElementById('pair-rows').innerHTML=DATA.pair_diagnostics.map(p=>`<tr data-focus="${p.id}"><td>${esc(p.center1)} + ${esc(p.center2)}${p.selected_by_engine?'<br><span class="status-bad">实际获胜</span>':p.latest_local_pair?'<br><span class="status-good">最近一对</span>':''}</td><td class="${p.downshift?'status-good':'status-bad'}">${p.downshift?'是':'否'}</td><td class="${p.force_ratio!=null&&p.force_ratio<.9?'status-good':'status-bad'}">${p.force_ratio==null?'—':fmt(p.force_ratio,3)}</td><td>${p.generated?'<span class="status-good">生成</span>':`<span class="status-bad">${esc(p.reason)}</span>`}</td></tr>`).join('');
document.querySelectorAll('#pair-rows tr').forEach(tr=>tr.onclick=()=>{let p=DATA.pair_diagnostics.find(x=>x.id===tr.dataset.focus),a=centers[p.center1],b=centers[p.center2];daily.focus(a.start,b.end,[a.id,b.id])});
let sp=DATA.pair_diagnostics.find(x=>x.id===DATA.selected_pair_id),lp=DATA.pair_diagnostics.find(x=>x.id===DATA.local_pair_id);document.getElementById('selected-pair-text').textContent=`${sp.center1_dates[0].slice(0,10)}—${sp.center2_dates[1].slice(0,10)}，第二中枢[${sp.center2_range.join(', ')}]，随后错误跨接到${sp.C_end}。`;document.getElementById('local-pair-text').textContent=`${lp.center1_dates[0].slice(0,10)}—${lp.center2_dates[1].slice(0,10)}，中枢下移=${lp.downshift}，创新低=${lp.new_low}，但C/A力度=${lp.force_ratio}。`;
document.getElementById('events').innerHTML=DATA.events.map(e=>`<div class="event-line"><span class="mono">${esc(e.at)}</span><b>${esc(e.event)}</b><span>${esc(e.reason||'')}</span></div>`).join('');document.getElementById('raw-evidence').textContent=JSON.stringify({structure_snapshot_at:DATA.structure_snapshot_at,structure_snapshot_basis:DATA.structure_snapshot_basis,structure_asof_date:DATA.structure_asof_date,timeline:DATA.timeline,trade:DATA.trade,trade_audit:DATA.trade_audit,source:DATA.source,child:DATA.child,pair_diagnostics:DATA.pair_diagnostics},null,2);
</script></body></html>'''


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    evidence = collect()
    evidence_text = json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    evidence_path = OUTPUT / "evidence.json"
    evidence_path.write_text(evidence_text, "utf-8")
    safe_data = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    report = HTML.replace("__DATA__", safe_data)
    report_path = OUTPUT / "report.html"
    report_path.write_text(report, "utf-8")
    manifest = {
        "schema": "002486-interactive-manifest/v1",
        "symbol": SYMBOL,
        "database_mode": "read-only",
        "database_sha256": sha256_file(PROJECT / "kline.db"),
        "engine_sha256": sha256_file(PROJECT / "backtest_daily_30min.py"),
        "result_sha256": sha256_file(PROJECT / "bt/random_200_backtest_result_seed_20260816.json"),
        "pdf_sha256": sha256_file(PROJECT / "108.pdf"),
        "evidence_sha256": sha256_file(evidence_path),
        "report_sha256": sha256_file(report_path),
    }
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", "utf-8"
    )
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
