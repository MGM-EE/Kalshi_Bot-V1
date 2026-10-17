# Kalshi research bot — implementation specification

Version: design 1.0 · 17 October 2026 · paper trading only

## Purpose and status

Determine whether the stated 15-minute BTC, ETH and SOL strategy has positive net expectancy under realistic costs and execution. Preserve a fixed baseline and measure changes separately. This document specifies the system; no bot, live connection, latency benchmark or performance result has been produced yet.

Labels: **O** = original/user-specified rule; **P** = proposed operational choice or experiment; **U** = unresolved fact requiring runtime verification. Proposed choices are explicitly versioned, not represented as recovered historical implementation details. The earlier source code has not been inspected; exact old-versus-new regression equivalence remains unverified.

## 1. Verified market facts and data sources

| Purpose | Source and status | Use |
|---|---|---|
| Market discovery, rules, order books, lifecycle | Kalshi Predictions REST and authenticated WebSocket documentation [1–3] | Discover each actual market; retain metadata and reconstruct books from snapshots/deltas. |
| BTC series | KXBTC15M public series response [4] | Verified fifteen-minute frequency, CF Benchmarks reference, quadratic fees and multiplier 1 at retrieval. Event overrides still require checking. |
| ETH and SOL series | KXETH15M / KXSOL15M market pages [5–6] | Example wording identifies ETHUSD_RTI / SOLUSD_RTI and final-minute averaging. Current series fee metadata was not retrieved successfully; do not assume BTC's multiplier applies. |
| Reference price | Kalshi `cfbenchmarks_value` [7] | Approximately one-second reference ticks, trailing average and final-minute accumulation. Authenticated index availability must be tested. |
| Faster reference observations | Kalshi `cfbenchmarks_value_5hz` [8] | Documented BTC, ETH and SOL coverage, up to five updates/second; no averaging fields. Availability is connection-specific. |
| Exchange microstructure | Coinbase Advanced Trade trades/L2; Kraken Spot WebSocket v2 trades/book [9–10] | Candidate direct feeds for momentum, flow, liquidity and lead/lag research. Actual products, access and latency need probing. |
| Fees | Current fee schedule, fee rounding, series metadata and event overrides [11–13] | Snapshot the effective configuration at every decision/fill. Unknown fee model means collect-only for that market. |

Verified examples settle using the final-minute average of the relevant CF Benchmarks index, rather than an arbitrary exchange's final trade. An “at least” contract includes equality [4–6,14]. Do not infer the strike from an exchange candle open. Do not apply BTC's displayed rounding to SOL: the inspected SOL example has a four-decimal strike.

Store separately: trading cutoff `Tc`, observation-window end `To`, expiration, determination and settlement times. Eligibility uses `Tc`, never the ticker's text or presumed payout time. Read each market's exact rules and tick-size structure. Unrecognized wording, unresolved rounding or conflicting timestamps disable that market's entries.

The documented per-second reference feed's quarter-hour accumulation uses `(To−60s, To]`; its ordinary trailing average is a different field [7]. Preserve the official reported result as settlement authority. Local reconstruction is a diagnostic. Missing local ticks do not imply the exchange had missing data or that the contract resolves NO.

## 2. Frozen baseline and exact entry formulas

### Required rules

| Setting | Value | Status |
|---|---|---|
| Entry window | 120 ≤ Tc − decision_time ≤ 600 seconds | O |
| Maximum all-in entry cost | $0.40 per filled contract | O |
| Direction | UP below strike approaching upward; DOWN above strike approaching downward | O |
| Strike distance | At most 0.2% | O; denominator defined below as P |
| BTC momentum | At least $5 toward strike over 30 seconds | O |
| Contract spread | At most $0.06 | O |
| Projected strike reach | Before trading cutoff | O; formula below is P |
| Re-entry | At most one filled entry order per asset-market round; no averaging down | O; order handling below is P |
| Exit | Sell at executable $0.95 or better; otherwise settlement; no position stop-loss | O |

Let `S(t)` be the selected signal price, `K` the official strike and `d=+1` for UP, `d=−1` for DOWN. **P:** use the one-second CF reference as the initial signal source, with exchange feeds collected alongside it. This source choice must be versioned because the old bot's source has not been verified. Missing reference data pauses entries; do not silently switch the baseline to an exchange price.

For a complete 30-second window:

```
T       = Tc − t
gap     = d × (K − S(t))
distance_fraction = abs(K − S(t)) / K
momentum = d × (S(t) − S(t−30s))
speed   = momentum / 30                       # dollars per second
reach_seconds = gap / speed                  # only if gap > 0 and speed > 0
```

