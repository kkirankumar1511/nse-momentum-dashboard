# NSE Momentum Dashboard — QA Test Plan

**Environment:** Sandbox (`scripts/run_sandbox.py`, port 8502) — real business
logic (screener.py, live_rebalance.py, backtest.py, state_db.py) running
against a fully mocked `kite_client` (synthetic candles/holdings/orders/GTTs,
`scripts/sandbox_mock_kite.py`) and a throwaway `cache/state_sandbox.db`.
**Zero real Kite API calls, zero real orders, zero risk to production data.**

Login: `Admin` / `Admin` (sandbox DB default).

Legend: ✅ Pass · ❌ Fail (defect logged) · ⚠️ Pass with note · ⬜ Not yet run

---

## 1. Login & Auth
| ID | Case | Steps | Expected |
|----|------|-------|----------|
| AUTH-1 | Valid login | Enter Admin/Admin, submit | Redirects to Overview |
| AUTH-2 | Invalid login | Wrong password | Error shown, stays on login |
| AUTH-3 | Remember-me cookie | Set cookie via `create_remember_token`, load app | Skips login, straight to Overview |
| AUTH-4 | Logout | Click Logout | Returns to login page, cookie cleared |

## 2. Overview
| ID | Case | Expected |
|----|------|----------|
| OV-1 | KPI strip renders | Total capital/Invested/Holdings/Cash/Total P&L/XIRR/Alpha/Max drawdown all show non-error values |
| OV-2 | Equity chart | Renders once ≥2 snapshots logged; range selector (1W/1M/.../All) changes the plotted window |
| OV-3 | Positions card | Lists open holdings by momentum rank, P&L colored red/green |
| OV-4 | Capital allocation per stock | Weight bars + drift vs target sum sensibly |
| OV-5 | Asset allocation by sector | Donut chart + concentration warning when one sector > threshold |
| OV-6 | Funds breakdown expander | Opens, shows available/utilised tables from margins API |
| OV-7 | Topbar chips | Pending-actions / last-scan / slots / universe chips all populate |
| OV-8 | Sync button | Click reruns the page without error |
| OV-9 | Mobile layout | At ≤600px viewport, chips wrap onto 2 lines, all 4 visible, no horizontal overflow |

## 3. Live Rebalance
| ID | Case | Expected |
|----|------|----------|
| LR-1 | Run today's scan | Click → spinner → proposal renders (sells/buys/top-ups/stop-updates) with no exception |
| LR-2 | Proposed sells table | Symbol/LTP/P&L/reason shown, keep-zone subtitle correct |
| LR-3 | Proposed buys table | Rank column present, sized off real available cash |
| LR-4 | Execute sells | Tick confirm, click Execute → mock `place_order` called, `close_trade` recorded, row disappears/updates |
| LR-5 | Execute buys | Same, mock order + `record_new_position`/`record_trade_entry` fire |
| LR-6 | Apply stop updates | Mock `modify_gtt_trigger` called, `current_stop` updates in state_db |
| LR-7 | What-if expander | Opens at end of page, computes without error |

## 4. Positions & Trade
| ID | Case | Expected |
|----|------|----------|
| PT-1 | Holdings (CNC) table | Shows seeded fake holdings with Invested/Current Capital columns |
| PT-2 | Auto-refresh pills | Off/10s/1m/etc. selectable, "Last refreshed" ticks when non-Off |
| PT-3 | Today's orders & positions | Renders (empty state fine pre-order) |
| PT-4 | GTT / Stop-Loss Management | Lists active GTTs (post LR-6/PT-6) with correct trigger price |
| PT-5 | Place an order — BUY | Fill symbol/qty, confirm, Execute → mock `place_order` + optional `place_gtt_stoploss` fire |
| PT-6 | Place an order — SELL/square-off | Toggle square-off, Execute → mock `square_off_position` fires, holding qty→0 |
| PT-7 | Segmented controls | BUY/SELL and MARKET/LIMIT render as pill toggles, not radio buttons |

