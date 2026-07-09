# Live Heating Costs

[![hacs](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration that compares every way a house can be
heated — wood/fuel, resistive electric, and air-source heat pumps — by the
**cost of the heat they actually deliver** (c/kWh), ranks them, and works out
which source(s) should be covering the current demand right now.

> **Status: early development.** Install as a HACS custom repository (below)
> — not yet in the HACS default store. Backed by an extensive pure-engine +
> Home-Assistant-layer test suite.

## Why

A "which heat source is cheapest right now" card sounds simple until a heat
pump is involved: its efficiency (COP) depends on outdoor temperature, on how
hard it's working, and on defrost cycling — none of which a flat number
captures. The naive shortcut (`instant_cop = modeled_capacity / max(power,
floor)`) looks fine until the pump goes idle: power collapses to near zero,
the floor takes over, and COP gets pinned to something like 20–22. That made
one real card claim delivered heat at **~0.24 c/kWh** — cheaper than doing
nothing — while the pump wasn't running at all. That bug is the reason this
integration exists: COP always comes from a manufacturer/measured curve, and
a *measured* COP is only ever blended in proportional to how much of the
sampling window the pump was actually heating. Idle → pure modeled COP.
Running flat-out → pure measured COP. No floor, no artifact.

Everything else follows the same rule: an electric source's cost is `buy
price ÷ COP`, and if either side of that is unknown, the **cost is unknown**
— never silently treated as COP = 1. There is no dashboard trick to hide a
sensor that hasn't caught up yet.

## Features

- **Three source types** — fixed-cost fuel (wood), electric with a fixed COP
  (resistive), and heat pumps modeled by COP(T)/capacity(T) curves.
- **Cost ranking + merit-order allocation** — sources are ranked cheapest
  first and the current heat demand is filled cheapest-first across whichever
  sources can dispatch it, with a demand-weighted blended cost.
- **The idle-cost fix** — modeled COP is the baseline; a measured COP
  (reconciled from metered electricity against the modeled curve) is blended
  in weighted by how much of the sampling window the pump spent actually
  heating, so idle time never fakes a bargain COP.
- **UA learning** — the house's heat-loss coefficient is seeded from a
  reasonable default and refined over time from logged daily outcomes (EWMA,
  quality-gated, with a one-time recorder backfill and a shadow accuracy
  metric).
- **24–48 h cost forecast** per source, from day-ahead electricity prices and
  hourly weather, with an optional beyond-horizon "estimated" extension and
  honest truncation when tomorrow's real prices aren't out yet.
- **Bundled Lovelace card** — a dependency-free vanilla-JS card with a "Now"
  ranking view and a "Forecast" timeline view. See [docs/card.md](docs/card.md).
- **Advisory only** — this integration never calls a `climate`/`switch`
  service. It only computes and reports; actuation (e.g. shedding a heat pump
  above a price threshold) belongs to your own automations.
- **Backs up with Home Assistant** — all runtime state (UA learner, day
  accumulator, wood log, fuel-price overrides) lives in a `Store`, included in
  HA backups.

## Source types

| Type | Config key | Typical use | Cost | Capacity |
|---|---|---|---|---|
| Fixed-cost fuel | `static` | Wood stove, fuel-fired sauna | `price ÷ (energy_density × efficiency)` — fixed, independent of electricity price | Configured kW (0 = contributes heat and feeds UA learning but is never dispatched) |
| Electric, fixed COP | `dynamic_fixed` | Resistive heater (COP 1.0), or any electric source with a known constant COP | `buy_price ÷ COP` | Configured kW |
| Heat pump (COP curve) | `curve` | Air-source heat pump | `buy_price ÷ blended COP` | Modeled `capacity(T)`; 0 below the operating limit |

## Built-in heat-pump presets

Two measured/published COP(T) and capacity(T) tables ship with the
integration (pick "Custom" in the config flow to paste your own instead):

- **Mitsubishi MSZ-FD25VA** — COP table is VTT's (the Finnish state research
  institute) real-operation measurement of this exact unit, a sliding 1 h
  mean *including defrost* (report VTT-S-09606-08). It is not monotone: it
  dips above +10 °C as the unit cycles at mild-weather part load. Operating
  limit −28 °C.
- **Mitsubishi MSZ-RW35VG** — COP table is Eurovent's EN 14825 load-following
  declared points. The −25 °C point is an **estimate** — no published data
  exists that cold for this unit. Operating limit −30 °C.

Both tables interpolate linearly between points and clamp flat beyond the
endpoints; neither is required to be monotone (see [CLAUDE.md](CLAUDE.md) for
why COP curves in general aren't).

## Entities created

One device per heat source (subentry) plus one hub device for whole-house
sensors.

### Per source

| Entity | Platform | Applies to | Unit | Meaning |
|---|---|---|---|---|
| Heat cost | `sensor` | all | c/kWh | Live delivered-heat cost. Unavailable whenever the cost can't be honestly computed. |
| Capacity | `sensor` | all | kW | Current dispatchable capacity (0 for a heat pump below its operating limit, or a fuel source configured with capacity 0). |
| COP | `sensor` | curve, dynamic_fixed | — | Current (blended) COP. Never a silent fallback to 1.0. |
| Cost forecast | `sensor` | curve, dynamic_fixed | c/kWh | Next slot's projected cost; the `forecast` attribute carries the full 24–48 h series. |
| Heating | `binary_sensor` | curve | — | On while this pump is actively delivering heat (excludes idle and pure defrost). |
| Active | `binary_sensor` | static, if activity detection is enabled | — | On while the wood/fuel source is detected active. |
| Fuel price | `number` | static | €/unit | Editable per-unit fuel price; overrides the configured default and persists across restarts. |

### Hub

| Entity | Platform | Unit | Meaning |
|---|---|---|---|
| Cheapest source | `sensor` | — | State = name of the cheapest dispatchable source. Attributes carry the full ranking, exclusions, demand, coverage and blended cost — this is the card's main data source. |
| Heating demand | `sensor` | kW | Current house heat demand (internal UA model or an external entity). |
| Heat-loss coefficient | `sensor` | W/°C | Effective UA (seed blended with learned). Attributes show seed, learned value, sample count and rolling MAE. |
| Cost forecast | `sensor` | — | State = ISO timestamp of the last forecast rebuild; the `slots` attribute carries the combined per-source forecast. |
| Demand covered | `binary_sensor` | — | On when merit-order allocation fully covers the current demand. |

## UA learning

House heat demand is modeled as `E ≈ UA × degree_hours`, where `degree_hours`
is a proper time integral of `max(base_temp − outdoor_temp, 0)` and `UA`
(kW/°C) is the house's heat-loss coefficient.

- **Seed**: `UA` starts at a configured prior — the default, **140 W/°C**,
  reproduces the legacy hand-tuned heating-demand constant it replaces.
- **Learning**: once a day (00:05 local), the day's actual delivered heat
  (Σ heat-pump `energy × COP` + resistive kWh + logged/estimated wood) and
  degree-hours are folded into an EWMA of the `delivered_kwh / degree_hours`
  ratio — α 0.15 for live days, a slower α 0.05 for backfilled ones, so bulk
  history can't dominate live learning. The learned value is clamped to
  0.3–3.0× the seed so one bad day can't run away with the model, and blends
  in linearly over the first 14 quality samples (`effective_ua` is a
  seed/learned mix until then).
- **Quality gates**: a day only teaches the model if it had a meaningful
  heating load (≥ 24 degree-hours) and its delivered heat was plausible
  (0.5–200 kWh) — guards against meter resets or a burn night nobody logged
  producing a wild ratio. Non-quality days are still stored and shown.
- **Backfill**: on first setup (only once — a reload never double-imports),
  up to `backfill_days` (default 60) of recorder history for metered sources
  is replayed through the same learner, so the model doesn't start cold.
- **Shadow accuracy**: each day's prediction is scored against what actually
  happened, producing a rolling MAE (kWh) you can watch to judge whether the
  model is actually tracking the house — before deciding to trust it further.
- **Wood keeps the learner honest**: a wood stove has no recorder-visible
  energy counter, so its delivered heat only reaches the day accumulator (and
  therefore the UA learner) via its activity-detection entity (duration or
  event mode) or the `log_wood` service. Skip logging wood and every burn
  night looks like the house needed less heat than it did.

## Forecast

Each electric source gets a 24–48 h projection of its own delivered-heat
cost, plus a combined hub view:

- **Day-ahead prices**: 15-minute slots parsed from the configured day-ahead
  price entity (Nord Pool, ENTSO-e, or a compatible template — auto-detected
  format).
- **Weather-driven COP**: hourly forecast temperatures (from the configured
  weather entity, cached 30 min) are linearly interpolated to each slot's
  midpoint and run through the same COP(T)/capacity(T) curve as the live
  reading. A heat-pump slot beyond the weather forecast's own horizon is
  **skipped outright** rather than extrapolated — there's nothing honest to
  say about it. A fixed-COP source has no such limit and is priced for every
  slot.
- **Estimated extension**: an optional beyond-horizon "forecast price" entity
  extends the series past the real day-ahead data (which usually only covers
  today + tomorrow); those slots are tagged `estimated: true` throughout, and
  a `has_tomorrow` attribute tells you honestly whether the real horizon
  actually reaches past ~12 h — no pretending an estimated slot is real data.
- **Combined view**: the hub's forecast sensor unions every source's slots by
  exact time boundary and marks the cheapest source per slot.

## The `log_wood` service

Manually log delivered wood heat for a static (fuel) source — as a direct
kWh figure, or as a fuel-unit count converted via that source's configured
energy density and efficiency. Feeds the same day accumulator (and therefore
the UA learner) as automatic activity detection.

```yaml
service: live_heating_costs.log_wood
data:
  energy_kwh: 12.5
  source: "Wood stove" # optional; title or subentry ID, defaults to the first static source
```

or, by fuel quantity:

```yaml
service: live_heating_costs.log_wood
data:
  units: 0.05 # e.g. 0.05 m³ of stacked wood
```

Specify exactly one of `energy_kwh` or `units`.

## Card

A bundled, dependency-free Lovelace card (`custom:live-heating-costs-card`)
with a ranked "Now" view and an SVG "Forecast" timeline, both auto-discovering
the integration's sensors:

```yaml
type: custom:live-heating-costs-card
```

![Now view screenshot placeholder](docs/images/card-now.png)
![Forecast view screenshot placeholder](docs/images/card-forecast.png)

See [docs/card.md](docs/card.md) for the full view breakdown, configuration
options, and the exact attribute contract it's written against.

## Installation

### HACS (custom repository)

1. HACS → ⋮ → *Custom repositories* → add
   `https://github.com/machadolucas/live-heating-costs`, category
   **Integration**.
2. Install **Live Heating Costs**, then restart Home Assistant.
3. *Settings → Devices & Services → Add Integration → Live Heating Costs.*

### Manual

Copy `custom_components/live_heating_costs` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

1. **Add the hub**: your live all-in buy-price sensor (EUR/kWh) and an
   outdoor-temperature sensor are required; a day-ahead price sensor, a
   beyond-horizon forecast-price sensor, and a weather entity are optional
   (they unlock the forecast). Then pick the heat-demand mode — *estimate
   from outdoor temperature* (the internal UA model, default) or *measure
   from an existing sensor* — and, for the internal model, the balance-point
   temperature (default 17 °C), UA seed (default 140 W/°C), whether UA
   learning is on (default yes), and how many days of recorder history to
   backfill (default 60).
2. **Add heat sources**, one at a time, from the integration's page:
   - *Fixed-cost fuel*: fuel unit (m³/kg/kWh), price per unit, energy density
     (defaults assume stacked birch: 1700 kWh/m³ or 4.1 kWh/kg), combustion
     efficiency, capacity, and optional activity detection — *duration* mode
     integrates an assumed output while a switch/binary_sensor/input_boolean
     is on, *event* mode credits a fixed kWh per debounced off→on transition
     (useful for a sauna heated in discrete sessions).
   - *Electric, fixed COP*: COP (1.0 for pure resistive), capacity, and an
     optional energy counter to feed the UA learner.
   - *Heat pump (COP curve)*: first the live sensors (power, cumulative
     energy, optional climate entity for `hvac_action` gating, optional
     pump-reported or per-source-override temperature, standby watts, COP
     blend window), then the COP(T)/capacity(T) tables — pick a built-in
     preset, or "Custom" and paste `temp:value` pairs (e.g.
     `-30:1.35, -25:1.6, 7:4.2`) or a JSON list of `[temp, value]` pairs.

## FAQ / notes

- **Why is a source's cost "unknown" instead of some number?** Three
  reasons, surfaced as `unavailable_reason`: `below_operating_limit` (a heat
  pump too cold to run), `price_unavailable` (the buy-price sensor is
  down), or `temp_unavailable` (no outdoor temperature to evaluate a curve
  source). An unknown COP or price always propagates to an unknown cost —
  there is **no** fallback to COP = 1, ever.
- **c/kWh vs EUR/kWh**: every entity displays cost in **c/kWh**. Internally,
  the whole engine works in **EUR/kWh**; the single ×100 conversion happens
  once, in the sensor platform.
- **Does this control anything?** No. It's advisory-only — it computes costs,
  rankings and a forecast, and that's the whole surface. Pair it with your
  own price-triggered automations if you want actuation.

## Development

```bash
uv venv --python 3.13 .venv313
uv pip install --python .venv313/bin/python -r requirements_test.txt
.venv313/bin/python -m pytest
.venv313/bin/ruff check . && .venv313/bin/ruff format --check .
```

The pure engine modules (`curves`, `cost_engine`, `blend`, `demand`,
`ua_learner`, `forecast`, `price_source`, `models`) have no Home Assistant
import and are tested standalone with plain `pytest`; `tests/ha/` needs
`pytest-homeassistant-custom-component`, which requires Python 3.13 (Home
Assistant doesn't support 3.14 yet).

See [CLAUDE.md](CLAUDE.md) and [docs/architecture.md](docs/architecture.md)
for architecture and design notes.

## License

MIT © Lucas Machado
