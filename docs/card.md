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

Nearly everything clickable on the card — rows, excluded rows, the demand
pill, the mix label, legend chips — opens the relevant sensor's HA more-info
dialog (click, or Enter/Space when focused). Every value also carries a
`title` tooltip explaining what it means, so the card teaches its own
vocabulary without a legend of its own.

## Now view

```
Heating costs                          ▲ 3.2 kW demand  mix 1.9 c/kWh  [Now] Forecast
────────────────────────────────────────────────────────────────────────────────────
RW35VG ● measured covers 3.2 kW  max 5.1 kW  COP 2.9  1.8 c/kWh │ FD25VA  max 2.5 kW  COP 2.1  2.4 c/kWh
Resistive backup  max 9.0 kW  COP 1.0  9.2 c/kWh                │ Sauna  not dispatchable  14.5 c/kWh
────────────────────────────────────────────────────────────────────────────────────
Wood stove                                                    below operating limit
```

- The **header is a single flex row**: title, then a "meta" group (demand
  pill + mix label), then the Now/Forecast toggle pushed to the right — on a
  narrow card the meta group and the toggle wrap onto their own line rather
  than clipping the title.
- The **demand pill** (▲ *X.X* kW demand) is green when `demand_covered` is
  true, red with `· short X.X kW` when it's false, and hidden entirely when
  `demand_kw` is `null`. It opens the heating-demand sensor's more-info.
- **mix *X.X* c/kWh** — the blended cost across all allocated sources — shows
  next to the pill when `blended_cost_c_per_kwh` is present, and opens the
  main (cheapest-source) sensor's more-info.
- Rows sit in a **responsive CSS grid** (`repeat(auto-fill, minmax(330px,
  1fr))`): two columns once the card is wide enough for two 330px cells,
  one on a narrow card. The grid fills row-major, so the ranking still reads
  cheapest-first left-to-right, top-to-bottom (1 2 / 3 4 / 5 6 …), not
  column-by-column.
- Each row shows, in order: name, a small pulsing dot when `active`, a
  **measured** chip when `measured_blend` is true (its cost came from a live
  measured blend, not the static curve), a green **covers *X.X* kW** chip
  when `allocated_kw > 0` (this **replaced** the old allocation progress
  bar — a chip reads better in a two-column grid than a thin bar ever did),
  the dispatchable **max *X.X* kW** capacity (or a muted *italic* **not
  dispatchable** when `capacity_kw` is 0 — e.g. a sauna deliberately
  configured with 0 kW: its heat still counts toward UA learning, it's just
  never planned for), **COP *Y*** when known (hidden, never faked as 1.0,
  when `cop` is `null`), and the bold cost to 1 decimal.
- The cheapest row — the first ranking row with `capacity_kw > 0`, the same
  rule the integration uses for its `cheapest` state (a cheaper 0 kW source
  can top the ranking but is never dispatched) — gets an accent left-border
  and tinted background.
