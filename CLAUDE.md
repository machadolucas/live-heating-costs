# CLAUDE.md — Live Heating Costs

Working notes for AI agents and future-me. Read this before changing code.

**Status:** pre-release (`manifest.json` at `0.2.0`, "card clarity +
compactness" — no tags cut yet). All features described here are implemented
and covered by tests — run them (see Dev workflow) before trusting a change.
See `docs/architecture.md` for the module map and data flow.

## What this is

A Home Assistant custom integration (`live_heating_costs`) that compares a
house's heat sources — wood/fuel, resistive electric, and heat pumps modeled
by COP(T)/capacity(T) curves — by delivered-heat cost, ranks them, and
allocates the current heat demand cheapest-first. It replaces a pile of
hand-tuned template sensors and `input_number` helpers on the author's home
server (the `macserver` repo, `docs/heating-costs.md`), whose worst bug was an
"instant COP" computed as `modeled_capacity / max(power, floor)`: when a pump
idled, the floor pinned COP to ~20–22, making delivered heat look like it cost
~0.24 c/kWh while the pump wasn't running. This integration exists specifically
to make that bug structurally impossible — see the engine contract below.

## Architecture

- **Hub config entry** — shared price/weather/demand config, the coordinator,
  the `Store`, the `log_wood` service, and the bundled-card registration.
- **One config *subentry* per heat source** — its own device + entities.

### Modules (`custom_components/live_heating_costs/`)

| File | Role | HA? |
|---|---|---|
| `const.py` | Domain, config keys, defaults, unit-convention docstring | no |
| `curves.py` | Piecewise-linear `Curve`, text/JSON parsing, built-in `PRESETS` (FD25/RW35) | no |
| `blend.py` | Modeled/measured COP blending gated on heating fraction — the idle-cost fix | no |
| `demand.py` | Degree-hours integration (trapezoidal, DST-safe) + UA-based demand | no |
| `ua_learner.py` | UA (heat-loss coefficient) EWMA learning: seed, quality gates, clamp, shadow MAE | no |
| `forecast.py` | Per-source, per-slot delivered-heat cost projection from prices + weather | no |
| `price_source.py` | Normalise heterogeneous price-forecast entities → UTC `ForecastSlot`s | no |
| `temp_source.py` | Outdoor-temperature fallback chain (pump → override → hub → weather) | no |
| `cost_engine.py` | `SourceSnapshot`, `electric_cost`/`wood_cost_eur_per_kwh`, `rank`, `merit_order` | no |
| `models.py` | Subentry `data` mapping → typed `SourceConfig` variant, validated | no |
| `coordinator.py` | Sampling tick, snapshot build, debounced recompute, forecast rebuild, daily capture | yes |
| `history.py` | One-shot recorder backfill of the UA learner | yes |
| `persistence.py` | `Store`-backed `RuntimeStore` (UA state, day accumulator, wood log, overrides) | yes |
| `config_flow.py` | Hub flow (+ reconfigure) and the source subentry wizard | yes |
| `entity.py` | Base entities: one device per source subentry, one hub device | yes |
| `sensor.py` | Per-source + hub sensors (cost, capacity, COP, forecast, demand, UA, ranking) | yes |
| `binary_sensor.py` | Per-source activity/heating flags + hub demand-covered flag | yes |
| `number.py` | Per-source editable fuel price | yes |
| `diagnostics.py` | Config-entry diagnostics dump | yes |
| `__init__.py` | Setup, card resource registration, `log_wood` service | yes |
| `frontend/live-heating-costs-card.js` | Bundled vanilla-JS Lovelace card (Now + Forecast views) | — |

## The engine contract (read before touching any pure module)

- **Money**: EUR/kWh everywhere internally; the single ×100 conversion to the
  displayed unit (c/kWh) happens exactly once, in `sensor.py`.
- **Units**: power kW, energy kWh, temperature °C, COP dimensionless.
- **Time**: UTC, timezone-aware everywhere in pure code. `blend.Sample`,
  `demand.integrate_degree_hours`, and `forecast.build_source_forecast` all
  raise `ValueError` on a naive datetime. **No pure module calls `now()`** —
  the coordinator supplies it.
- **None-propagation, never COP = 1**: `cost_engine.electric_cost` returns
  `None` if `buy_eur_per_kwh` or `cop` is `None`, or `cop <= 0`. `rank()` and
  `merit_order()` exclude any source whose cost is `None` — an unknown COP or
  price makes a source unranked and unallocated, not "priced like resistive."
  This is the exact bug this integration was built to eliminate; don't
  reintroduce a fallback anywhere in this chain.
- **Blend gating semantics** (`blend.py`): `is_active` (doing work at all)
  includes both `"heating"` and `"defrosting"` `hvac_action`; `is_producing_heat`
  is `"heating"` only. So a defrost segment counts toward the *denominator*
  (active seconds) but contributes zero to the modeled-heat *numerator* —
  that asymmetry is what drags the measured COP down during a defrost cycle,
  which the old floor hack could never see. `blend_cop` returns the pure
  modeled COP (clamped) whenever there's no usable measurement, and blends
  proportional to `heating_fraction` when there is. No modeled COP (below the
  operating limit) means no answer at all, regardless of measurement.
- **Forecast temperature anchoring** (`forecast.anchor_temps`): the hourly
  weather forecast's first point is the *next* full hour, and `interp_temp`
  rightly refuses to extrapolate before it — so every curve source used to
  drop out of the leading slots and `combined_slots` crowned a resistive
  source "cheapest" for the current hour. `anchor_temps` prepends the
  *measured* outdoor temperature as a `now` point (a real reading, not an
  extrapolation) so those slots interpolate normally instead; wired in
  `coordinator.async_rebuild_forecast`, a no-op when the series already
  covers `now` or no current reading is available.
- **Static sources in the combined forecast** (`forecast.combined_slots(...,
  static_costs=...)`): wood/sauna have no per-slot forecast of their own (flat
  cost, no weather dependence), but leaving them out of the union meant a
  cheap fuel source could never win the forecast band even when it plainly
  beat every electric source all day. `static_costs` folds them in as
  `{subentry_id: (cost_eur_per_kwh, capacity_kw)}`, same eligibility as a
  dynamic source (known cost, nonzero capacity — a 0 kW static source is
  never "cheapest"). Each slot also gets `partial: bool`: true when a source
  that *does* have slots elsewhere is missing at this boundary (e.g. a heat
  pump before the weather forecast starts) — `cheapest` is still computed
  from what's known, but the card draws that slot as an honest "don't know"
  rather than implying the missing source was priced out. A fuel-price
  override edit now also triggers an immediate forecast rebuild (previously
  it waited for the next 15-min tick), since the static line depends on it.
- **Outdoor temperature falls back, cost never does** (`temp_source.py`): a
  curve source resolves `pump reading -> per-source override -> hub sensor ->
  weather entity`; the hub's own temperature (degree-hour accumulation, the
  hub `temp_c`, `forecast.anchor_temps`) resolves the last two links. This is
  *not* a hole in the None-propagation rule above: every link is a real
  measurement of the same physical quantity, the winning one is published as
  the `temp_source` attribute, and when all links fail the result is still
  `None` — the source is still excluded with `temp_unavailable`, never priced
  at a guessed temperature. `PLAUSIBLE_TEMP_RANGE_C` (-90..60 °C) rejects
  sentinels like -999 and unconverted Kelvin; a weather entity reporting °F is
  converted. The weather entity is in the coordinator's watched set precisely
  because it *is* the temperature source while the sensor is down. Motivation:
  one flat RuuviTag battery took out every heat pump's COP and stalled
  degree-hour accumulation — and a *partial* stall is the dangerous case,
  since a day keeps its full `delivered_kwh` against half its degree-hours and
  teaches the UA learner roughly double the true coefficient.
