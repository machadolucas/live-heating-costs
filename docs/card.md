# Dashboard card

Live Heating Costs bundles one Lovelace card in a single dependency-free JS
file: `custom:live-heating-costs-card`. It has two views, switched by a small
header toggle:

- **Now** (default) — one row per heat source, ranked cheapest-first, plus a
  demand pill and the sources currently excluded from ranking.
- **Forecast** — an SVG timeline of per-source cost over the next 24/48h,
  with a bottom band coloured by whichever source is cheapest each slot.

The card is configurable from the dashboard UI (a visual editor) as well as
YAML, and auto-discovers the integration's sensors when `entity` /
`forecast_entity` are omitted.

## Now view

```
Heating costs                              [Now] Forecast
▲ 3.2 kW demand              mix 1.9 c/kWh
──────────────────────────────────────────────────────────
MSZ-RW35VG ● measured    5.1 kW   COP 2.9      1.8 c/kWh
▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░
MSZ-FD25VA ●              2.5 kW   COP 2.1      2.4 c/kWh
▓░░░░░░░░░░░░░░░░░░░
Resistive backup          9.0 kW   COP 1.0      9.2 c/kWh
──────────────────────────────────────────────────────────
Wood stove                              below operating limit
```

- The **demand pill** (▲ *X.X* kW demand) is green when `demand_covered` is
  true, red with `· short X.X kW` when it's false, and hidden entirely when
  `demand_kw` is `null`.
- **mix *X.X* c/kWh** — the blended cost across all allocated sources — shows
  next to the pill when `blended_cost_c_per_kwh` is present.
- Each row shows capacity and cost to **1 decimal**; COP is hidden when
  `null` (an unknown COP is never faked as 1.0).
- A small pulsing dot marks a source as `active`; a **measured** chip marks
  `measured_blend` (its cost came from a live measured blend, not the static
  curve).
- The cheapest row (`ranking[0]`) gets an accent left-border and tinted
  background.
- A thin **allocation bar** sits under any row with `allocated_kw > 0`: the
  bar's own width is `allocated_fraction` (this source's share of total
  demand); the darker fill inside it is `allocated_kw / capacity_kw` (how
  hard this source is working). Rows with no allocation show no bar.
- **Excluded** sources (cost couldn't be computed this tick) are listed at
  the bottom, greyed, with a human reason: `below_operating_limit` → "below
  operating limit", `price_unavailable` → "price unavailable",
  `temp_unavailable` → "temperature unavailable". Any other slug is shown
  as-is (forward-compatible with reasons this card doesn't know about yet).

## Forecast view

An SVG strip: a coloured band across the bottom shows which source is
cheapest per time slot; cost polylines for every source run above it,
scaled to the slots shown (outliers beyond `p95 × 1.3` are clamped so one
spike doesn't flatten the rest of the chart). Slots beyond the integration's
real price data (`estimated: true`) get a hatched overlay on the band and
dashed line segments. A dotted vertical marker plus a small **≈** shows
where estimated data begins; hour gridlines are drawn every 6h; a dashed
tick at the left edge marks "now".

Tap (or click) anywhere on the strip to see that slot's detail below it:
local time range, then each source's cost ascending (`name: X.X c`), tagged
`(estimated)` when applicable. Tap elsewhere to update it.

A legend below lists every known source as a coloured chip (colours are
assigned once per source and kept for the life of the card, so they don't
shuffle as the ranking re-sorts); the currently-cheapest source's chip is
bold.

If no forecast entity/data is available, the view shows a quiet "No
forecast available yet." note instead of the chart.

## Usage

Add a Manual card (or the card picker → "Live Heating Costs Card"):

```yaml
type: custom:live-heating-costs-card
```

That's the entire minimal config — both sensors and the title default and
auto-discover. A fuller example:

```yaml
type: custom:live-heating-costs-card
title: Heating costs
entity: sensor.house_cheapest_source        # optional; auto-discovered
forecast_entity: sensor.house_cost_forecast # optional; auto-discovered
hours: 48                                   # 24 (default) or 48
show_forecast: true                         # false hides the toggle + Forecast view entirely
```

## Config options

| Key               | Type    | Default    | Notes                                                                 |
|-------------------|---------|------------|------------------------------------------------------------------------|
| `entity`          | string  | auto       | The `…_cheapest_source` sensor. Auto-discovered: any `sensor.*` whose attributes have `integration: "live_heating_costs"` and a `ranking` array. |
| `forecast_entity` | string  | auto       | The `…_cost_forecast` sensor. Auto-discovered: any `sensor.*` whose attributes have `integration: "live_heating_costs"` and a `slots` array. |
| `title`           | string  | `"Heating costs"` | Card header text. |
| `hours`           | `24` \| `48` | `24`  | Forecast horizon shown (capped by however many slots the sensor actually has). |
| `show_forecast`   | boolean | `true`     | Set `false` to hide the Now/Forecast toggle and only ever show Now. |

## UI configuration

Add the card from the picker (or click **Edit** on it) to get a form: pick
the two sensors (filtered to this integration), set the title, forecast
horizon, and whether to show the Forecast view at all. YAML works exactly as
above.

## Sizing

The card declares grid options (`getGridOptions`) for Sections dashboards:
4 rows / 12 columns by default, resizable down to 3 rows / 6 columns.

## Data contract

The card renders directly off two sensors' attributes and treats every
field as possibly missing (the integration may still be starting up):

- **Main sensor** (`entity`) — state is the cheapest dispatchable source's
  name (or `"unknown"`); attributes: `ranking` (array, cheapest first — see
  each field's meaning in the Now-view section above), `excluded` (array of
  `{name, reason}`), `demand_kw`, `demand_covered`, `shortfall_kw`,
  `blended_cost_c_per_kwh`.
- **Forecast sensor** (`forecast_entity`) — attributes: `slots` (array of
  `{start, end, costs: {subentry_id: cost_c_per_kwh|null}, cheapest,
  estimated}`), `names` (`{subentry_id: display_name}`), `horizon_end`,
  `has_tomorrow`.

This contract is defined and owned by the integration's sensor platform, not
by the card — if a field is renamed there, update
`custom_components/live_heating_costs/frontend/live-heating-costs-card.js`
to match.

## Notes

- Plain JavaScript, no build step, one file:
  `custom_components/live_heating_costs/frontend/live-heating-costs-card.js`.
- No SVG/chart library dependency — the Forecast timeline is hand-drawn SVG
  scaled via `viewBox`, so it stays crisp at any card width.
- Colours are theme-aware throughout (HA CSS custom properties), so the card
  looks right in both light and dark themes with no hardcoded black/white.

![Now view screenshot placeholder](../docs/images/card-now.png)
![Forecast view screenshot placeholder](../docs/images/card-forecast.png)
