# Architecture

## Shape

- **Hub config entry** — the shared resources: buy-price/day-ahead/forecast-price
  entities, the weather and outdoor-temperature entities, the demand-model
  choice, the `LiveHeatingCostsCoordinator`, the `RuntimeStore`, the `log_wood`
  service, and the bundled-card registration.
- **One config *subentry* per heat source** — its own parameters and its own
  device + entities (`sensor` cost/capacity/[COP]/[forecast], `binary_sensor`
  [heating/active], `number` [fuel price] — bracketed ones are source-type
  dependent). One hub-level device holds the whole-house sensors (cheapest
  source, demand, UA coefficient, combined forecast, demand-covered).

## Data flow

```
buy price entity ───────────────────────────┐
day-ahead price entity ─┐                   │
forecast-price entity ──┤ price_source       │
                         │ .slots_from_state  │
                         ▼                    │
                  UTC ForecastSlots           │
                         │                    │
weather entity (hourly) ─── coordinator._async_weather_temps (cached 30 min)
                         │                    │
outdoor temp sensor ─────┼────────────────────┤
  (+ weather fallback,   │                    │
   temp_source.resolve)  │                    │
pump power/energy, ──────┤                    │
  climate.hvac_action    │                    │
Shelly/energy counters ──┤                    │
wood activity entities ──┘                    │
                                               ▼
                    coordinator: 60s sampling tick
                    ├─ per-curve-source sample deque (blend.Sample)
                    ├─ hub temperature deque (24h window)
                    └─ day accumulator (degree-hours, delivered/electricity/wood kWh)
                                               │
                    ┌──────────────────────────┼───────────────────────────┐
                    ▼                          ▼                           ▼
          curves.Curve.value_at      blend.window_cop / blend_cop   demand.integrate_degree_hours
          (COP(T), capacity(T))      (idle→modeled, gated on         + demand.instant_demand_kw
                    │                 heating_fraction)                    │
                    └──────────────┬───────────┘                          │
                                   ▼                                       │
                    cost_engine.electric_cost / wood_cost_eur_per_kwh      │
                                   │                                       │
                                   ▼                                       ▼
                    cost_engine.rank + merit_order  ◄──────── HeatingDemandSensor's demand_kw
                                   │
                                   ▼
                              HubData (coordinator.build_hub_data)
                                   │
                    ┌──────────────┼───────────────────────┐
                    ▼              ▼                       ▼
             sensor.py      binary_sensor.py          number.py
          (per-source +     (heating/active,        (fuel_price,
           hub sensors)      demand_covered)          Store-backed)
                    │
                    ▼
        bundled card (frontend/live-heating-costs-card.js)
        reads sensor attributes only — see docs/card.md

Day roll / capture (_maybe_roll_day: on load, top of every sampling tick,
and the 00:05 local safety-net job — date-guarded, so a day is captured once
even if HA was down across midnight):
  stale day accumulator ──► predict (out-of-sample) ──► note_prediction
    ──► ua_learner.make_row(coverage=temp_covered_s / real day length) + ingest
    ──► UaState ──► RuntimeStore; new DayAccum keeps the counter baselines
  (quality-gated: degree_hours >= 24, delivered_kwh in [0.5, 200],
   temperature coverage >= 90 %)
  A sampling gap > SAMPLE_GAP_REBASELINE_S re-baselines the energy counters
  instead of crediting the outage's jump.

Recorder background tasks (async_setup_entry, never block setup):
  history.async_backfill — whole local days before today, days with fewer
    than BACKFILL_MIN_TEMP_HOURS hourly means skipped, runs once
    (persisted backfill_done) ──► rows tagged backfilled=True (slower EWMA α)
  coordinator.async_seed_temp_history — last 24 h of hourly means (°C) seed
    the 24 h temperature history so the demand model doesn't restart cold

Forecast rebuild (on the :00/:15/:30/:45 slot grid, fuel-override edits, or
price-entity changes via the recompute throttle; serialized by a lock,
requests coalesced, prices read after the weather await):
  day-ahead + estimated price slots + hourly weather temps
    ──► forecast.build_source_forecast (per curve/dynamic_fixed source)
    ──► forecast.combined_slots (union by exact time boundary, cheapest tagged)
    ──► HubData.forecast / forecast_combined ──► SourceCostForecastSensor / HubCostForecastSensor
```