Entry requires `gap>0`, `distance_fraction≤0.002`, positive speed, `reach_seconds≤T`, the applicable momentum threshold, the time window, spread and cost gates. Equality to the strike produces no new entry under the “currently losing side” rule. Reach time is a linear extrapolation filter, not a probability estimate or prediction of final-minute average settlement.

**P: sampling.** Evaluate once per second using only events received by that decision time. Select the latest eligible source tick at each sampling boundary, with no future interpolation and a maximum two-second sample age. Require the full 31-point history; store missingness and pause if unavailable. Collect raw data at full rate. A three-second decision cadence is a separate legacy-comparison run.

**P: ETH/SOL extension.** At each market's opening, freeze `r*=5 / BTC_reference_at_open`. For asset `a`, use `H_a=r* × asset_reference_at_open`; BTC remains exactly $5. Require `momentum≥H_a`. This preserves an equal percentage threshold, not equal volatility. Example only: BTC $80,000, ETH $2,500, SOL $100 implies $5, $0.15625 and $0.00625 respectively. Missing opening references block that asset's extended strategy. A volatility-normalized alternative belongs in a separate experiment.

### Objective trend diagnostics, without silently tightening baseline

Fit an OLS line to the same 31 one-second samples: `beta=sum((x−mean(x))×(S−mean(S)))/sum((x−mean(x))²)`, with x in seconds. Record direction-adjusted slope `d×beta`.

Divide the window into six five-second blocks. Persistence is the number of blocks with a strictly positive direction-adjusted return, divided by six; zero returns do not count as positive. Reversal magnitude is `max(d×S_i) − d×S(t)` over that window.

**P: strict-trend experiment:** additionally require `d×beta>0`, at least four positive blocks out of six, and reversal magnitude ≤ `0.5×H_a`. The original baseline only requires positive net 30-second approach and its magnitude threshold. These added gates are not described as original rules.

## 3. Costs, quantity and realistic fills

Use exact decimal arithmetic and current price increments. For desired integer quantity q, walk the selected outcome's ask ladder to obtain fills `(p_j,q_j)`; do not treat midpoint or last trade as a purchase price. Derive YES asks from complementary NO bids, and vice versa, when using the bid-only Kalshi representation [3].

```
purchase_cost(q) = sum(q_j × p_j)
entry_cost(q) = purchase_cost(q) + entry_fees(q)
all_in(q) = entry_cost(q) / q
budget = min($5, 0.02 × unreserved_available_cash)
q* = largest supported integer q satisfying:
     sufficient executable depth, all_in(q) ≤ $0.40,
     entry_cost(q) ≤ budget, and all other entry gates
```

No feasible q means skip. Recompute depth, fees and feasible quantity at simulated arrival. Validate the all-in ceiling and budget for any actual partial-fill prefix, not just the intended full quantity. If a permitted execution cannot satisfy these constraints, model no fill instead of an over-budget entry.

For the documented quadratic taker model, per fill the unrounded fee is `0.07×M×q×p×(1−p)`; maker coefficients differ [11]. Resolve the effective model/multiplier, including event overrides [12–13]. Apply current rounding and per-order accumulator rules: model fee is rounded upward to six decimal dollars, balance change is floored to the account's precision, and rounding overpayment rebates are tracked across that order [12]. Direct/non-direct precision differs. Never round every fill to a cent unconditionally or apply a single fee percentage to gross spend.

**U:** account fee/precision class and live event overrides. Until verified, run explicitly labeled fee scenarios; do not present their paper results as fee-exact. A simple example with M=1, direct precision, q=12 and one fill at $0.38 costs $4.758 after $0.198 fees, or $0.3965/share. At $0.40, fees push all-in cost above the allowed ceiling. The example excludes additional slippage and is not a current executable quote.

**P: execution defaults.** Paper IOC-style marketable limit orders, 500 ms assumed decision-to-arrival delay until measured, full visible depth as an optimistic-capacity bound and 50% visible depth as the primary conservative scenario. Walk the book at arrival using only already received state. Expire an entry arriving outside 120–600 seconds remaining, or with failed mandatory gates. Do not fill across a feed gap. Partial fills form one position and permanently consume that market-round entry slot; no top-up order. A zero-fill attempt can be retried no sooner than three seconds later. Only one pending entry per market, with funds reserved transactionally.

Primary simulated prices come from arrival-time depth, which already includes spread and price movement. Separately replay the earlier assumption of one cent adverse slippage per share per side and delays of 100/250/1,000/2,000 ms. Explicitly label extra haircuts; never add spread twice. Stress scenarios are alternatives, not independent extra trades.

