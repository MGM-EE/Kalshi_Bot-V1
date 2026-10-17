# Operating and validating the research bot

Use Python 3.12+ and `uv sync --frozen --extra dev` from the repository root. Generated data and local keys are ignored by Git. Do not commit an `.env`, a PEM, an API key, or a database containing private account information.

## Real-data readiness

1. Confirm read-only REST discovery and official market rule/time/strike/tick interpretation. Unrecognized metadata is retained with skip reasons.
2. Configure a local RSA private-key path and key ID through the host's secure environment. Check authenticated reference index entitlement and snapshot/delta continuity. A successful public API request does not establish these capabilities.
3. Measure host clock uncertainty and configure a conservative bound. The bot does not equate HTTP RTT with clock offset or market-data latency.
4. Confirm series/event fees and account precision separately. An explicitly named paper fee scenario can be explored without claiming fee-exact results.
5. Wait for a complete 31-sample reference lookback and, for ETH/SOL, the opening references. Check reference/book age, feed sequence, backlog, and every decision reason in the dashboard.
6. Begin the instrumentation/validation period before freezing a forward study. Real money and exchange orders are never used by this application.

Optional Coinbase/Kraken collectors store raw public trade and level2/book messages. They tag integrity as unvalidated; checksum reconstruction, validated aggressive-side normalization, latency comparisons, predictive feature ingestion, and venue selection require additional work. Do not treat these raw records as a verified faster reference.

## Reviewed rule changes

The parser deliberately recognizes a narrow set of official market wording. If Kalshi changes wording, inspect the full current rules, actual strike field, timestamp fields, rounding, and price ranges. The collector stores `rules_sha256` with market events. A reviewed override file may resolve rule-window and rounding interpretation:

```json
{
  "markets": {
    "EXACT-OFFICIAL-TICKER": {
      "rules_sha256": "64-character-SHA256-of-the-actual-rules",
      "observation_end": "2026-10-17T16:30:00Z",
      "rounding_digits": 4,
      "evidence": "Reviewed official rule URL and explanation"
    }
  }
}
```

The example is not valid runtime evidence: replace every field from a reviewed contract. Overrides cannot replace the strike, cutoff, direction mapping, fee configuration, or price ranges. A changed hash disables the reviewed interpretation. Never infer a settlement result from an exchange candle or a missing reference tick.

## Persistence and recovery

The database uses WAL, foreign keys, a busy timeout, Decimal strings, and a single shared paper account. Each received event and its order/account transitions commit in one transaction. Pending reservations are transactional and a unique market slot prevents duplicate filled entries after restart. Decision/order IDs link the audit trail; arrival-time execution context stores the effective rules and fees.

Stop the collector before copying a database, or use SQLite's backup API to include committed WAL state. Retain the database, `run.json`, raw directory, and code/config version together. Each directory belongs to a frozen code/configuration hash. Do not modify the database to bypass that check; use a new directory for a new experiment.

On live-process restart, pending entries and exits are canceled. Restored books are invalid and the CF lookback is cleared. New verified snapshots and references must arrive before entries resume. A fatal processing exception stops the process; restart it to rebuild observation state from committed events. No automatic historical fill is attempted across a process or feed gap.

Gzip is a secondary raw audit mirror. If a write is interrupted after an SQLite commit, recover all canonical event payloads from the database (the replay CLI reads SQLite directly). IDs make replay idempotent. A replay portfolio is always separate from its source portfolio.

## Continuous host

The example systemd units in `deploy/` are templates. Set a dedicated OS user, absolute checkout/venv/data paths, and a protected environment file before enabling them on a host you control. Install dependencies before starting services. The collector and dashboard are separate processes, and the dashboard uses SQLite read-only connections. It never runs a second trading engine.

The dashboard binds to loopback by default. If you expose it through a reverse proxy, add authentication and TLS at that proxy; the dashboard does not implement user accounts. No host, domain, public endpoint, automatic deployment, or 24-hour collection service is provisioned by this repository.

## Verification status and limits

Engineering tests exercise baseline boundaries, complementary books, fee rounding, quantity prefixes, shared reservations, partial exits, risk-stop reset/DST, official delayed settlement, stream recovery, causal sampling, transaction rollback, replay, exports, and dashboard read-only behavior. Live smoke checks of public metadata/books are separate from authenticated feed validation. Synthetic fixture results establish software behavior only.

The supplied study calls for seven instrumentation days and a 42-day chronological study with immutable folds. Those periods have not been collected. Sparse/demo reports suppress performance inference. The research module exposes split, labeling, scaling and bootstrap primitives; no calibrated predictive model, robust profitability result, or complete portfolio-reinvestment study is delivered.