## Modules

| Module | Responsibility | HA? |
|---|---|---|
| `const.py` | Domain, config keys, tunables, unit-convention docstring | no |
| `curves.py` | `Curve` (piecewise-linear, clamped, non-monotone-safe), text/JSON parsing, `PRESETS` | no |
| `blend.py` | `window_cop` (reconcile modeled COP against metered energy) + `blend_cop` (gate on heating fraction) | no |
| `demand.py` | `integrate_degree_hours` (trapezoidal, DST-safe), `instant_demand_kw`, `daily_energy_kwh` | no |
| `ua_learner.py` | `DailyRow`/`UaState`, `make_row`, `ingest` (EWMA + clamp), `effective_ua`, shadow MAE | no |
| `forecast.py` | `TempPoint`, `SourceSlotCost`, `build_source_forecast`, `combined_slots` | no |
| `price_source.py` | Auto-detect + normalize heterogeneous price-entity attribute layouts → UTC `ForecastSlot`s | no |
| `temp_source.py` | Outdoor-temperature fallback chain (`resolve`) + weather-attribute parsing (`weather_temp_c`) | no |
| `cost_engine.py` | `SourceSnapshot`, `electric_cost`, `wood_cost_eur_per_kwh`, `rank`, `merit_order`, `Allocation` | no |
| `models.py` | Subentry `data` → `StaticSourceConfig` / `DynamicFixedConfig` / `CurveSourceConfig` | no |
| `coordinator.py` | Sampling tick, day accumulator, snapshot build, debounced recompute, forecast rebuild, daily capture | yes |
| `history.py` | One-shot recorder-statistics backfill of the UA learner | yes |
| `persistence.py` | `RuntimeStore` — `Store`-backed load/schedule-save/save-now | yes |
| `config_flow.py` | Hub flow (+ reconfigure) and the source subentry wizard (static/dynamic_fixed/curve) | yes |
| `entity.py` | `LiveHeatingCostsEntity` (one device per source subentry) / `LiveHeatingCostsHubEntity` (hub device) | yes |
| `sensor.py`, `binary_sensor.py`, `number.py` | Entity platforms | yes |
| `diagnostics.py` | Config-entry diagnostics (serializes dataclasses/datetimes) | yes |
| `__init__.py` | Setup, Lovelace resource registration for the bundled card, `log_wood` service | yes |
| `frontend/live-heating-costs-card.js` | Bundled vanilla-JS Lovelace card (Now + Forecast views), no build step | — |

## Key contracts

- **Units**: EUR/kWh internally everywhere; the sensor platform converts to
  c/kWh exactly once (×100), never earlier. Power is kW, energy kWh,
  temperature °C, COP dimensionless.
- **Timezone**: every pure module requires timezone-aware datetimes and raises
  `ValueError` on a naive one (`blend.Sample.__post_init__`,
  `demand._require_aware`, `forecast._require_aware`). No pure module calls
  `now()` — the coordinator (or a test) supplies `now` explicitly, which is
  what keeps the whole chain DST-correct (a 23h/25h day integrates its true
  real-time length in `demand.integrate_degree_hours`).
- **None-propagation, never COP = 1**: `cost_engine.electric_cost(buy, cop)`
  returns `None` if either input is `None` or `cop <= 0`. `rank()` excludes
  any snapshot whose `cost_eur_per_kwh` is `None`; `merit_order()` never
  allocates to an excluded or zero-capacity source. This is the direct fix for
  the legacy card's `float(1)` COP-unavailable fallback — see
  `docs/heating-costs.md`.
- **Blend gating + the honest-numerator caveat**: `blend.window_cop`
  reconciles a *modeled* COP(T) curve against *metered* electricity over a
  rolling window — `measured_cop = Σ(cop_at(temp) × Δenergy while heating) /
  Σ(Δenergy while active)`. `blend_cop` then weights that measured value by
  `heating_fraction` against the pure modeled COP, so an idle window
  (`heating_fraction = 0`) collapses to the modeled curve and a fully-active
  window collapses to the measured value. Caveat that must stay documented
  wherever this is surfaced: the numerator is still the *modeled* curve, not
  an independent heat measurement — this is "curve reconciled against
  metered electricity," a reconciliation, not a calorimeter. It fixes the
  idle-floor artifact; it does not turn the pump into a heat meter.
