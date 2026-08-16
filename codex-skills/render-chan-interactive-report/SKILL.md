---
name: render-chan-interactive-report
description: Generate a standalone interactive HTML evidence report for trades in D:\\code\\chan\\backtest_daily_30min. Use when asked to visualize a stock's daily and 30-minute K-lines, pens, centers, center pairs, MACD, 一买/二买 source chain, candidate lifecycle, entry/exit events, or to turn a Chan backtest case into a zoomable auditable HTML report.
---

# Render Chan Interactive Report

Create a read-only, standalone HTML evidence report for one trade. Keep rendering separate from the Chan verdict: display the supplied or previously audited conclusion, but do not infer a strict 一买 merely because price later rose.

## Build a case report

For an audited or already frozen case, run from the project directory:

```powershell
py -3 C:\Users\Administrator\.codex\skills\render-chan-interactive-report\scripts\build_report.py `
  --project D:\code\chan\backtest_daily_30min `
  --evidence bt\audits\<case>\evidence.json
```

To build a new case with the current engine:

```powershell
py -3 C:\Users\Administrator\.codex\skills\render-chan-interactive-report\scripts\build_report.py `
  --project D:\code\chan\backtest_daily_30min `
  --symbol <symbol> `
  --entry-date <YYYY-MM-DD> `
  --result <matching-result.json>
```

The script infers the source 一买 date through `source_b1_id` and takes replay bounds from result metadata when `--result` is supplied. Supply `--signal-date YYYY-MM-DD` only when inference is unavailable or when reviewing a specific source candidate. Use `--classification`, `--verdict-title`, and `--verdict-summary` to carry a verified audit conclusion into the page. If the current engine cannot reproduce an old result, stop replay and render its frozen evidence or restore the matching code build.

The default output is:

```text
bt/audits/<symbol>_interactive_<signal-date>/
├── report.html
├── evidence.json
└── manifest.json
```

## Required workflow

1. Confirm the requested symbol and trade entry date from the result or audit evidence. Prefer supplying the originating result so future bars cannot silently change the historical replay boundary.
2. Prefer `--evidence` for an already frozen case. Otherwise replay with `--result` or explicit bounds; keep `kline.db` read-only.
3. Check the printed report path and the three generated files.
4. Open `report.html` for the user and report the conclusion separately in plain language.
5. If the page says `REVIEW_REQUIRED`, do not upgrade it from visual appearance alone. Use `$audit-chan-backtests` for a formal engine/strict-Chan verdict.

## Report guarantees

- Embed all data, CSS, and JavaScript; use no network resources.
- Show daily and 30-minute K-lines, MACD, pens, centers, center-pair diagnostics, signal/entry/exit markers, source candidate, child candidate, and lifecycle events when present.
- Support wheel zoom, drag pan, hover details, layer toggles, range shortcuts, and click-to-focus tables.
- Draw every center only over its real start/end interval.
- Preserve exact evidence in `evidence.json`; use `manifest.json` for the relevant database slice, engine, template, report, and evidence hashes. Use `--hash-full-database` only when a full `kline.db` hash is required.
- Never treat a daily-pen center as strict Chan truth or use later price action to validate an earlier buy point.

Read [references/report_contract.md](references/report_contract.md) when adapting the script, changing page fields, or consuming its JSON programmatically.