## 5. Screener
| ID | Case | Expected |
|----|------|----------|
| SC-1 | Run screen | Click → candidates + full universe tables populate from fake candles |
| SC-2 | Candidates passing all gates | Paginated 10/row, info icon tooltip present |
| SC-3 | Full universe | Gate-check ✓/✗ badges immediately after Symbol, paginated |
| SC-4 | Chart a symbol | Selecting a symbol renders a candlestick/line chart without error |

## 6. Fundamentals
| ID | Case | Expected |
|----|------|----------|
| FN-1 | Ranked table (cached) | Loads from existing `fno_value_scores.pkl` cache, paginated |
| FN-2 | Score breakdown + Sub-metric buckets | Always-visible side-by-side cards, symbol dropdown changes both |
| FN-3 | Sector filter | Narrows Ranked table correctly |
| FN-4 | Rows with incomplete data | Expander lists any null-score rows |

*(FN-1..4 use the existing real cached fundamentals data — no live NSE fetch
triggered, per plan; a live "Run value score scan" is NOT executed in this
pass to avoid unnecessary real network calls to NSE.)*

## 7. Backtest
| ID | Case | Expected |
|----|------|----------|
| BT-1 | Run configuration card | Segmented Date range, Years slider, Capital/Max positions inputs all present, aligned |
| BT-2 | Strategy parameters (3 groups) | Trade management / Technical indicator / Scanner param sections render, inline checkbox+input pairs |
| BT-3 | Run backtest | Click → mock candle fetch → simulation completes → equity curve/metrics/year-by-year/closed trades all populate |
| BT-4 | Fundamentals Build History badge | Shows AVAILABLE/NOT AVAILABLE correctly per cache existence |
| BT-5 | All closed trades | Paginated 20/page, filters (symbol/reason/outcome) work |

## 8. Admin
| ID | Case | Expected |
|----|------|----------|
| AD-1 | Strategy configuration | Change a value, Save → `state_db.update_strategy_config` persists, confirmed on reload |
| AD-2 | Skip stocks from scanner | Toggle Skip on a symbol, Save → symbol drops from `config.UNIVERSE`, filter (All/Skipped/Not) works |
| AD-3 | Change dashboard password | Change + confirm mismatch → error; matching → success, DB hash updates |
| AD-4 | Kite API settings | Update key/secret → state_db updated (does not affect mocked kite_client) |

## 9. Ledger
| ID | Case | Expected |
|----|------|----------|
| LD-1 | Log a deposit | Enter date/amount/note, submit → new row appears in table below |
| LD-2 | Select row to edit | Click a row's radio → form above pre-fills with that row's values |
| LD-3 | Save changes | Edit amount, Save → row updates, "Updated." confirmation |
| LD-4 | Delete entry | Select row, Delete → row removed |

## 10. Tradebook
| ID | Case | Expected |
|----|------|----------|
| TB-1 | Filters | Symbol/Status/Exit reason/Entered-since narrow the table correctly |
| TB-2 | Win-rate / P&L summary cards | Compute correctly from seeded closed trades |
| TB-3 | Download CSV | Button present, doesn't error |

## 11. Job Log
| ID | Case | Expected |
|----|------|----------|
| JL-1 | Per-job-type cards | Show last run age + status badge for each of the 4 job types |
| JL-2 | Failed-run card | Shows only the traceback's LAST LINE, not the full dump |
| JL-3 | History table + filters | Job type/Status/Since filter correctly |
| JL-4 | Failed runs expander | Full traceback visible on demand |

## 12. Rebalance History
| ID | Case | Expected |
|----|------|----------|
| RH-1 | Filters | Action/Status/Symbol/Since narrow correctly |
| RH-2 | Lifecycle badges | proposed/executed/error/expired colored correctly |
| RH-3 | Populated by LR-4/LR-5/LR-6 | Rows from the Live Rebalance execute actions appear here |

---

## Out of scope for this pass
- Real Kite OAuth login flow (requires a real broker session).
- Live NSE fundamentals scan / fundamentals history build (real external
  network dependency, read-only but unnecessary to hit repeatedly in
  automated testing).
- Anything requiring the production VPS.