- **Merit-order semantics** (`cost_engine.merit_order`): fills `demand_kw`
  strictly cheapest-first from the already-`rank`ed list; a source with
  `capacity_kw <= 0` or unknown cost is skipped entirely (never allocated,
  even if present in `ranked`). Returns `(allocations, demand_covered,
  blended_cost)`, where `blended_cost` is the demand-weighted average
  €/kWh of what was *actually* allocated (`None` if nothing was).
- **Card attribute contract**: `CheapestSourceSensor` and
  `HubCostForecastSensor`'s `extra_state_attributes` are the card's entire
  data surface, and their exact key sets are locked by
  `tests/ha/test_card_state.py`. See [card.md](card.md) for the full contract
  and the card's own consumption of it — the two must be changed together.
- **Store schema (v1)**: one `Store` per hub entry
  (`STORAGE_VERSION = 1`, key `live_heating_costs.{entry_id}`), holding:
  `ua` (`UaState.to_dict()` — seed, learned, sample_count, rolling_mae_kwh,
  last_predicted_kwh, rows), `day_accum` (`DayAccum.to_dict()` — date,
  degree_hours, delivered/electricity/wood kWh, per-source kWh, last-seen
  energy counters, `last_tick_utc`, `temp_covered_s`), `wood_log` (capped
  to the last 500 entries), `fuel_price_overrides`, `last_event_ts`
  (per-source, for event-mode debounce), and `backfill_done`. Significant
  events save within 10 s (`SAVE_DELAY_S`); the 60 s sampling tick within
  300 s (`SAVE_DELAY_TICK_S`). `RuntimeStore.schedule_save` only ever pulls
  a pending write *earlier* (plain `Store.async_delay_save` postpones it to
  the newest deadline, which a steady tick would do forever); HA's
  final-write hook flushes on shutdown and unload saves immediately.
- **Outdoor temperature is a chain, not one entity** (`temp_source.py`): a
  curve source resolves `pump reading -> per-source override -> hub sensor ->
  weather entity`, and the hub's own temperature (degree-hour accumulation,
  the hub temperature sensor, `forecast.anchor_temps`) resolves the last two.
  Every link is a real measurement of the same quantity and the winner is
  published as the `temp_source` attribute, so nothing is invented — when all
  links fail the answer is still `None` and the source is still excluded with
  `temp_unavailable`. Readings outside `PLAUSIBLE_TEMP_RANGE_C` (-90..60 °C)
  are rejected as sentinels, and a weather entity reporting °F is converted.
  Without the weather link a single flat sensor battery blanked out every heat
  pump *and* stalled degree-hour accumulation, which is worse than it sounds:
  a half-day gap keeps the day's full `delivered_kwh` against half its
  degree-hours, teaching the UA learner roughly double the true coefficient.
- **Market-day / tomorrow-prices honesty**: `has_tomorrow` on both the
  per-source and the hub forecast sensor means "the *real* (non-estimated)
  price horizon extends more than 12 h beyond now" — it says nothing about
  the estimated extension. A curve source's forecast slot is *skipped*
  entirely once it falls beyond the weather forecast's own horizon (no
  extrapolated temperature, no fabricated cost); a fixed-COP source has no
  such limit since temperature doesn't enter its cost at all.

See the per-module docstrings (especially `blend.py`, `ua_learner.py`,
`forecast.py`, and `cost_engine.py`) for the calculation details; this doc is
the map, not a substitute for reading them before changing behavior.

## Optional whole-house shadow model

`shadow_config.py` validates portable expert configuration. `shadow.py` is the HA
boundary for `telemetry.py` counter integrity, `ventilation.py` manufacturer
interpolation, `thermal.py` air/fabric dynamics, `allocation.py` room constraints,
and `calibration.py` chronological validation. Pure modules take explicit UTC
timestamps and never access HA. The legacy demand path and entity identities
remain available; no model actuates devices.

Five-minute features and pre-issued one-hour forecasts are retained for 90 days
in the existing Store under a versioned `shadow` payload. Configuration changes
invalidate validation evidence. Calibration readiness is cached between new
features; full feature histories are excluded from sensor attributes. Offline
bounded fitting runs through the host heavy-job wrapper. See
[demand-model.md](demand-model.md) for calibration and activation requirements.