For baseline exit, submit a simulated sell limit of $0.95 only when bids support it. On arrival, fill only volume at $0.95 or better. Partial exits retain the remaining shares under the same policy. No imaginary sale on an ask or last-price touch. No resting maker queue simulation in version 1; if later added, initialize queue ahead, count identifiable qualifying executions and never assume canceled displayed size belonged ahead of us.

## 4. Portfolio ledger and stops

**O:** one shared $250 paper bankroll, $5/2% entry cap, $25 daily realized-loss stop and four-consecutive-loss stop across BTC/ETH/SOL combined.

**P:** daily loss means net realized P&L ≤ −$25, including fees, in America/New_York. Pause new entries through the next local midnight; reset the daily counter and loss streak then. Zero-profit closed positions leave the streak unchanged; a positive completed position resets it; a negative completed position increments it. Partial exits affect daily P&L immediately, while streak updates occur once per fully closed/settled position. Simultaneous completions use source event time then ticker as deterministic tie-breaker.

Stops affect new entries only. Existing positions retain baseline exits and can push losses beyond $25; this is not a guaranteed total-loss cap. Record gross realized losses separately. Settlement proceeds become available only when official settlement is observed, never automatically at cutoff.

One entry per asset per round permits three correlated positions. At $250 cash, sequential maximum costs are approximately $5, $4.90 and $4.802, before quantity rounding. Delayed settlements can leave exposure from older rounds. Record worst-case open loss as remaining unrecouped cost, gross outcome exposure, same-direction exposure and cash committed. Additional portfolio caps are separate proposals, not hidden changes.

## 5. Feed health and latency benchmark

**P:** compare Coinbase and Kraken on the intended deployment host over at least 24 hours, covering quiet and active periods. Report message-age p50/p95/p99, source cadence, disconnects, sequence/checksum failures, book coverage, processing delay and disagreement with CF reference. Do not equate update frequency or ping RTT with end-to-end event latency. Timestamp semantics and cross-server clock uncertainty must be disclosed; use local monotonic time for process durations.

Select the reliable candidate with the best measured freshness; retain the other as a labeled secondary feed. Log product mapping, venue, USD/USDT basis and failover events. After any source change, rebuild the lookback before using it. Public access is not proof of entitlement to every channel; test subscriptions and index lists.

Initial configurable health gates: reference source age ≤2 s, connection heartbeat age ≤5 s, processing backlog ≤250 ms, clock-offset uncertainty ≤100 ms, no sequence/checksum gap, complete trend window and valid uncrossed book. These are P, not empirically optimized thresholds. A quiet book is not stale just because no price changed: distinguish stream continuity and last snapshot verification from last delta time. After >5 s without relevant traffic and without a confirming health mechanism, validate the book through a fresh snapshot subject to rate limits; suppress entries until verified.

Backoff/reconnect, snapshot replay, idempotent ingestion and durable gap records are mandatory. During outages keep recording what is available, flag open positions as unpriced where appropriate, and do not fabricate exits.

## 6. Architecture and data schema

**P: smallest implementation.** One Python async collector/strategy process with a single ledger writer; SQLite in WAL mode for metadata/decisions/orders/state, compressed append-only event segments for raw streams, and Parquet partitions for analytics. A small local dashboard reads the same database and streaming chart data. It never runs the trading engine itself. No microservices or machine-learning dependency in phase 1.

| Record | Required fields |
|---|---|
| run_config | run_id, strategy/execution/fee model versions, config and code hashes, start time, host, source selection, assumptions |
| markets | series/event/ticker, asset, round, K, outcome wording/mapping, rule text/hash, tick structure, Tc/To/expiration/settlement, source, fee metadata |
| raw_events | event ID, venue/channel/instrument, connection epoch, source/sending/receipt/monotonic/processing times, sequence, raw payload, checksum, quality flags |
| book_checkpoints | instrument, checkpoint time, both ladders, stream cursor and raw-event offset for reconstruction |
| decisions | decision ID, market, side, received-event watermark, config hash, every feature, every gate, reason codes, candidate quantity/cost, account state |
| paper_orders/fills | idempotency key, intended/arrival time, type/limit, quantity, fill price, fees/components, depth used, fill scenario, partial/cancel reason |
| positions/ledger | entry linkage, open quantity, allocated cost, cash movements, realized P&L, settlement status, streak, daily stop, reserved cash |
| trajectories | entry ID, elapsed time, bid/ask/mid, liquidation quantity/value, executable availability, features, observed/missing flag |
| experiments/labels | baseline entry ID, exit-policy version, horizon/target, outcome, censoring reason, train/validation/test split |
| feed_health | connection/gap events, freshness, lag, clock uncertainty, last verified state, recovery actions |