- **Missing entity ids are logged, not just silent**: `_warn_missing_entities`
  (first sampling tick onward, once per id) warns for any configured entity
  that is absent from the state machine. A renamed or mistyped id otherwise
  looks exactly like a temporarily unavailable one — both just yield `None` —
  so a source could sit unavailable for months with nothing in the log.
- **Entity-id attributes, never parsed client-side**: `sensor.py` resolves
  each row's own sensor's entity id from the entity registry (unique id
  `{subentry_id}_heat_cost`, `{entry_id}_heating_demand`) and publishes it as
  `entity_id` on every `ranking`/`excluded` row and as the top-level
  `demand_entity_id`. The card only ever receives a ready-made entity id —
  it never derives one from a name or subentry id — and opens it via a
  `hass-more-info` event.

## UA learner contract (`ua_learner.py`, tuned in `const.py`)

- **Quality gates** (`make_row`): a day only teaches the model if
  `degree_hours >= UA_MIN_DEGREE_HOURS` (24) and `delivered_kwh` falls inside
  `UA_DELIVERED_RANGE_KWH` (0.5–200). Non-quality rows are still stored/shown,
  just not folded into `learned`.
- **EWMA alphas**: `UA_EWMA_ALPHA_LIVE` = 0.15 for live days,
  `UA_EWMA_ALPHA_BACKFILL` = 0.05 for backfilled ones — deliberately slower so
  a bulk historical import can't dominate live learning.
- **Clamp**: `learned` is clamped to `UA_LEARNED_CLAMP` (0.3×–3.0×) the
  configured `seed` after every update, so one bad day can't run away with the
  model.
