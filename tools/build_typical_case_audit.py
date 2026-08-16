#!/usr/bin/env python3
"""Build a focused, visual Chan case-study bundle and standalone HTML report."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import backtest_daily_30min as engine  # noqa: E402


CASES = [
    {"symbol": "002486.SZ", "entry": "2024-06-19", "role": "failure", "group": "A", "label": "第一次二买失败"},
    {"symbol": "002486.SZ", "entry": "2024-07-26", "role": "control", "group": "A", "label": "同股后续二买盈利对照"},
    {"symbol": "600635.SH", "entry": "2025-04-03", "role": "failure", "group": "B", "label": "盘整背驰源二买失败"},
    {"symbol": "002730.SZ", "entry": "2025-12-05", "role": "control", "group": "B", "label": "盘整背驰源二买盈利对照"},
    {"symbol": "300782.SZ", "entry": "2026-07-09", "role": "failure", "group": "C", "label": "三买回到中枢失败"},
    {"symbol": "002637.SZ", "entry": "2025-12-17", "role": "control", "group": "C", "label": "三买盈利对照"},
]


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def parse_time(value: Any) -> dt.datetime | None:
    text = str(value or "").strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d", "%Y%m%d%H%M%S"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def iso(value: Any) -> str | None:
    parsed = parse_time(value)
    if not parsed:
        return None
    return parsed.strftime("%Y-%m-%d %H:%M:%S" if parsed.time() != dt.time() else "%Y-%m-%d")


def reset_engine() -> None:
    engine.clear_all()
    engine.clear_daily_cache()
    engine.clear_30min_cache()
    engine._FRESH_B1_CACHE.clear()


def query_bars(connection: sqlite3.Connection, table: str, time_col: str, symbol: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT {time_col},open,high,low,close,vol,amount FROM {table} WHERE code=? ORDER BY {time_col}",
        (symbol,),
    )
    output = []
    for row in rows:
        parsed = parse_time(row[0])
        if not parsed:
            continue
        output.append({
            "at": parsed.strftime("%Y-%m-%d %H:%M:%S" if parsed.time() != dt.time() else "%Y-%m-%d"),
            "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]),
            "vol": float(row[5] or 0), "amount": float(row[6] or 0),
        })
    return output


def window(values: list[dict[str, Any]], start: dt.datetime, end: dt.datetime, limit: int = 520) -> list[dict[str, Any]]:
    selected = [row for row in values if start <= parse_time(row["at"]) <= end]
    if len(selected) <= limit:
        return selected
    stride = math.ceil(len(selected) / limit)
    # Keep full OHLC semantics when compressing only for visualization.
    output = []
    for offset in range(0, len(selected), stride):
        part = selected[offset:offset + stride]
        output.append({
            "at": part[-1]["at"], "open": part[0]["open"], "high": max(x["high"] for x in part),
            "low": min(x["low"] for x in part), "close": part[-1]["close"],
            "vol": sum(x["vol"] for x in part), "amount": sum(x["amount"] for x in part),
            "visual_aggregate_count": len(part),
        })
    return output


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def macd_hist(values: list[float]) -> list[float]:
    fast, slow = ema(values, 12), ema(values, 26)
    diff = [a - b for a, b in zip(fast, slow)]
    dea = ema(diff, 9)
    return [(a - b) * 2 for a, b in zip(diff, dea)]


def select_pens(pens: list[dict[str, Any]], start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
    output = []
    for pen in pens or []:
        a, b = parse_time(pen.get("start_at")), parse_time(pen.get("end_at"))
        if a and b and b >= start and a <= end:
            output.append(pen)
    return output


def slice_hash(rows: list[dict[str, Any]]) -> str:
    return sha256_bytes(canonical(rows))


def compact_event(event: dict[str, Any]) -> bool:
    return event.get("event") in {"generated", "activated", "invalidated", "superseded", "consumed", "filled", "updated"}


def collect(project: Path, database: Path) -> dict[str, Any]:
    result = json.loads((project / "bt/random_200_backtest_result_seed_20260816.json").read_text("utf-8"))
    original = {(t["symbol"], t["entry"]): t for stock in result["results"] for t in stock.get("trades", [])}
    grouped: dict[str, dict[str, Any]] = {}
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        for symbol in sorted({item["symbol"] for item in CASES}):
            reset_engine()
            replay = engine.backtest_one(symbol, "20240101", "20260805", conn=connection, audit_trace=True)
            daily_all = query_bars(connection, "daily_kline", "date", symbol)
            m30_all = query_bars(connection, "kline_30min", "datetime", symbol)
            grouped[symbol] = {"replay": replay, "daily": daily_all, "m30": m30_all}
    finally:
        connection.close()

    records = []
    for spec in CASES:
        symbol, entry = spec["symbol"], spec["entry"]
        payload = grouped[symbol]
        trade = next((x for x in payload["replay"]["trades"] if x.get("entry") == entry), None)
        if not trade:
            raise RuntimeError(f"current engine did not reproduce {symbol} {entry}")
        historical = original[(symbol, entry)]
        stripped = {key: value for key, value in trade.items() if key != "audit"}
        if stripped != historical:
            raise RuntimeError(f"trace run changed historical fields for {symbol} {entry}")
        audit = trade["audit"]
        candidates = {row["candidate_id"]: row for row in payload["replay"]["audit_trace"]["candidates"]}
        candidate = candidates.get(audit.get("candidate_id"))
        source = candidates.get(audit.get("source_b1_id"))
        source_at = parse_time((source or {}).get("occur_at")) or parse_time(audit.get("occur_at")) or parse_time(entry)
        exit_at = parse_time(trade.get("exit")) or parse_time(entry)
        entry_at = parse_time(entry)
        daily_start, daily_end = source_at - dt.timedelta(days=560), exit_at + dt.timedelta(days=80)
        m30_start, m30_end = source_at - dt.timedelta(days=45), exit_at + dt.timedelta(days=20)
        daily = window(payload["daily"], daily_start, daily_end, 420)
        m30 = window(payload["m30"], m30_start, m30_end, 620)
        structure = audit.get("structure") or {}
        relevant_ids = {audit.get("candidate_id"), audit.get("source_b1_id")}
        events = [row for row in payload["replay"]["audit_trace"]["candidate_events"] if row.get("candidate_id") in relevant_ids and compact_event(row)]
        record = {
            "case_id": f"{symbol}-{entry}", **spec, "trade": historical, "trace_trade": audit,
            "candidate": candidate, "source_b1": source, "candidate_events": events,
            "risk_flags": payload["replay"]["audit_trace"].get("risk_flags", []),
            "daily_bars": daily, "m30_bars": m30,
            "daily_pens": select_pens(structure.get("daily_bis", []), daily_start, daily_end),
            "m30_pens": select_pens(structure.get("m30_bis", []), m30_start, m30_end),
            "hashes": {"daily_bars": slice_hash(daily), "m30_bars": slice_hash(m30)},
            "time_order": {
                "occur_at": audit.get("occur_at"), "confirm_at": audit.get("confirm_at"),
                "first_seen_at": audit.get("first_seen_at"), "decision_at": audit.get("decision_at"),
                "fill_at": audit.get("fill_at"), "exit_decision_at": audit.get("exit_decision_at"),
                "exit_fill_at": audit.get("exit_fill_at"),
            },
            "price_checks": {
                "entry_matches_fill_open": any(
                    parse_time(row["at"]) == parse_time(audit.get("fill_at")) and abs(row["open"] - trade["entry_price"]) < 1e-9
                    for row in payload["m30"]
                ),
                "exit_matches_fill_open": any(
                    parse_time(row["at"]) == parse_time(audit.get("exit_fill_at")) and abs(row["open"] - trade["exit_price"]) < 1e-9
                    for row in payload["m30"]
                ),
            },
        }
        records.append(record)
    return {"schema_version": "typical-chan-cases/v1", "cases": records}


def nearest_index(rows: list[dict[str, Any]], value: Any) -> int | None:
    target = parse_time(value)
    if not rows or not target:
        return None
    return min(range(len(rows)), key=lambda index: abs((parse_time(rows[index]["at"]) - target).total_seconds()))


def chart_svg(rows: list[dict[str, Any]], pens: list[dict[str, Any]], markers: list[tuple[str, Any, str]], center: dict[str, Any] | None, title: str) -> str:
    if not rows:
        return '<div class="empty">没有可视化数据</div>'
    width, height, left, right, top, price_bottom, macd_top, bottom = 1200, 500, 58, 18, 34, 360, 382, 472
    lows, highs = [x["low"] for x in rows], [x["high"] for x in rows]
    low, high = min(lows), max(highs)
    padding = max((high - low) * 0.06, abs(high) * 0.002, 1e-6)
    low, high = low - padding, high + padding
    plot_w = width - left - right
    step = plot_w / max(len(rows), 1)
    def x(index: int) -> float: return left + (index + .5) * step
    def y(value: float) -> float: return top + (high - value) / (high - low) * (price_bottom - top)
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
             f'<rect width="{width}" height="{height}" fill="#0c1324" rx="10"/>',
             f'<text x="{left}" y="22" fill="#dce8ff" font-size="15" font-weight="700">{html.escape(title)}</text>']
    for tick in range(5):
        value = low + (high - low) * tick / 4
        yy = y(value)
        parts.append(f'<line x1="{left}" x2="{width-right}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="#26344e" stroke-width="1"/>')
        parts.append(f'<text x="4" y="{yy+4:.1f}" fill="#8494ad" font-size="11">{value:.2f}</text>')
    if center:
        zd, zg = center.get("ZD"), center.get("ZG")
        if isinstance(zd, (int, float)) and isinstance(zg, (int, float)) and low <= zg and zd <= high:
            upper, lower = y(min(zg, high)), y(max(zd, low))
            yy, hh = min(upper, lower), abs(lower - upper)
            parts.append(f'<rect x="{left}" y="{yy:.1f}" width="{plot_w:.1f}" height="{max(hh,2):.1f}" fill="#a78bfa" opacity=".17"/>')
            parts.append(f'<text x="{width-right-125}" y="{yy+14:.1f}" fill="#c4b5fd" font-size="11">中枢 ZD {zd:.2f} / ZG {zg:.2f}</text>')
    body_w = max(min(step * .62, 6), .7)
    for index, row in enumerate(rows):
        xx = x(index); yo, yc, yh, yl = y(row["open"]), y(row["close"]), y(row["high"]), y(row["low"])
        color = "#35d399" if row["close"] >= row["open"] else "#fb7185"
        parts.append(f'<line x1="{xx:.2f}" x2="{xx:.2f}" y1="{yh:.2f}" y2="{yl:.2f}" stroke="{color}" stroke-width="1"/>')
        parts.append(f'<rect x="{xx-body_w/2:.2f}" y="{min(yo,yc):.2f}" width="{body_w:.2f}" height="{max(abs(yc-yo),1):.2f}" fill="{color}"/>')
    pen_points = []
    for pen in pens:
        for key_at, key_price in (("start_at", "start_price"), ("end_at", "end_price")):
            idx = nearest_index(rows, pen.get(key_at))
            value = pen.get(key_price)
            if idx is not None and isinstance(value, (int, float)):
                pen_points.append((idx, float(value)))
    pen_points = sorted(set(pen_points))
    if pen_points:
        points = " ".join(f"{x(idx):.1f},{y(value):.1f}" for idx, value in pen_points)
        parts.append(f'<polyline points="{points}" fill="none" stroke="#60a5fa" stroke-width="2.2" opacity=".92"/>')
    marker_colors = ["#facc15", "#38bdf8", "#f97316", "#ef4444"]
    for marker_index, (label, at, _) in enumerate(markers):
        idx = nearest_index(rows, at)
        if idx is None: continue
        xx, color = x(idx), marker_colors[marker_index % len(marker_colors)]
        parts.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{top}" y2="{price_bottom}" stroke="{color}" stroke-width="1.4" stroke-dasharray="5 4"/>')
        parts.append(f'<text x="{min(xx+4,width-130):.1f}" y="{top+14+marker_index*14}" fill="{color}" font-size="11">{html.escape(label)}</text>')
    hist = macd_hist([row["close"] for row in rows])
    max_hist = max(max((abs(value) for value in hist), default=0), 1e-9)
    zero = (macd_top + bottom) / 2
    parts.append(f'<line x1="{left}" x2="{width-right}" y1="{zero:.1f}" y2="{zero:.1f}" stroke="#50617d"/>')
    for index, value in enumerate(hist):
        hh = abs(value) / max_hist * (bottom - macd_top) / 2
        color = "#fb7185" if value < 0 else "#35d399"
        yy = zero if value < 0 else zero - hh
        parts.append(f'<rect x="{x(index)-max(body_w/2,.45):.2f}" y="{yy:.2f}" width="{max(body_w, .9):.2f}" height="{max(hh,.5):.2f}" fill="{color}" opacity=".72"/>')
    for fraction in (0, .25, .5, .75, 1):
        index = min(round((len(rows)-1) * fraction), len(rows)-1)
        parts.append(f'<text x="{x(index)-28:.1f}" y="492" fill="#8494ad" font-size="10">{html.escape(rows[index]["at"][:10])}</text>')
    parts.append('</svg>')
    return "".join(parts)


DEFAULT_FINDINGS = {
    "002486.SZ-2024-06-19": {
        "label": "不能简单叫假一买：源链次日失效，持仓没有联动处理",
        "classification": "源链存活管理风险",
        "confidence": "MEDIUM",
        "engine_verdict": "当前引擎按规则触发二买；6月20日源一买和派生候选先后失效，但已开仓仓位继续持有至硬止损。",
        "strict_verdict": "UNKNOWN：入场前 1.50→1.65→1.51 的30分钟代理结构已确认，与二买相容；缺完整次级别走势和A/C力度，不能证明正宗日线一买，也不能因后来跌到1.31倒判为假。",
        "summary": "主要暴露的是源一买失效后的仓位/候选链管理，而不是已经证实的“假一买”。日线与30分钟在入场附近还有1个最小价位差异，但目前没有证据表明修正后交易会消失。",
        "markers": [
            ["源链失效", "2024-06-20 10:00:00", "invalid"],
            ["派生候选失效", "2024-06-20 10:30:00", "invalid"],
        ],
    },
    "002486.SZ-2024-07-26": {
        "label": "同股新低后形成的新源链盈利，说明关键在结构时点",
        "classification": "盈利对照",
        "confidence": "MEDIUM",
        "engine_verdict": "后续新低附近重新形成一买—二买链，当前引擎按同类规则入场并盈利 +53.85%。",
        "strict_verdict": "UNKNOWN：盈利不会自动证明它是严格缠论二买；同样缺完整次级别走势与 source 结构原子。",
        "summary": "同一股票前后两笔一亏一盈，说明不能把“二买”名称本身当成胜率保证；源链的形成与失效时点更重要。",
    },
    "600635.SH-2025-04-03": {
        "label": "最典型：类一买源早已失效，陈旧二买仍被买入",
        "classification": "类一买 + 陈旧候选",
        "confidence": "HIGH",
        "engine_verdict": "源一买3月13日已被实时重建判无效，但派生二买没有同步失效，仍在4月3日被消费成交。",
        "strict_verdict": "严格证据整体仍为 UNKNOWN；但可见笔几何明确看不到两个下移中枢，源信号又是盘整背驰，04-03也不是源一买后的首次第二段低点。",
        "summary": "这就是最接近“假一买导致二买失败”的案例。更准确的说法是：类一买/盘整背驰被当作正宗一买使用，同时失效源链未级联清理，产生了三周后的陈旧二买。",
        "markers": [["源一买失效", "2025-03-13 10:00:00", "invalid"]],
    },
    "002730.SZ-2025-12-05": {
        "label": "同类盘整背驰代理也能盈利，但不能因此升级为正宗一买",
        "classification": "盈利对照",
        "confidence": "MEDIUM",
        "engine_verdict": "同为盘整背驰源二买，最终盈利 +84.08%。",
        "strict_verdict": "UNKNOWN：仍缺严格次级别走势和源一买链证明，且实际入场也明显晚于源后首轮回抽。",
        "summary": "这个盈利对照说明“代理不严格”与“最后是否赚钱”是两回事；不能因为赚了就把盘整背驰叫成正宗趋势一买。",
    },
    "300782.SZ-2026-07-09": {
        "label": "不是假一买，而是非首次回抽的“晚到三买”",
        "classification": "晚到三买代理",
        "confidence": "HIGH",
        "engine_verdict": "当前三买只有“离开→回抽不进中枢”两态，没有锁定首次回抽，所以7月的后续回抽仍忠实满足当前宽松谓词。",
        "strict_verdict": "UNKNOWN：严格日线中枢缺5分钟递归证据；但在现有日K笔/30分钟笔代理下，04-30第一次回抽已完成，06-12又有一次，07-09明确不是首次回抽。",
        "summary": "日K笔代理中枢约 ZD=77.21、ZG=86.28。离开后第一次回抽在4月30日止于99.12，6月12日又回到87.60；7月9日才入场，属于后续多次回抽仍被当作三买，不是假一买问题。",
        "markers": [
            ["首次回抽", "2026-04-30", "first_pullback"],
            ["再次回抽", "2026-06-12", "later_pullback"],
            ["实际触发低点", "2026-07-08 11:00:00", "later_pullback"],
        ],
    },
    "002637.SZ-2025-12-17": {
        "label": "同样不是首次回抽却盈利，证明代理缺陷与盈亏无关",
        "classification": "盈利对照",
        "confidence": "HIGH",
        "engine_verdict": "当前宽松三买代理触发后盈利 +34.22%。",
        "strict_verdict": "UNKNOWN：严格中枢证据不足；在笔代理下，首次回抽早在2025-07-09，12月入场同样是晚到回抽。",
        "summary": "它和失败的300782具有相同的“并非首次回抽”问题，却取得盈利，说明结论不是根据盈亏倒推，而是针对当前三买代理的结构缺口。",
        "markers": [["首次回抽", "2025-07-09", "first_pullback"]],
    },
}


def render(bundle: dict[str, Any], findings: dict[str, Any]) -> str:
    cases = bundle["cases"]
    groups = {key: [row for row in cases if row["group"] == key] for key in ("A", "B", "C")}
    cards = []
    for case in cases:
        trade, audit = case["trade"], case["trace_trade"]
        source, candidate = case.get("source_b1") or {}, case.get("candidate") or {}
        structure = (source or candidate).get("structure") or {}
        finding = findings.get(case["case_id"])
        markers = [
            ("源一买" if source else "信号发生", source.get("occur_at") or audit.get("occur_at"), "source"),
            ("入场", audit.get("fill_at") or trade.get("entry"), "entry"),
            ("离场", audit.get("exit_fill_at") or trade.get("exit"), "exit"),
        ]
        if finding:
            markers.extend(tuple(row) for row in finding.get("markers", []))
        daily_chart = chart_svg(case["daily_bars"], case["daily_pens"], markers, structure, f"{case['symbol']} 日K · 蓝线=日K笔 · 紫带=引擎日K笔中枢")
        m30_chart = chart_svg(case["m30_bars"], case["m30_pens"], markers, structure, f"{case['symbol']} 30分钟K · 蓝线=30分钟笔 · 下栏=MACD柱")
        conclusion = ""
        if finding:
            conclusion = f'<div class="verdict"><div><span class="tag">{html.escape(finding["classification"])}</span><span class="confidence">置信度 {html.escape(finding["confidence"])}</span></div><h3>{html.escape(finding["label"])}</h3><p>{html.escape(finding["summary"])}</p><div class="dual"><p><b>引擎口径：</b>{html.escape(finding.get("engine_verdict", "—"))}</p><p><b>严格缠论：</b>{html.escape(finding.get("strict_verdict", "—"))}</p></div></div>'
        event_rows = "".join(
            f"<tr><td>{html.escape(str(event.get('at')))}</td><td>{html.escape(str(event.get('event')))}</td><td>{html.escape(str(event.get('reason') or ''))}</td></tr>"
            for event in case["candidate_events"]
        ) or '<tr><td colspan="3">无关键状态事件</td></tr>'
        cards.append(f'''<article class="case {'failure' if case['role']=='failure' else 'control'}" id="{html.escape(case['case_id'])}">
<header><div><span class="role">{'失败案例' if case['role']=='failure' else '盈利对照'}</span><h2>{html.escape(case['symbol'])} · {html.escape(case['label'])}</h2></div><div class="pnl {'loss' if trade['pnl_pct'] <= 0 else 'win'}">{trade['pnl_pct']:+.2f}%</div></header>
{conclusion}
<div class="facts"><div><span>买点</span><strong>{html.escape(trade['name'])}</strong></div><div><span>背驰类型</span><strong>{html.escape(trade.get('div_type') or '—')}</strong></div><div><span>入场</span><strong>{trade['entry']} @ {trade['entry_price']}</strong></div><div><span>离场</span><strong>{trade['exit']} @ {trade['exit_price']}</strong></div><div><span>源一买ID</span><strong>{html.escape(str(audit.get('source_b1_id') or '无'))}</strong></div><div><span>成交核对</span><strong>{'PASS' if all(case['price_checks'].values()) else 'CHECK'}</strong></div></div>
<div class="chart-wrap">{daily_chart}</div><div class="chart-wrap">{m30_chart}</div>
<details><summary>候选链与时序</summary><table><thead><tr><th>时间</th><th>事件</th><th>原因</th></tr></thead><tbody>{event_rows}</tbody></table><pre>{html.escape(json.dumps({'timeline':case['time_order'],'source_b1':source,'candidate':candidate,'entry_predicates':audit.get('entry_predicates'),'exit_predicates':audit.get('exit_predicates')},ensure_ascii=False,indent=2))}</pre></details>
</article>''')
    summary_rows = "".join(
        f'<tr><td>{html.escape(row["symbol"])}</td><td>{html.escape(row["trade"]["name"])}</td><td>{row["trade"]["pnl_pct"]:+.2f}%</td><td>{html.escape((findings.get(row["case_id"]) or {}).get("label", "盈利对照"))}</td></tr>'
        for row in cases
    )
    embedded = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>典型个股缠论结构审查</title>
<style>:root{{--bg:#08101f;--panel:#111b30;--panel2:#17243d;--ink:#e8f0ff;--muted:#91a3bf;--line:#2b3b59;--red:#fb7185;--green:#35d399;--gold:#facc15}}*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(150deg,#07101e,#101a30 55%,#091322);color:var(--ink);font:14px/1.65 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1480px;margin:auto;padding:30px}}h1{{font-size:30px;margin:0}}.lead{{color:var(--muted);max-width:980px;font-size:15px}}.overview,.case{{background:rgba(17,27,48,.94);border:1px solid var(--line);border-radius:16px;padding:20px;margin:20px 0;box-shadow:0 18px 55px #0004}}table{{width:100%;border-collapse:collapse}}th,td{{border:1px solid var(--line);padding:8px 10px;text-align:left}}th{{background:#17243d}}.case header{{display:flex;justify-content:space-between;gap:18px;align-items:start}}.case h2{{margin:4px 0 10px;font-size:23px}}.role,.tag,.confidence{{display:inline-block;border-radius:999px;padding:3px 9px;background:#253653;color:#b9d4ff;margin-right:7px}}.failure .role{{background:#512434;color:#ffafbd}}.control .role{{background:#153f35;color:#8ff0c8}}.pnl{{font-size:30px;font-weight:800}}.loss{{color:var(--red)}}.win{{color:var(--green)}}.verdict{{border-left:4px solid var(--gold);background:#241f14;padding:15px 18px;border-radius:8px;margin:12px 0 18px}}.verdict h3{{font-size:20px;margin:8px 0 3px}}.confidence{{background:#3c3215;color:#ffe18c}}.dual{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}}.dual p{{margin:0;background:#151c2b;border:1px solid #3b4457;padding:10px 12px;border-radius:8px}}.facts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:13px 0}}.facts div{{background:var(--panel2);border:1px solid var(--line);padding:10px;border-radius:8px}}.facts span{{display:block;color:var(--muted);font-size:12px}}.chart-wrap{{overflow:auto;margin:14px 0}}.chart{{display:block;min-width:900px;width:100%;height:auto;border:1px solid var(--line);border-radius:10px}}details{{background:#0d1628;border:1px solid var(--line);padding:11px;border-radius:9px;margin-top:12px}}summary{{cursor:pointer;font-weight:700}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;color:#c8d5eb;background:#080e1a;padding:12px;border-radius:8px;max-height:500px;overflow:auto}}.note{{padding:12px 15px;border:1px solid #55471e;background:#241f14;border-radius:9px;color:#f8e4a3}}@media(max-width:700px){{main{{padding:14px}}.case header{{display:block}}.dual{{grid-template-columns:1fr}}}}</style></head><body><main>
<h1>典型个股缠论结构审查</h1><p class="lead">不是只看盈亏，而是把源一买、二买/三买触发、日K笔中枢、30分钟笔和成交时序放在同一张证据卡上。紫色区间是当前引擎的“日K笔中枢代理”，不是自动等同于严格缠论日线中枢。</p>
<div class="note"><b>结论边界：</b>当前代码已对原 200 股结果实现 200/200 全字段复现，三笔核心交易在冷/热缓存和正/逆顺序下也一致；但审计旁路的候选ID与事件次序并不完全稳定，所以“交易结果稳定”不等于“审计代码完全通过”。严格口径依照108课已核验页面，且不因后来下跌倒推当时信号真假。为控制独立HTML大小，长区间K线会按相邻bar做仅供显示的OHLC聚合；成交价格核对使用数据库中的原始30分钟bar。</div>
<section class="overview"><h2>先看结论</h2><table><thead><tr><th>股票</th><th>买点</th><th>结果</th><th>结构结论</th></tr></thead><tbody>{summary_rows}</tbody></table></section>
{''.join(cards)}
<section class="overview"><h2>理论锚点</h2><ul><li>第17课 / PDF物理页36：中枢由至少三个连续次级别走势类型重叠形成。</li><li>第21课 / 物理页45：二买必须与源一买紧密相连。</li><li>第24课 / 物理页52：MACD比较必须先证明A/B/C处于同一趋势结构。</li><li>第27课 / 物理页66：第一个中枢后的背驰属于盘整背驰，不等同趋势背驰一买。</li><li>第53课 / 物理页126-127：固定观察级别并检查完整次级别离开/回抽。</li></ul></section>
<script type="application/json" id="evidence-data">{embedded}</script></main></body></html>'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--findings", type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    database = (args.database or project / "kline.db").resolve()
    output = (args.output or project / "bt/audits/typical_case_review_20260816").resolve()
    output.mkdir(parents=True, exist_ok=True)
    bundle = collect(project, database)
    findings = dict(DEFAULT_FINDINGS)
    if args.findings and args.findings.is_file():
        external = json.loads(args.findings.read_text("utf-8"))
        findings.update(external.get("findings", external))
    identity = {
        "cases": CASES,
        "result_sha256": sha256_file(project / "bt/random_200_backtest_result_seed_20260816.json"),
        "engine_sha256": sha256_file(project / "backtest_daily_30min.py"),
        "pdf_sha256": sha256_file(project / "108.pdf"),
        "bundle_sha256": sha256_bytes(canonical(bundle)),
    }
    audit_id = "case-" + sha256_bytes(canonical(identity))[:24]
    bundle["audit_id"] = audit_id
    evidence_text = json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    strict_bundle = {
        "schema_version": "typical-chan-strict-bundle/v1",
        "audit_id": audit_id,
        "pdf_sha256": identity["pdf_sha256"],
        "cases": [{
            key: case.get(key) for key in (
                "case_id", "symbol", "entry", "role", "group", "label", "trade", "time_order",
                "daily_bars", "m30_bars", "daily_pens", "m30_pens", "hashes",
            )
        } for case in bundle["cases"]],
        "theory_index": str(Path.home() / ".codex/skills/audit-chan-backtests/references/108_page_index.json"),
        "protocol": str(Path.home() / ".codex/skills/audit-chan-backtests/references/audit_protocol.md"),
        "exclusions": "No old report conclusions, engine center verdicts, or preset classifications are included.",
    }
    strict_text = json.dumps(strict_bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    (output / "evidence.json").write_text(evidence_text, "utf-8")
    (output / "strict_bundle.json").write_text(strict_text, "utf-8")
    manifest = {
        "audit_id": audit_id, **identity, "database": str(database), "database_mode": "read-only",
        "evidence_sha256": sha256_file(output / "evidence.json"),
        "strict_bundle_sha256": sha256_file(output / "strict_bundle.json"),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", "utf-8")
    (output / "report.html").write_text(render(bundle, findings), "utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