Store every raw event and every one-second decision for every tracked market, including outside entry eligibility. Record all failed gates, not just the first reason. Snapshot books at startup/resync and periodically; preserve deltas between snapshots. Continue post-exit observations until cutoff and official settlement.

Transactional uniqueness on `(run_id, market_ticker, entry_slot)` prevents duplicate filled entries after restart. Commit cash, position and fill in one transaction. Save replay cursor with state. Replay reconstructs identical decisions, fills and balances from receipt order and configuration. No order-submission client or live-order path is included. Credentials stay in local environment/secret storage, never in data exports or chat.

## 7. Post-entry analytics

Let `L_q(t)` be proceeds from walking executable bids for the entry quantity, minus exit fees, and let `C0` be actual all-in entry cost. Define `V(t)=L_q(t)−C0`. Missing sufficient depth yields “full liquidation unavailable,” not a midpoint sale. Also report executable quantity and partial proceeds. For conservative account valuation, an unsellable remainder may be marked zero with an explicit valuation flag; this is not a simulated fill.

Measure MFE=`max V(t)` and MAE=`min V(t)` over entry-to-cutoff (and separately over actual holding time); report dollar values and `100×V(t)/q` cents/share. For fixed liquidation levels 45/50/60/70/80/90/95 cents, record first executable full-position touch, delayed simulated fill, elapsed time and net P&L. Settlement payout is a separate outcome, not an executable pre-close quote.

Build equal-trade-weighted mean, median and 10th/25th/75th/90th percentile paths at fixed elapsed horizons 5/15/30/60/120 seconds and at fixed times to cutoff. Report time-weighted average within each trajectory separately; do not overweight active markets by counting every tick as an independent observation. At every horizon show eligible trades, fully observed trades and missing/censored cases. No post-cutoff forward filling or survivor-only paths.

Report fraction of observable seconds above/below net break-even, settlement win rate, net sell-versus-hold difference, peak-to-realized giveback and paired exit-policy differences. Unknown settlement remains pending. Split by asset, direction, entry-cost band, time left, strike distance, volatility and depth; sparse groups are descriptive only.

## 8. Signal research and exits after the baseline works

Primary predictive target: whether full-position net liquidation value improves by at least 5 cents/share within the next 30 seconds. Secondary targets: 60/120-second improvement, 10-cent adverse movement first, and official settlement outcome. Only evaluate horizons that fit before cutoff; distinguish negative outcomes from censored data.

Candidate features: 5/15/30-second returns, OLS slope/change, trailing volatility, signed aggressive notional, near-price depth imbalance `(bid_depth−ask_depth)/(bid_depth+ask_depth)`, executed-trade size percentile, reference/exchange basis, and lagged BTC/ETH/SOL returns. Define depth bands in basis points and fit scaling on training data only.

**P: unusual-flow diagnostic:** trade notional above the trailing hour's 99.5th percentile and at least five times its median, requiring 1,000 prior trades; otherwise mark unavailable. Record aggressive side only if venue semantics support it. Separate executed trade flow from book withdrawal; aggregated L2 changes do not uniquely identify cancellations, hidden orders or whale intent. On-chain transfers are not assumed to be exchange trades.

Pre-register the following small exit families, each using the same baseline fills and quantities:

| Family | Initial research definition |
|---|---|
| Fixed targets | Executable gross 50/60/70/80/90/95 cents; otherwise settlement |
| Trailing protection | Arm once net liquidation profit reaches 10 cents/share; attempt exit after a 5-cent/share fall from the observed peak |
| Time exit | Attempt liquidation at 60 seconds remaining; residual quantity settles |
| Reversal | Attempt exit after direction-adjusted five-second return is negative at three consecutive valid one-second decisions |
| Unusual-flow risk | Attempt exit when qualifying adverse executed flow coincides with negative five-second momentum; flag as an experimental early-loss exit |
| Volatility/liquidity risk | Research only: trailing 30-second volatility above training 90th percentile plus executable bid depth below training 10th percentile, persistent for three decisions |

All reactive policies incur delay, fees and depth constraints. Exit triggers are not guaranteed exit prices. When their features are unavailable, experimental policies fall back to the original 95-cent/settlement policy and log the fallback. Do not change baseline entry rules in an exit-only comparison.

For an eventual calibrated model, `EV_hold=q×Pr(selected outcome wins)−L_q(t)` is the simple settlement-versus-sell comparison; entry cost cancels. A path-dependent exit policy needs a separate expected future-net-proceeds model. Estimate calibration/uncertainty out of sample; an arbitrary combined score is not a probability. Version 1 displays no invented probability or “AI confidence.”

