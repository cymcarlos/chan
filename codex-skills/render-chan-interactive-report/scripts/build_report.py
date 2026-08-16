#!/usr/bin/env python3
"""Build a standalone interactive Chan evidence report for one project trade."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any


DEFAULT_PROJECT = Path(r"D:\code\chan\backtest_daily_30min")
LEGACY_TOOL = Path("tools/build_002486_interactive_audit.py")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a self-contained daily/30-minute Chan audit HTML."
    )
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--symbol", help="Exchange-qualified code, e.g. 002486.SZ")
    parser.add_argument("--entry-date", help="Trade entry date: YYYY-MM-DD")
    parser.add_argument("--signal-date", help="Source first-buy date; inferred from trace when omitted")
    parser.add_argument(
        "--evidence", type=Path, help="Render an existing evidence.json without replaying the engine"
    )
    parser.add_argument("--backtest-start", help="Defaults to result metadata, then 20200101")
    parser.add_argument(
        "--backtest-end", help="Defaults to result metadata, then the latest daily bar in kline.db"
    )
    parser.add_argument("--output", type=Path, help="Output directory")
    parser.add_argument("--result", type=Path, help="Optional originating result JSON for the manifest")
    parser.add_argument(
        "--hash-full-database",
        action="store_true",
        help="Also hash the full kline.db; slower than the default case-slice hash",
    )
    parser.add_argument("--classification")
    parser.add_argument("--verdict-title")
    parser.add_argument("--verdict-summary")
    parser.add_argument("--verdict-tag")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def iso_date(value: Any) -> str:
    text = str(value or "")
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    raise ValueError(f"not a date: {value!r}")


def engine_date(value: str) -> str:
    return iso_date(value).replace("-", "")


def open_readonly(database: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def latest_daily_date(database: Path) -> str:
    with open_readonly(database) as conn:
        value = conn.execute("SELECT MAX(date) FROM daily_kline").fetchone()[0]
    return engine_date(str(value))


def result_bounds(project: Path, result: Path | None) -> tuple[str | None, str | None]:
    if result is None:
        return None, None
    resolved = result if result.is_absolute() else project / result
    payload = json.loads(resolved.read_text("utf-8"))
    metadata = payload.get("metadata") or {}
    return metadata.get("sdt"), metadata.get("edt_argument") or metadata.get("effective_edt")


def load_project_builder(project: Path) -> ModuleType:
    tool = project / LEGACY_TOOL
    if not tool.is_file():
        raise FileNotFoundError(
            f"interactive template builder is missing: {tool}. "
            "Restore tools/build_002486_interactive_audit.py first."
        )
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))
    spec = importlib.util.spec_from_file_location("chan_interactive_template", tool)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import template builder: {tool}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PROJECT = project
    return module


def find_trade(replay: dict[str, Any], entry_date: str) -> dict[str, Any]:
    target = iso_date(entry_date)
    matches = [row for row in replay.get("trades", []) if iso_date(row.get("entry")) == target]
    if len(matches) != 1:
        available = [str(row.get("entry")) for row in replay.get("trades", [])]
        raise RuntimeError(
            f"expected one trade entered on {target}, found {len(matches)}; entries={available}. "
            "The current engine may not match the originating result; use frozen --evidence or the matching build."
        )
    return matches[0]


def infer_signal_date(
    module: ModuleType, symbol: str, entry_date: str, start: str, end: str, database: Path
) -> str:
    module.engine.clear_all()
    module.engine.clear_daily_cache()
    module.engine.clear_30min_cache()
    module.engine._FRESH_B1_CACHE.clear()
    with open_readonly(database) as conn:
        replay = module.engine.backtest_one(symbol, start, end, conn=conn, audit_trace=True)
    trade = find_trade(replay, entry_date)
    audit = trade.get("audit") or {}
    candidates = {
        row.get("candidate_id"): row for row in replay.get("audit_trace", {}).get("candidates", [])
    }
    source_id = audit.get("source_b1_id") or audit.get("candidate_id")
    source = candidates.get(source_id) or {}
    value = source.get("signal_at") or source.get("occur_at") or audit.get("occur_at")
    if not value:
        raise RuntimeError("cannot infer source first-buy date; pass --signal-date explicitly")
    return iso_date(value)


def add_days(value: str, days: int) -> str:
    return (datetime.strptime(iso_date(value), "%Y-%m-%d") + timedelta(days=days)).strftime(
        "%Y-%m-%d"
    )


def prepare_evidence(
    evidence: dict[str, Any], args: argparse.Namespace, signal_date: str
) -> dict[str, Any]:
    pairs = evidence.get("pair_diagnostics", [])
    selected = next((row for row in pairs if row.get("id") == evidence.get("selected_pair_id")), None)
    local = next((row for row in pairs if row.get("id") == evidence.get("local_pair_id")), None)
    if selected is None or local is None:
        raise RuntimeError("selected or latest local center pair is missing from captured evidence")

    evidence["schema"] = "render-chan-interactive-report/v1"
    evidence["symbol"] = args.symbol
    evidence["generated_at"] = datetime.now().astimezone().isoformat()
    prior_verdict = evidence.get("verdict") or {}
    evidence["verdict"] = {
        "classification": args.classification
        or prior_verdict.get("classification")
        or "REVIEW_REQUIRED",
        "tag": args.verdict_tag or prior_verdict.get("tag") or "交互证据",
        "title": args.verdict_title
        or prior_verdict.get("title")
        or "结构证据已生成，结论待审查",
        "summary": args.verdict_summary
        or prior_verdict.get("summary")
        or "本页展示引擎冻结结构与候选因果链；严格一买/二买结论需结合审计证据裁决。",
    }
    ratio = local.get("force_ratio")
    selected_years = (
        f"{selected['center1_dates'][0][:4]}—{selected['center2_dates'][1][:4]}"
    )
    pnl = evidence.get("trade", {}).get("pnl_pct")
    evidence["metrics"] = [
        {"label": "引擎实际采用中枢", "value": f"{selected_years}结构"},
        {
            "label": "最近中枢对C/A力度比",
            "value": "—" if ratio is None else f"{float(ratio):.3f}",
            "tone": "bad" if ratio is None or float(ratio) >= 0.9 else "good",
        },
        {"label": "项目背驰门槛", "value": "< 0.900"},
        {
            "label": "交易结果",
            "value": "—" if pnl is None else f"{float(pnl):+.2f}%",
            "tone": "bad" if pnl is not None and float(pnl) <= 0 else "good",
        },
    ]
    stale = selected.get("id") != local.get("id")
    local_reason = local.get("reason") or "未记录"
    evidence["explanation"] = {
        "title": "引擎采用结构与最近结构的差异",
        "text": (
            f"引擎采用{selected['center1_dates'][0][:10]}至"
            f"{selected['center2_dates'][1][:10]}的中枢对。"
            f"信号前最近中枢对为{local['center1_dates'][0][:10]}至"
            f"{local['center2_dates'][1][:10]}，其判定为：{local_reason}。"
            + ("两者不是同一对，应重点审查扫描顺序、连续性和去重。" if stale else "两者一致。")
        ),
    }

    trade = evidence.get("trade", {})
    audit = evidence.get("trade_audit", {})
    evidence["markers_daily"] = [
        {"at": signal_date, "label": "源一买候选", "kind": "signal"},
        {"at": args.entry_date, "label": f"{trade.get('name', '买点')}入场", "kind": "entry"},
        {"at": trade.get("exit"), "label": "离场", "kind": "exit"},
    ]
    markers_m30 = [
        {"at": audit.get("occur_at"), "label": "买点发生", "kind": "signal"},
        {"at": audit.get("decision_at"), "label": "入场决策", "kind": "decision"},
        {"at": audit.get("fill_at"), "label": "入场成交", "kind": "entry"},
    ]
    for event in evidence.get("events", []):
        name = str(event.get("event") or "")
        if "invalid" in name.lower() or "失效" in name:
            markers_m30.append(
                {"at": event.get("at"), "label": "候选失效", "kind": "invalid"}
            )
    markers_m30.append(
        {"at": audit.get("exit_fill_at") or trade.get("exit"), "label": "离场成交", "kind": "exit"}
    )
    seen: set[tuple[Any, Any, Any]] = set()
    evidence["markers_m30"] = [
        row
        for row in markers_m30
        if row.get("at") and not (tuple(row.values()) in seen or seen.add(tuple(row.values())))
    ]
    evidence["ranges"] = {
        "daily": {
            "context": [selected["center1_dates"][0][:10], add_days(signal_date, 90)],
            "local": [local["center1_dates"][0][:10], add_days(signal_date, 45)],
            "signal": [add_days(signal_date, -60), add_days(signal_date, 30)],
        },
        "m30": {
            "setup": [add_days(signal_date, -30), add_days(args.entry_date, 3)],
            "position": [add_days(args.entry_date, -3), add_days(trade.get("exit") or args.entry_date, 3)],
        },
    }
    return evidence


def generic_template(template: str, symbol: str) -> str:
    template = template.replace("002486.SZ", symbol)
    template = template.replace(
        '<div><span class="badge">CODE_DEFECT</span><span class="badge">假一买源链</span></div>',
        '<div><span class="badge" id="verdict-classification"></span>'
        '<span class="badge" id="verdict-tag"></span></div>',
    )
    old_metrics = '<div class="metrics"><div class="metric"><span>实际采用中枢</span><strong>2020—2021旧结构</strong></div><div class="metric"><span>最近两中枢C/A力度比</span><strong class="status-bad">2.436</strong></div><div class="metric"><span>背驰门槛</span><strong>&lt; 0.900</strong></div><div class="metric"><span>交易结果</span><strong class="status-bad">-14.84%</strong></div></div>'
    template = template.replace(old_metrics, '<div class="metrics" id="metric-rows"></div>')
    old_explain = '<section class="section"><h2>为什么这是陈旧中枢假一买</h2><div class="explain"><b>程序实际绑定：</b><span id="selected-pair-text"></span><br><b>当时最近结构：</b><span id="local-pair-text"></span><br><b>关键差异：</b>最近两中枢虽然下移且C段创新低，但C段平均下跌力度是A段的2.436倍，不是背驰。旧候选因为扫描顺序和 <span class="mono">(买点类型, 信号日期)</span> 去重键先占位，成为二买的源一买。</div></section>'
    new_explain = '<section class="section"><h2 id="explanation-title"></h2><div class="explain"><span id="explanation-text"></span><br><b>引擎实际绑定：</b><span id="selected-pair-text"></span><br><b>信号前最近结构：</b><span id="local-pair-text"></span></div></section>'
    template = template.replace(old_explain, new_explain)
    old_init = "document.getElementById('verdict-title').textContent=DATA.verdict.title;document.getElementById('verdict-summary').textContent=DATA.verdict.summary;"
    new_init = "document.getElementById('verdict-classification').textContent=DATA.verdict.classification;document.getElementById('verdict-tag').textContent=DATA.verdict.tag;document.getElementById('verdict-title').textContent=DATA.verdict.title;document.getElementById('verdict-summary').textContent=DATA.verdict.summary;document.getElementById('metric-rows').innerHTML=DATA.metrics.map(m=>`<div class=\"metric\"><span>${esc(m.label)}</span><strong class=\"${m.tone==='bad'?'status-bad':m.tone==='good'?'status-good':''}\">${esc(m.value)}</strong></div>`).join('');document.getElementById('explanation-title').textContent=DATA.explanation.title;document.getElementById('explanation-text').textContent=DATA.explanation.text;"
    template = template.replace(old_init, new_init)
    template = template.replace(
        "const dailyRanges={context:['2020-08-01','2024-08-31'],local:['2023-02-01','2024-07-31'],signal:['2024-04-15','2024-06-30']};\nconst m30Ranges={setup:['2024-05-20 10:00:00','2024-06-20 15:00:00'],position:['2024-06-17 10:00:00','2024-06-25 15:00:00']};",
        "const dailyRanges=DATA.ranges.daily;\nconst m30Ranges=DATA.ranges.m30;",
    )
    template = template.replace("实际获胜", "实际采用")
    return template


def write_outputs(
    module: ModuleType,
    evidence: dict[str, Any],
    output: Path,
    project: Path,
    result: Path | None,
    source_evidence: Path | None,
    hash_full_database: bool,
) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    evidence_path = output / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n", "utf-8"
    )
    payload = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    report = generic_template(module.HTML, evidence["symbol"]).replace("__DATA__", payload)
    if "__DATA__" in report or report.count('id="audit-data"') != 1:
        raise RuntimeError("HTML payload injection failed")
    if "http://" in report or "https://" in report:
        raise RuntimeError("standalone report unexpectedly contains an external URL")
    report_path = output / "report.html"
    report_path.write_text(report, "utf-8")
    database = project / "kline.db"
    database_stat = database.stat()
    manifest: dict[str, Any] = {
        "schema": "render-chan-interactive-report-manifest/v1",
        "symbol": evidence["symbol"],
        "database_mode": "read-only",
        "database_size_bytes": database_stat.st_size,
        "database_mtime_ns": database_stat.st_mtime_ns,
        "data_slice_sha256": sha256_json(
            {"symbol": evidence["symbol"], "daily": evidence.get("daily"), "m30": evidence.get("m30")}
        ),
        "engine_sha256": sha256_file(project / "backtest_daily_30min.py"),
        "template_sha256": sha256_file(project / LEGACY_TOOL),
        "skill_script_sha256": sha256_file(Path(__file__)),
        "evidence_sha256": sha256_file(evidence_path),
        "report_sha256": sha256_file(report_path),
    }
    if hash_full_database:
        manifest["database_sha256"] = sha256_file(database)
    if result:
        resolved = result if result.is_absolute() else project / result
        manifest["result_path"] = str(resolved)
        manifest["result_sha256"] = sha256_file(resolved)
    if source_evidence:
        manifest["source_evidence_path"] = str(source_evidence)
        manifest["source_evidence_sha256"] = sha256_file(source_evidence)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", "utf-8"
    )
    return report_path


def main() -> int:
    args = arguments()
    project = args.project.resolve()
    database = project / "kline.db"
    if not database.is_file():
        raise FileNotFoundError(database)
    module = load_project_builder(project)
    source_evidence: Path | None = None
    if args.evidence:
        source_evidence = args.evidence if args.evidence.is_absolute() else project / args.evidence
        evidence = json.loads(source_evidence.read_text("utf-8"))
        args.symbol = args.symbol or evidence.get("symbol")
        args.entry_date = args.entry_date or evidence.get("trade", {}).get("entry")
        source = evidence.get("source") or {}
        signal_value = (
            args.signal_date
            or source.get("signal_at")
            or source.get("occur_at")
            or next(
                (
                    row.get("at")
                    for row in evidence.get("markers_daily", [])
                    if row.get("kind") == "signal"
                ),
                None,
            )
        )
        if not signal_value:
            raise RuntimeError("source evidence does not contain a signal date")
        signal = iso_date(signal_value)
    else:
        if not args.symbol or not args.entry_date:
            raise RuntimeError("--symbol and --entry-date are required unless --evidence is supplied")
        result_start, result_end = result_bounds(project, args.result)
        start = engine_date(args.backtest_start or result_start or "20200101")
        end = engine_date(args.backtest_end or result_end) if (args.backtest_end or result_end) else latest_daily_date(database)
        signal = iso_date(args.signal_date) if args.signal_date else infer_signal_date(
            module, args.symbol, args.entry_date, start, end, database
        )
        module.SYMBOL = args.symbol
        module.SIGNAL_DATE = signal
        module.ENTRY_DATE = iso_date(args.entry_date)
        original_backtest = module.engine.backtest_one

        def bounded_backtest(code: str, _start: str, _end: str, *extra: Any, **kwargs: Any):
            return original_backtest(code, start, end, *extra, **kwargs)

        module.engine.backtest_one = bounded_backtest
        try:
            evidence = module.collect()
        finally:
            module.engine.backtest_one = original_backtest
    if not args.symbol or not args.entry_date:
        raise RuntimeError("symbol or entry date is missing from input evidence")
    evidence = prepare_evidence(evidence, args, signal)
    output = args.output
    if output is None:
        stem = args.symbol.replace(".", "_")
        output = project / "bt" / "audits" / f"{stem}_interactive_{signal.replace('-', '')}"
    elif not output.is_absolute():
        output = project / output
    report_path = write_outputs(
        module,
        evidence,
        output,
        project,
        args.result,
        source_evidence,
        args.hash_full_database,
    )
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