- **Excluded** sources (cost couldn't be computed this tick) are listed at
  the bottom, greyed, with a human reason: `below_operating_limit` → "below
  operating limit", `price_unavailable` → "price unavailable",
  `temp_unavailable` → "temperature unavailable". Any other slug is shown
  as-is (forward-compatible with reasons this card doesn't know about yet).
  An excluded row is clickable/focusable too, whenever its `entity_id` is
  known.

## Forecast view

An SVG strip, sized **pixel-true**: a `ResizeObserver` measures the card's
actual CSS width and the `viewBox` is set to that real width × 150, so
nothing is stretched or scaled — text stays 11px and strokes stay crisp at
any card size, on any screen.

- A **y axis** in the left gutter shows 2–4 "nice" gridlines (the usual
  {1, 2, 5}×10ⁿ ladder) labelled in c/kWh, with a small unit label above them.
  The scale is signed: when a slot is negatively priced (spot prices do go
  below zero) the domain extends below 0, a 0 gridline is drawn, and the
  negative ticks are labelled with their sign rather than clamped to the
  baseline.
- A solid, primary-coloured vertical line at the left edge of the plot marks
  **"now HH:MM"** (the wall clock), so the timeline's origin is never
  ambiguous.
- Cost polylines run for every source, scaled to the slots shown (outliers
  beyond `p95 × 1.3` are clamped so one spike doesn't flatten the rest of the
  chart). **Static sources** (wood, sauna) have a flat, time-invariant cost
  and simply draw as flat lines alongside the dynamic ones.
- The bottom band is coloured per slot by whichever source is cheapest there
  (including a static source, if its known flat cost and nonzero capacity
  beat everything else). A slot flagged **`partial`** — a heat pump the
  weather forecast can't reach yet, so its cost there is unknown rather than
  merely high — draws in the neutral divider colour, the same as a slot with
  no cheapest source at all: an honest "don't know", not a colour that looks
  like a real answer.
- Slots beyond the integration's real price data (`estimated: true`) get a
  hatched overlay on the band and dashed line segments; a dotted vertical
  marker plus a small **≈ estimate** label marks where that boundary falls.
  Hour gridlines are drawn every 3h/6h/12h depending on how wide the strip
  is (denser on a wide card, sparser on a phone), with a label dropped
  whenever it would collide with the "now" line.
- The **readout** below the strip follows the pointer on hover, updating
  live; tapping (or clicking) the strip **pins** it to that slot — tap the
  pinned slot again to unpin and hand control back to hover. It shows the
  slot's local time range, then each source's cost ascending (`name: X.X
  c`), and appends `(estimated)` and/or `heat-pump cost unknown (no
  temperature forecast)` when applicable.
- A **legend** below lists every known source as a coloured, clickable chip
  (colours are assigned once per source and kept for the life of the card,
  so they don't shuffle as the ranking re-sorts); the currently-cheapest
  source's chip (same rule as the Now view's highlighted row) is bold. Clicking a chip opens that source's more-info, same
  as clicking its Now-view row.

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

The card declares grid options (`getGridOptions`) for Sections dashboards
with **`rows: "auto"`** so it sizes to its content — the Now view (a grid of
ranked sources, one or two columns depending on card width) and the Forecast
view (a fixed-height strip) differ in height and the user toggles between
them at runtime, so a fixed row count would clip or overflow one of them.
`getCardSize` (the masonry fallback) accounts for the two-column grid: it
divides the ranking length by the measured column count (two once the card
is wide enough for `minmax(330px, …)` to fit twice, otherwise one) before
adding the header and excluded-block rows. Width defaults to 12 columns,
resizable down to 6.

## Data contract

The card renders directly off two sensors' attributes and treats every
field as possibly missing (the integration may still be starting up). All
entity ids below are resolved **server-side**, from the entity registry —
the card never parses a name or subentry id into a guessed entity id.

- **Main sensor** (`entity`) — state is the cheapest dispatchable source's
  name (or `"unknown"`); attributes: `ranking` (array, cheapest first — see
  each field's meaning in the Now-view section above, including `entity_id`,
  the source's own heat-cost sensor, used to open its more-info), `excluded`
  (array of `{name, subentry_id, reason, entity_id}` — `subentry_id` keys the
  source's colour and its forecast-legend chip, which an excluded source can
  still have), `demand_kw`, `demand_covered`,
  `shortfall_kw`, `blended_cost_c_per_kwh`, `demand_entity_id` (the
  heating-demand sensor, for the demand pill's more-info), `shadow` (the optional whole-house model snapshot) and `shadow_entity_id` (resolved estimated requirement sensor). The shadow pill shows total and remaining kW separately; unknown remaining heat stays unknown. `shadow.active` is false until independent calibration and chronological validation pass. The snapshot also exposes `expected_delivery_kw`, quality reasons, source provenance, airflow bounds, and validation evidence.
- **Forecast sensor** (`forecast_entity`) — attributes: `slots` (array of
  `{start, end, costs: {subentry_id: cost_c_per_kwh|null}, cheapest,
  estimated, partial}`), `names` (`{subentry_id: display_name}`),
  `horizon_end`, `has_tomorrow`. `costs` may include static-source ids
  (wood, sauna) alongside dynamic ones — their cost is constant across
  slots — and `cheapest` may name a static source when its flat cost and
  nonzero capacity beat every dynamic source that slot. `partial` is true
  when a source that has forecast slots elsewhere is missing from this one
  (its cost there is unknown, not merely absent from the comparison).

Both sensors' attributes are rebuilt by the integration every minute, and
HA hands the card a new `hass` object on *any* state change in the house.
The card therefore caches the discovered entity ids (re-scanning
`hass.states` only when a cached id disappears), re-renders only when the
two sensors' state objects change identity (HA replaces the object on every
update, so a reference comparison is exact and O(1)), keeps its `<style>` in
a persistent element and swaps only the content container, restores
keyboard focus to the same `data-more-info` element after a re-render, and
re-resolves a pinned forecast slot by its `start` so the readout always
shows the current costs rather than a snapshot from when it was tapped.

This contract is defined and owned by the integration's sensor platform, not
by the card — if a field is renamed there, update
`custom_components/live_heating_costs/frontend/live-heating-costs-card.js`
to match.

## Notes

The comfort-target number defaults to 21 °C and does not change pump setpoints.
See [Demand model](demand-model.md) for optional configuration and commissioning.


- Plain JavaScript, no build step, one file:
  `custom_components/live_heating_costs/frontend/live-heating-costs-card.js`.
- No SVG/chart library dependency — the Forecast timeline is hand-drawn SVG,
  measured pixel-true via `ResizeObserver` rather than stretched to fit, so
  it stays crisp at any card width.
- Colours are theme-aware throughout (HA CSS custom properties), so the card
  looks right in both light and dark themes with no hardcoded black/white.

![Now view screenshot placeholder](../docs/images/card-now.png)
![Forecast view screenshot placeholder](../docs/images/card-forecast.png)