Run paired per-entry exit comparisons first, with no reinvestment of released cash. Then run separate full-portfolio simulations to quantify capital reuse and risk-stop effects. Keeping these separate prevents apparent exit improvements caused only by different subsequent opportunities.

## 9. Validation and acceptance criteria

Collect forward data unless a historical source provides timestamped reconstructible depth. Candles and trades alone cannot establish execution quality. Do not claim otherwise.

Proposed collection plan: 7-day instrumentation/fee-validation period excluded from performance claims, followed by 42 chronological days split 21 training / 7 validation / 14 untouched test. Extend by complete weeks if data are insufficient. These periods are operational starting points, not proof of statistical adequacy. Freeze parameters before test; do not tune on test outcomes.

Group BTC/ETH/SOL sharing a quarter-hour into the same fold. Purge observations whose label windows cross boundaries, apply an additional 15-minute embargo, and keep pending settlement cash in each causal portfolio replay. Use day-block bootstrap confidence intervals for expectancy and paired policy differences; report sensitivity to block size and clustered dependence. Pre-register the primary outcome and all tested variants; disclose every trial and use a multiple-comparison correction for secondary selections.

Report net dollars/trade, net cents/share, net P&L/day, fees/slippage, fill rate, maximum drawdown, loss streaks, capital-time exposure, concentration by asset/day, profit factor with loss count, and sample/coverage counts. Include deployment/data costs separately from trading P&L. A positive estimate with a confidence interval spanning zero is inconclusive. Results concentrated in a few rounds or disappearing under plausible execution stress are not a robust edge.

Required engineering tests before paper results are trusted:

- Exact 120/600-second boundaries, late arrival, timezone/day reset and DST.
- YES/NO complementary books, price precision, empty depth and cross-side symmetry.
- Fee rounding/accumulator fixtures, event overrides and partial-fill cost ceilings.
- Concurrent reservations, delayed settlement, partial exits and stops across assets.
- Snapshot/delta recovery, duplicate/out-of-order events and missing data.
- Crash during ledger transaction, restart without duplicate entries, deterministic replay.
- Immutable test split and feature availability by receipt time.

## 10. Dashboard and delivery sequence

Dashboard: fixed PAPER banner; cash/equity/realized P&L; open/pending settlement positions; three asset cards with eligibility and every skip reason; price/reference/strike chart; executable contract bid/ask and own fill markers; feed-health timestamps; complete paginated trade history; trajectory distributions; baseline-versus-experiment tables; downloadable filtered data. Display settlement-window progress separately from ordinary price crossing. Show missing data and stale charts explicitly.

Build sequence: (1) market/fee verification and durable collectors; (2) replayable baseline/ledger and engineering tests; (3) dashboard and post-entry statistics; (4) paired exit experiments; (5) predictive research with held-out evaluation. A continuously running collector needs a user-controlled always-on host; an open chat is not deployment.

Unresolved before a faithful run: actual host and connectivity; Kalshi feed authentication/availability; current ETH/SOL and event fee details; account rounding class; exact old signal source/code if historical equivalence is required. Proposed defaults above cover retry semantics, shared risk stops, reset timezone, trend formalization and normalized asset thresholds. No assumption here authorizes live trading.

## Sources checked 17 October 2026

1. https://docs.kalshi.com/getting_started/api_environments
2. https://docs.kalshi.com/websockets/orderbook-updates
3. https://docs.kalshi.com/getting_started/orderbook_responses
4. https://external-api.kalshi.com/trade-api/v2/series/KXBTC15M
5. https://kalshi.com/markets/kx/test/kxeth15m-26oct111630
6. https://kalshi.com/markets/kx/test/kxsol15m-26oct152000
7. https://docs.kalshi.com/websockets/cfbenchmarks-value
8. https://docs.kalshi.com/websockets/cfbenchmarks-value-5hz
9. https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-overview
10. https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/book
11. https://kalshi.com/docs/kalshi-fee-schedule.pdf
12. https://docs.kalshi.com/getting_started/fee_rounding
13. https://docs.kalshi.com/api-reference/events/get-event-fee-changes
14. https://assets.kalshi.com/contract_terms/CRYPTO.pdf
15. https://docs.kalshi.com/api-reference/market/get-series
16. https://docs.kalshi.com/api-reference/market/get-market

Documentation verifies available interfaces, not successful authentication, present market liquidity, measured latency or profitable performance. All must be established during implementation and forward collection.