- **Seed/learned blend**: `effective_ua` ramps the learned value in linearly
  over the first `UA_SEED_BLEND_SAMPLES` (14) quality samples
  (`w = min(sample_count, 14) / 14`); before that, and whenever `learned` is
  still `None`, the seed alone is used.
- **Shadow accuracy**: `note_prediction` stashes a prediction; the next
  `ingest` (for a non-backfilled row) scores it into `rolling_mae_kwh` via a
  separate, harder-coded EWMA (`_MAE_ALPHA = 0.2`, not in `const.py` — it only
  tunes the observability metric, not the model itself).
- All of `ua_learner.py` is pure and immutable — every function returns a new
  `UaState`; there is no `datetime.now()`/`date.today()` in this module (the
  caller stamps dates).

## Advisory-only rule

This integration **never calls a `climate`/`switch`/any actuation service**.
The only `hass.services.async_call` in the codebase is the read-only
`weather.get_forecasts` in `coordinator.py` (for the forecast's temperature
series). If you're tempted to add an automation-like behavior (e.g. shutting
off a heat pump above a price threshold), it does not belong here — that's the
job of the user's own automations (see the `macserver` repo's
`docs/heating-costs.md`, which notes that `price-hold` already owns pump
actuation and this integration must stay advisory so the two never fight over
the same entity).

## Dev workflow

```bash
# HA-side tests need Python 3.13 (HA doesn't support 3.14 yet):
uv venv --python 3.13 .venv313
uv pip install --python .venv313/bin/python -r requirements_test.txt
.venv313/bin/python -m pytest
.venv313/bin/ruff check . && .venv313/bin/ruff format --check .
```

Pure-module tests (`tests/test_*.py`: curves, cost_engine, blend, demand,
ua_learner, forecast, price_source, temp_source, models) import their module
directly and need only `pytest` — no `hass` fixture, no Home Assistant
install. Keep those modules HA-free; if a change needs `hass`, it belongs in
`tests/ha/` instead.

## Testing conventions

- `tests/conftest.py` puts the repo root on `sys.path` and registers the
  `pytest_homeassistant_custom_component` plugin; the `enable_custom_integrations`
  autouse fixture lives in `tests/ha/conftest.py` specifically, so the pure
  tests never pull in Home Assistant.
- **Timing gotcha**: after mutating simulated time with
  `async_fire_time_changed`, call `hass.async_block_till_done()` **both
  before and after** — before, to let any pending debounced callback settle
  first; after, to let the newly-fired one run. See the `_tick` helper
  repeated in `tests/ha/test_coordinator.py`, `test_card_state.py`, and
  `test_ua_learning.py`.
- `tests/ha/conftest.py` provides builders — `hub_entry_data`,
  `wood_subentry`, `resistive_subentry`, `curve_subentry`,
  `async_setup_hub` — that assemble a `MockConfigEntry` with subentries and
  drive it through `async_setup`. Reuse these instead of hand-rolling subentry
  dicts.
- The card's attribute contract (`CheapestSourceSensor` and
  `HubCostForecastSensor`'s `extra_state_attributes`) is locked by
  `tests/ha/test_card_state.py` — it asserts the exact key sets, including
  each ranking/excluded row's `entity_id`, the top-level `demand_entity_id`,
  and each forecast slot's `partial` flag (and that resolved entity ids are
  real, registered entities, not just present). **Change the sensor and the
  card and this test together**, and update `docs/card.md`'s data contract
  section too.

## Releasing

A release is the **version bump + commit to `main` + a GitHub release**.
HACS reads the version from `manifest.json` and also picks up GitHub releases.

1. Bump `"version"` in `custom_components/live_heating_costs/manifest.json`
   (SemVer: minor for a feature, patch for a fix/tweak).
2. Commit directly to `main`. Message convention: `Area: short description`,
   e.g. `UA learner: …`, `Card: …`, `Forecast: …`.
3. `git push origin main`.
4. `gh release create vX.Y.Z --title "vX.Y.Z — short human description" --notes "…"`
   — the GitHub release creates the tag; there is no separate `git tag` step.

## Conventions

- Comment the *why*, not the *what* — match the density already in
  `blend.py`/`ua_learner.py`'s module docstrings.
- New calculation logic goes into a pure module (or a new one) as a tested
  function first, then gets wired into `coordinator.py`. Don't put
  calculation logic directly in the coordinator or the entity platforms.
- Capture real payloads (recorder statistics shapes, weather forecast items,
  price-entity attribute layouts) as test fixtures rather than inventing
  synthetic ones from scratch where a real shape is available.
- Don't commit secrets; there aren't any in this integration's own config
  (entity IDs and computed numbers only — see `diagnostics.py`), but don't
  assume that stays true forever.
