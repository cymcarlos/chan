# Report contract

## Inputs

- `project`: `D:\code\chan\backtest_daily_30min` unless explicitly overridden.
- `symbol`: exchange-qualified code such as `002486.SZ`.
- `entry_date`: the trade entry date in `YYYY-MM-DD`.
- `signal_date`: optional source 一买 date; otherwise infer it from the trade audit trace.
- `evidence`: optional frozen evidence JSON. When present, render without engine replay and preserve its verdict unless overrides are supplied.
- `backtest_start` and `backtest_end`: replay bounds. Prefer the originating result metadata; only fall back to the latest daily bar when no result boundary exists.
- Optional verdict fields must come from an audit or be clearly labeled as a provisional engine diagnostic.

## Evidence semantics

- `daily_pens` and `centers` are engine-proxy structures frozen at the signal date.
- `pair_diagnostics.generated` means the current engine predicate passed; it is not a strict-Chan verdict.
- `selected_pair_id` identifies the pair that won the engine's scan/deduplication order.
- `local_pair_id` identifies the latest adjacent downward-center pair visible at the signal date.
- `force_ratio` is the engine MACD average-force ratio; the project threshold is `0.9`.
- `source` is the source 一买 candidate and `child` is the traded candidate, normally 二买.
- Missing trace fields remain absent or `null`; never synthesize them.

## Output checks

- `report.html` contains exactly one embedded JSON payload with id `audit-data`.
- `evidence.json` parses and contains the same case payload.
- `manifest.json` hashes `report.html`, `evidence.json`, and the symbol's embedded daily/30-minute data slice; it also records read-only database metadata. Full database hashing is optional because it can be slow.
- The HTML contains no `http://` or `https://` resource references.
- Center rectangles use each center's own start/end timestamps.

## Interpretation guardrails

- A profitable rebound does not prove the preceding signal was a valid 一买.
- A losing 二买 does not prove its source 一买 was false.
- Use the engine view to explain what code selected; use `$audit-chan-backtests` for strict level, completed sub-level trend types, center continuity, divergence, and confirmation timing.
