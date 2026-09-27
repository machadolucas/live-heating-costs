/*
 * Live Heating Costs card — a single dependency-free vanilla-JS Lovelace card:
 * `custom:live-heating-costs-card`.
 *
 * The integration ranks the house's heat sources (wood, resistive, heat pumps,
 * …) by delivered-heat cost every tick and exposes that as a `ranking` array
 * (plus `excluded` sources it couldn't cost) on one sensor, and a per-source
 * cost forecast (`slots`) on a sibling sensor. This card has two views,
 * switched by a small header toggle, and answers one question at a glance:
 * *what is the cheapest way to heat right now, and soon?*
 *
 *   - "Now"      — a single header row (title · demand pill · blended mix ·
 *                  view toggle) over a responsive grid of source rows: name,
 *                  live/measured chips, the share of demand it covers, its
 *                  dispatchable capacity ("max X kW", or "not dispatchable"
 *                  for a 0 kW source), COP when known, and the cost. The
 *                  excluded sources follow underneath.
 *   - "Forecast" — a pixel-true SVG timeline (sized by a ResizeObserver, so
 *                  nothing is stretched): a y axis in c/kWh, a solid "now"
 *                  line at the left edge, cost polylines per source, and a
 *                  bottom band coloured by the cheapest source per slot.
 *                  Static sources (wood, …) carry a flat constant cost and so
 *                  simply draw as flat lines. Slots the backend could only
 *                  partly cost (`partial` — a heat pump with no temperature
 *                  forecast) are drawn in the neutral divider colour, like a
 *                  slot with no cheapest source at all. Hovering the strip
 *                  updates the readout; tapping pins it.
 *
 * Rows, excluded rows, the demand pill, the mix label and the legend chips all
 * carry `data-more-info` and open the corresponding HA more-info dialog when
 * clicked (or on Enter/Space for the keyboard-focusable rows).
 *
 * Both the main ("…_cheapest_source") and forecast ("…_cost_forecast")
 * sensors are auto-discovered when `entity` / `forecast_entity` are omitted,
 * by scanning `hass.states` for the `integration: "live_heating_costs"`
 * marker attribute. See docs/card.md for the full data contract.
 */

const REASON_LABEL = {
  below_operating_limit: "below operating limit",
  price_unavailable: "price unavailable",
  temp_unavailable: "temperature unavailable",
};

// Small, high-contrast palette. Colours are assigned to subentry ids the
// first time they're seen (see LiveHeatingCostsCard#_colorFor) and then kept
// for the life of the card element, so a source's colour doesn't jump around
// as the ranking re-sorts by price.
const PALETTE = [
  "#4caf50",
  "#2196f3",
  "#ff9800",
  "#e91e63",
  "#9c27b0",
  "#00bcd4",
  "#795548",
  "#607d8b",
];

// Forecast SVG geometry. The width is measured (ResizeObserver) so the
// viewBox is 1:1 with CSS pixels — no `preserveAspectRatio="none"` stretching
// of text or stroke widths any more. Everything below is in those pixels.
const VB_H = 150; //  total svg height
const GUTTER = 30; //  left gutter reserved for the y-axis labels
const CHART_TOP = 18; //  top of the plotted area
const CHART_BOTTOM = 108; //  baseline of the plotted area
const BAND_TOP = 112; //  cheapest-source band
const BAND_H = 14;
const BAND_BOTTOM = BAND_TOP + BAND_H;
const AXIS_BASELINE = 145; //  hour-label text baseline
const FORECAST_PAD_X = 14; //  must match `.forecast` horizontal padding
const NOW_LABEL_CLEAR_PX = 40; //  hour labels closer than this to "now" are dropped

function fmt1(v) {
  const n = Number(v);
  return v == null || Number.isNaN(n) ? "—" : n.toFixed(1);
}

function esc(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

function reasonLabel(reason) {
  return REASON_LABEL[reason] || reason || "unknown";
}

function pad2(n) {
  return String(n).padStart(2, "0");
}

function fmtClock(d) {
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

// Trim float noise off a tick value: 5 → "5", 0.5 → "0.5", 10 → "10".
function fmtTick(v) {
  return String(Number(Number(v).toFixed(2)));
}

// Any `sensor.*` whose attributes carry the integration marker plus a
// `ranking` array. Prefers an entity_id ending `_cheapest_source` (the
// documented naming) but falls back to any match so a rename doesn't break
// auto-discovery.
function discoverMainEntity(hass) {
  if (!hass || !hass.states) return null;
  const candidates = Object.keys(hass.states)
    .filter((id) => id.startsWith("sensor."))
    .filter((id) => {
      const a = hass.states[id].attributes || {};
      return a.integration === "live_heating_costs" && Array.isArray(a.ranking);
    })
    .sort();
  if (!candidates.length) return null;
  return candidates.find((id) => id.endsWith("_cheapest_source")) || candidates[0];
}

// Sibling of discoverMainEntity: any `sensor.*` with the marker plus `slots`.
function discoverForecastEntity(hass) {
  if (!hass || !hass.states) return null;
  const candidates = Object.keys(hass.states)
    .filter((id) => id.startsWith("sensor."))
    .filter((id) => {
      const a = hass.states[id].attributes || {};
      return a.integration === "live_heating_costs" && Array.isArray(a.slots);
    })
    .sort();
  if (!candidates.length) return null;
  return candidates.find((id) => id.endsWith("_cost_forecast")) || candidates[0];
}

// The coordinator's rule for "cheapest": the first ranked source that can
// actually be dispatched. A 0 kW source (e.g. a sauna counted for UA learning
// only) can top the price ranking but is never planned for, so highlighting
// ranking[0] would disagree with the sensor's own state.
function cheapestRow(ranking) {
  return (Array.isArray(ranking) ? ranking : []).find((r) => r && r.capacity_kw > 0) || null;
}

// "Nice" gridline step for a value span: the {1, 2, 5}×10^n ladder, picking
// the first step that yields 2–4 intervals.
function niceStep(span) {
  if (!(span > 0)) return null;
  for (let e = -3; e <= 5; e++) {
    for (const m of [1, 2, 5]) {
      const s = m * Math.pow(10, e);
      const n = Math.floor(span / s + 1e-9);
      if (n >= 2 && n <= 4) return s;
    }
  }
  return span / 3;
}

function define(name, cls) {
  if (!customElements.get(name)) customElements.define(name, cls);
}

function registerCard(card) {
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === card.type)) {
    window.customCards.push(card);
  }
}

const CARD_CSS = `
  ha-card { padding: 0; overflow: hidden; }

  /* One header row: title · meta (demand + mix) · view toggle. On a narrow
     card the meta group and the toggle wrap under the title. */
  .head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px;
          padding: 10px 14px 6px; }
  .head .title { font-weight: 600; font-size: 1em; flex: 0 1 auto; min-width: 0;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 6px 8px; min-width: 0; }
  .viewtoggle { flex: 0 0 auto; margin-left: auto; display: inline-flex; border-radius: 12px;
          overflow: hidden; border: 1px solid var(--divider-color, rgba(127,127,127,0.4)); }
  .vbtn { padding: 3px 10px; font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.03em;
          cursor: pointer; user-select: none; color: var(--secondary-text-color); }
  .vbtn.active { background: var(--primary-color); color: var(--text-primary-color, #fff); }

  .pill { font-size: 0.74em; font-weight: 600; padding: 2px 9px; border-radius: 10px; white-space: nowrap; }
  .pill.ok { background: color-mix(in srgb, var(--success-color, #4caf50) 20%, transparent);
          color: var(--success-color, #4caf50); }
  .pill.bad { background: color-mix(in srgb, var(--error-color, #db4437) 18%, transparent);
          color: var(--error-color, #db4437); }
  .mix { font-size: 0.76em; color: var(--secondary-text-color); white-space: nowrap; }
  .hint { color: var(--secondary-text-color); padding: 10px 14px 14px; font-size: 0.85em; }

  /* Anything that opens a more-info dialog is clickable; an empty attribute
     (no entity known) deliberately keeps the default cursor. */
  [data-more-info]:not([data-more-info=""]) { cursor: pointer; }

  /* Two columns on a wide card, one when narrow — row-major, so the ranking
     still reads 1 2 / 3 4 / 5 6 cheapest-first. */
  .rows { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
          column-gap: 8px; }
  .row { display: flex; align-items: center; gap: 8px; min-width: 0; font-size: 0.86em;
          padding: 5px 12px; border-left: 3px solid transparent; }
  .row:hover { background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); }
  .row.cheapest { border-left-color: var(--primary-color);
          background: color-mix(in srgb, var(--primary-color) 7%, transparent); }
  .row.cheapest:hover { background: color-mix(in srgb, var(--primary-color) 14%, transparent); }
  .row .name { flex: 1 1 auto; min-width: 0; font-weight: 600; display: flex; align-items: center;
          gap: 5px; white-space: nowrap; overflow: hidden; }
  .row .name .label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: #ff9800; flex: 0 0 auto;
          animation: lhc-pulse 1.6s ease-in-out infinite; }
  .chip { font-size: 0.6em; text-transform: uppercase; letter-spacing: 0.03em; padding: 1px 5px;
          border-radius: 6px; background: var(--secondary-background-color, rgba(127,127,127,0.15));
          color: var(--secondary-text-color); flex: 0 0 auto; }
  .chip.ok { text-transform: none; letter-spacing: 0; font-size: 0.68em; font-weight: 600;
          background: color-mix(in srgb, var(--success-color, #4caf50) 20%, transparent);
          color: var(--success-color, #4caf50); }
  .cap, .cop { color: var(--secondary-text-color); font-size: 0.86em; white-space: nowrap; flex: 0 0 auto; }
  .cap.none { font-style: italic; opacity: 0.75; }
  .cost { font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; flex: 0 0 auto;
          min-width: 5.4em; text-align: right; }

  .excluded { padding: 4px 12px 10px; margin-top: 2px;
          border-top: 1px solid var(--divider-color, rgba(127,127,127,0.2)); }
  .exrow { display: flex; justify-content: space-between; gap: 8px; font-size: 0.76em;
          color: var(--secondary-text-color); line-height: 1.45; padding: 1px 0; }
  .exrow:hover { background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); }
  @keyframes lhc-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

  .forecast { padding: 6px ${FORECAST_PAD_X}px 12px; }
  svg.strip { display: block; width: 100%; height: ${VB_H}px; cursor: crosshair;
          touch-action: manipulation; }
  .axis-label { fill: var(--secondary-text-color); font-size: 11px; }
  .unit-label { fill: var(--secondary-text-color); font-size: 9px; }
  .now-label { fill: var(--primary-color); font-size: 11px; font-weight: 600; }
  .approx-label { fill: var(--secondary-text-color); font-size: 11px; }
  .readout { min-height: 2.4em; margin-top: 4px; font-size: 0.8em; color: var(--secondary-text-color); }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 6px; }
  .legchip { display: inline-flex; align-items: center; gap: 5px; font-size: 0.78em;
          color: var(--secondary-text-color); }
  .legchip.bold { font-weight: 700; color: var(--primary-text-color); }
  .swatch { width: 9px; height: 9px; border-radius: 50%; background: var(--c, var(--primary-color));
          display: inline-block; flex: 0 0 auto; }
`;

class LiveHeatingCostsCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._view = "now";
    this._tapped = null;
    this._pinned = false;
    this._colorMap = this._colorMap || new Map();
    this._uid = this._uid || Math.random().toString(36).slice(2, 8);
    this._sig = null;
    this._mainIdCache = null;
    this._fcIdCache = null;
    this._render();
  }

  // HA assigns a new `hass` on *every* state change anywhere in the house —
  // many times a second on a busy instance. _render() bails out cheaply
  // unless one of the two sensors this card reads actually changed.
  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  // The forecast SVG is drawn 1:1 with CSS pixels, so it has to know the
  // card's real width. A ResizeObserver is the only reliable way to learn it
  // (the card can be resized by the grid, the sidebar, or the window).
  connectedCallback() {
    if (typeof ResizeObserver === "undefined") return;
    if (!this._ro) {
      this._ro = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const box = entry.contentRect || {};
          this._onResize(box.width || 0);
        }
      });
    }
    try {
      this._ro.observe(this);
    } catch (err) {
      /* detached / unsupported — fall back to the default width */
    }
  }

  disconnectedCallback() {
    if (this._ro) this._ro.disconnect();
  }

  _onResize(outerWidth) {
    // An inline (or not-yet-laid-out) host reports 0×0 — that's "unknown",
    // not "tiny", so keep whatever width we had rather than collapsing.
    if (!(outerWidth > 0)) return;
    const w = Math.max(120, Math.round(outerWidth - FORECAST_PAD_X * 2));
    // Only re-render on a real change: the render itself can nudge layout, so
    // reacting to every observer callback would loop.
    if (!(w > 0) || w === this._width) return;
    this._width = w;
    this._render();
  }

  // Best-effort first measurement, for the tick before the observer fires.
  _measureOnce() {
    if (this._width != null) return;
    if (typeof this.getBoundingClientRect !== "function") return;
    const rect = this.getBoundingClientRect() || {};
    if (rect.width > 0) this._width = Math.max(120, Math.round(rect.width - FORECAST_PAD_X * 2));
  }

  getCardSize() {
    // Masonry fallback: header + the grid of ranked sources (two columns once
    // the card is wide enough for `minmax(330px, …)` to fit twice) + the
    // excluded block.
    const id = this._hass ? this._mainEntityId() : null;
    const a = (id && this._hass.states[id] && this._hass.states[id].attributes) || {};
    const rank = Array.isArray(a.ranking) ? a.ranking.length : 5;
    const excl = Array.isArray(a.excluded) && a.excluded.length ? 1 : 0;
    const cols = (this._width || 0) >= 690 ? 2 : 1;
    return 1 + Math.ceil(rank / cols) + excl;
  }

  // Sections (grid) dashboards: size to content. The Now view (one row per
  // source) and the Forecast view (fixed-height strip) have very different
  // heights, and the user toggles between them at runtime — a fixed `rows`
  // would clip/overflow one of them, so let the grid measure actual content.
  getGridOptions() {
    return { rows: "auto", columns: 12, min_columns: 6 };
  }

  static getConfigElement() {
    return document.createElement("live-heating-costs-card-editor");
  }

  static getStubConfig() {
    return {};
  }

  // Auto-discovery scans every state in the house, so the result is cached
  // and only redone when the cached entity disappears (removed/renamed).
  _mainEntityId() {
    if (this._config.entity) return this._config.entity;
    const states = (this._hass && this._hass.states) || {};
    if (!this._mainIdCache || !states[this._mainIdCache]) {
      this._mainIdCache = discoverMainEntity(this._hass);
    }
    return this._mainIdCache;
  }

  _forecastEntityId() {
    if (this._config.forecast_entity) return this._config.forecast_entity;
    const states = (this._hass && this._hass.states) || {};
    if (!this._fcIdCache || !states[this._fcIdCache]) {
      this._fcIdCache = discoverForecastEntity(this._hass);
    }
    return this._fcIdCache;
  }

  // Stable colour per subentry id: assigned once, in order of first sighting
  // (cheapest-first from the ranking, then any left over from the forecast's
  // `names`, then anything only a slot's `costs` mentions), then never
  // reassigned for the life of this card element.
  _colorFor(id) {
    if (!this._colorMap.has(id)) {
      this._colorMap.set(id, PALETTE[this._colorMap.size % PALETTE.length]);
    }
    return this._colorMap.get(id);
  }

  _ensureColors(mainState, forecastState) {
    const ranking = (mainState && mainState.attributes && mainState.attributes.ranking) || [];
    ranking.forEach((r) => this._colorFor(r.subentry_id));
    const fa = (forecastState && forecastState.attributes) || {};
    Object.keys(fa.names || {}).forEach((id) => this._colorFor(id));
    (Array.isArray(fa.slots) ? fa.slots : []).forEach((s) => {
      Object.keys(s.costs || {}).forEach((id) => this._colorFor(id));
    });
  }

  // subentry_id → the source's own heat-cost sensor, so a legend chip can open
  // the same more-info dialog its Now-view row does.
  _entityMap(mainState) {
    const map = new Map();
    const a = (mainState && mainState.attributes) || {};
    const add = (rows) => {
      (Array.isArray(rows) ? rows : []).forEach((r) => {
        if (r && r.subentry_id && r.entity_id) map.set(r.subentry_id, r.entity_id);
      });
    };
    add(a.ranking);
    add(a.excluded);
    return map;
  }

  // ---- Header -----------------------------------------------------------

  _demandPillHtml(a) {
    if (a.demand_kw == null) return "";
    const covered = a.demand_covered;
    const cls = covered ? "pill ok" : "pill bad";
    let text = `▲ ${fmt1(a.demand_kw)} kW demand`;
    if (!covered && a.shortfall_kw != null) text += ` · short ${fmt1(a.shortfall_kw)} kW`;
    const title =
      "House heat demand right now (heat-loss coefficient × temperature deficit, " +
      "or the external demand entity)";
    return `<span class="${cls}" data-more-info="${esc(a.demand_entity_id || "")}" title="${esc(
      title,
    )}">${text}</span>`;
  }

  _headHtml(mainState, showForecast) {
    const title = this._config.title || "Heating costs";
    const a = (mainState && mainState.attributes) || {};
    const mainId = this._mainEntityId();
    const pill = this._demandPillHtml(a);
    const mix =
      a.blended_cost_c_per_kwh != null
        ? `<span class="mix" data-more-info="${esc(
            mainId || "",
          )}" title="Demand-weighted average cost of the sources currently allocated">mix ${fmt1(
            a.blended_cost_c_per_kwh,
          )} c/kWh</span>`
        : "";
    const meta = pill || mix ? `<span class="meta">${pill}${mix}</span>` : "";
    return `<div class="head"><span class="title">${esc(title)}</span>${meta}${
      showForecast ? this._toggleHtml() : ""
    }</div>`;
  }

  // ---- Now view ---------------------------------------------------------

  _rowHtml(r, isCheapest) {
    const dot = r.active ? `<span class="dot" title="active"></span>` : "";
    const measured = r.measured_blend ? `<span class="chip">measured</span>` : "";
    const covers =
      r.allocated_kw > 0
        ? `<span class="chip ok" title="Share of the current demand assigned to this source (cheapest-first)">covers ${fmt1(
            r.allocated_kw,
          )} kW</span>`
        : "";
    // A 0 kW source still contributes measured heat, it just never gets
    // planned for — say so instead of printing a meaningless "0.0 kW".
    const cap =
      r.capacity_kw > 0
        ? `<span class="cap" title="Dispatchable heat output right now">max ${fmt1(
            r.capacity_kw,
          )} kW</span>`
        : `<span class="cap none" title="Configured with 0 kW capacity: its heat counts, but it is never planned for">not dispatchable</span>`;
    const cop = r.cop != null ? `<span class="cop">COP ${fmt1(r.cop)}</span>` : "";
    return `<div class="row${
      isCheapest ? " cheapest" : ""
    }" role="button" tabindex="0" data-more-info="${esc(r.entity_id || "")}">
      <span class="name"><span class="label">${esc(r.name)}</span>${dot}${measured}${covers}</span>
      ${cap}
      ${cop}
      <span class="cost">${fmt1(r.cost_c_per_kwh)} c/kWh</span>
    </div>`;
  }

  _nowHtml(mainState) {
    if (!mainState) {
      return `<div class="hint">No heating-cost sensor found — pick one in the card editor.</div>`;
    }
    const a = mainState.attributes || {};
    const ranking = Array.isArray(a.ranking) ? a.ranking : [];
    const excluded = Array.isArray(a.excluded) ? a.excluded : [];
    const cheapest = cheapestRow(ranking);
    const rows = ranking.length
      ? `<div class="rows">${ranking.map((r) => this._rowHtml(r, r === cheapest)).join("")}</div>`
      : `<div class="hint">No ranked sources right now.</div>`;
    const exclHtml = excluded.length
      ? `<div class="excluded">${excluded
          .map((x) => {
            const eid = esc(x.entity_id || "");
            const focusable = x.entity_id ? ` role="button" tabindex="0"` : "";
            return `<div class="exrow" data-more-info="${eid}"${focusable}><span>${esc(
              x.name,
            )}</span><span>${esc(reasonLabel(x.reason))}</span></div>`;
          })
          .join("")}</div>`
      : "";
    return `${rows}${exclHtml}`;
  }

  // ---- Forecast view ------------------------------------------------------

  _hours() {
    return this._config.hours === 48 ? 48 : 24;
  }

  // x in [GUTTER, VB_W] over [rangeStart, rangeEnd].
  _xScale(t, rangeStart, rangeEnd) {
    const span = rangeEnd - rangeStart;
    const frac = span > 0 ? (t - rangeStart) / span : 0;
    const clamped = Math.max(0, Math.min(1, frac));
    return GUTTER + clamped * (this._VB_W - GUTTER);
  }

  // Signed domain [yMin, yMax]: spot prices go negative, and a negative
  // cost clamped to the baseline would read as "free" instead of "paid to
  // consume". yMin is 0 unless some cost is below it.
  _yScale(cost, yMin, yMax) {
    const span = yMax - yMin;
    const clamped = Math.max(yMin, Math.min(cost, yMax));
    const frac = span > 0 ? (clamped - yMin) / span : 0;
    return CHART_BOTTOM - frac * (CHART_BOTTOM - CHART_TOP);
  }

  // "Nice" gridline values across [yMin, yMax]. With a 0 baseline (the usual
  // all-positive case) 0 itself is skipped — the baseline already marks it;
  // with a negative domain 0 is drawn so the sign change is visible.
  _yTicks(yMin, yMax) {
    const step = niceStep(yMax - yMin);
    if (step == null) return [];
    const ticks = [];
    const first = Math.ceil(yMin / step - 1e-9);
    for (let i = first; i * step <= yMax * 1.0001 + 1e-12; i++) {
      const v = i * step;
      if (v === 0 && yMin === 0) continue;
      ticks.push(Math.abs(v) < step * 1e-6 ? 0 : v);
      if (ticks.length > 8) break;
    }
    return ticks;
  }

  // Hour gridlines: denser on a wide strip, sparser on a phone-width one.
  _hourStep() {
    const w = this._VB_W || 800;
    if (w >= 700) return 3;
    if (w < 360) return 12;
    return 6;
  }

  _hourLabels(rangeStart, rangeEnd, stepHours) {
    const labels = [];
    let t = new Date(rangeStart);
    t.setMinutes(0, 0, 0);
    if (t <= rangeStart) t = new Date(t.getTime() + 3600000);
    let guard = 0;
    while (t.getHours() % stepHours !== 0 && guard++ < 48) {
      t = new Date(t.getTime() + 3600000);
    }
    while (t < rangeEnd) {
      labels.push(new Date(t));
      t = new Date(t.getTime() + stepHours * 3600000);
    }
    return labels;
  }

  _timelineSvg(shown, names, rangeStart, rangeEnd) {
    const W = this._VB_W;
    const H = VB_H;
    const hatchId = `lhc-hatch-${this._uid}`;
    const neutral = "var(--divider-color, #888)";

    // y-scale: clamp outliers at p95 * 1.3 so one spike doesn't flatten
    // every other line.
    const allCosts = [];
    shown.forEach((s) => {
      Object.values(s.costs || {}).forEach((v) => {
        if (v != null) allCosts.push(v);
      });
    });
    let yMax = 1;
    let yMin = 0;
    if (allCosts.length) {
      const sorted = [...allCosts].sort((x, y) => x - y);
      const p95 = sorted[Math.min(sorted.length - 1, Math.floor(0.95 * (sorted.length - 1)))];
      const actualMax = sorted[sorted.length - 1];
      const actualMin = sorted[0];
      if (actualMax > 0) {
        const clampMax = p95 > 0 ? p95 * 1.3 : actualMax;
        yMax = Math.min(actualMax, clampMax) || actualMax;
      } else {
        yMax = 0; // every cost is at or below zero
      }
      yMin = Math.min(0, actualMin);
      if (!(yMax > yMin)) yMax = yMin + 1;
      // Snap a negative floor down to a gridline so the lowest negative
      // cost sits against a labelled tick rather than an unlabelled edge.
      if (yMin < 0) {
        const step = niceStep(yMax - yMin);
        if (step) yMin = Math.floor(yMin / step + 1e-9) * step;
      }
    }

    const x = (t) => this._xScale(t, rangeStart, rangeEnd);
    const y = (c) => this._yScale(c, yMin, yMax);

    // y axis: faint gridlines across the plot, right-aligned labels in the
    // gutter, and one unit label above them.
    let yAxis = `<text x="0" y="${CHART_TOP - 6}" class="unit-label">c/kWh</text>`;
    this._yTicks(yMin, yMax).forEach((v) => {
      const ly = y(v);
      yAxis +=
        `<line x1="${GUTTER}" y1="${ly}" x2="${W}" y2="${ly}" stroke="var(--divider-color, rgba(127,127,127,0.2))" stroke-width="1"></line>` +
        `<text x="${GUTTER - 4}" y="${ly + 3.5}" class="axis-label" text-anchor="end">${fmtTick(
          v,
        )}</text>`;
    });

    // Bottom band, coloured per slot by its cheapest source. A `partial` slot
    // (a heat pump whose cost is unknown because there's no temperature
    // forecast) is as unknown as one with no cheapest at all → neutral.
    let band = "";
    let lastRealEnd = null;
    let anyEstimated = false;
    shown.forEach((s) => {
      const s0 = Math.max(new Date(s.start).getTime(), rangeStart.getTime());
      const s1 = Math.min(new Date(s.end).getTime(), rangeEnd.getTime());
      if (s1 <= s0) return;
      const x0 = x(s0);
      const x1 = x(s1);
      const color = s.cheapest && !s.partial ? this._colorFor(s.cheapest) : neutral;
      band += `<rect x="${x0}" y="${BAND_TOP}" width="${Math.max(
        0,
        x1 - x0,
      )}" height="${BAND_H}" fill="${color}"></rect>`;
      if (s.estimated) {
        anyEstimated = true;
        band += `<rect x="${x0}" y="${BAND_TOP}" width="${Math.max(
          0,
          x1 - x0,
        )}" height="${BAND_H}" fill="url(#${hatchId})" opacity="0.55"></rect>`;
      } else {
        lastRealEnd = lastRealEnd == null ? s1 : Math.max(lastRealEnd, s1);
      }
    });

    // Per-source cost polylines, split into segments so estimated slots can
    // be dashed independently and a gap (unknown cost) simply isn't drawn.
    // Static sources have a flat cost in every slot, so they draw as a flat
    // line with no special handling.
    let lines = "";
    for (const id of this._colorMap.keys()) {
      const color = this._colorMap.get(id);
      for (let i = 0; i < shown.length - 1; i++) {
        const c0 = (shown[i].costs || {})[id];
        const c1 = (shown[i + 1].costs || {})[id];
        if (c0 == null || c1 == null) continue;
        const x0 = x(new Date(shown[i].start).getTime());
        const x1 = x(new Date(shown[i + 1].start).getTime());
        const dashed = shown[i].estimated || shown[i + 1].estimated;
        lines += `<line x1="${x0}" y1="${y(c0)}" x2="${x1}" y2="${y(c1)}" stroke="${color}" stroke-width="1.6" fill="none" ${
          dashed ? 'stroke-dasharray="3,2"' : ""
        }></line>`;
      }
    }

    // Hour gridlines; the label is dropped when it would collide with "now".
    const stepHours = this._hourStep();
    const hourLabels = this._hourLabels(rangeStart, rangeEnd, stepHours)
      .map((d) => {
        const lx = x(d.getTime());
        const text =
          Math.abs(lx - GUTTER) < NOW_LABEL_CLEAR_PX
            ? ""
            : `<text x="${lx}" y="${AXIS_BASELINE}" class="axis-label" text-anchor="middle">${pad2(
                d.getHours(),
              )}:00</text>`;
        return (
          `<line x1="${lx}" y1="${CHART_TOP}" x2="${lx}" y2="${BAND_BOTTOM}" stroke="var(--divider-color, rgba(127,127,127,0.2))" stroke-width="1"></line>` +
          text
        );
      })
      .join("");

    // "Now" — a solid accent line at the left edge of the plot, labelled with
    // the wall clock so the timeline's origin is never ambiguous.
    const nowLine =
      `<line x1="${GUTTER}" y1="${CHART_TOP}" x2="${GUTTER}" y2="${BAND_BOTTOM}" stroke="var(--primary-color)" stroke-width="2"></line>` +
      `<text x="${GUTTER + 4}" y="${CHART_TOP - 6}" class="now-label">now ${esc(
        fmtClock(new Date()),
      )}</text>`;

    // End-of-real-data boundary, beyond which slots are extrapolated.
    let marker = "";
    if (anyEstimated && lastRealEnd != null) {
      const mx = x(lastRealEnd);
      const labelW = 58; // rough width of "≈ estimate" at 11px
      const fitsRight = mx + 4 + labelW <= W;
      marker =
        `<line x1="${mx}" y1="${CHART_TOP}" x2="${mx}" y2="${BAND_BOTTOM}" stroke="var(--secondary-text-color)" stroke-width="1" stroke-dasharray="1,2"></line>` +
        (fitsRight
          ? `<text x="${mx + 4}" y="${CHART_TOP + 12}" class="approx-label">≈ estimate</text>`
          : `<text x="${
              mx - 4
            }" y="${CHART_TOP + 12}" class="approx-label" text-anchor="end">≈ estimate</text>`);
    }

    return `<svg class="strip" viewBox="0 0 ${W} ${H}">
      <defs>
        <pattern id="${hatchId}" width="4" height="4" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
          <rect width="4" height="4" fill="transparent"></rect>
          <line x1="0" y1="0" x2="0" y2="4" stroke="rgba(255,255,255,0.9)" stroke-width="1"></line>
        </pattern>
      </defs>
      ${yAxis}
      ${hourLabels}
      ${band}
      ${lines}
      ${nowLine}
      ${marker}
    </svg>`;
  }

  _legendHtml(mainState, names, slots) {
    const ranking =
      (mainState && mainState.attributes && Array.isArray(mainState.attributes.ranking)
        ? mainState.attributes.ranking
        : []) || [];
    const cheapest = cheapestRow(ranking);
    const cheapestId = cheapest ? cheapest.subentry_id : null;
    const visible = new Set([
      ...Object.keys(names || {}),
      ...ranking.map((r) => r.subentry_id),
      ...(
        (mainState && mainState.attributes && mainState.attributes.excluded) ||
        []
      ).map((x) => x.subentry_id),
    ]);
    (Array.isArray(slots) ? slots : []).forEach((s) => {
      Object.keys(s.costs || {}).forEach((id) => visible.add(id));
    });
    const entities = this._entityMap(mainState);
    const chips = [...this._colorMap.entries()]
      .filter(([id]) => visible.has(id))
      .map(([id, color]) => {
        const label = (names && names[id]) || id;
        const bold = id === cheapestId ? " bold" : "";
        return `<span class="legchip${bold}" style="--c:${color}" data-more-info="${esc(
          entities.get(id) || "",
        )}"><span class="swatch"></span>${esc(label)}</span>`;
      })
      .join("");
    return chips ? `<div class="legend">${chips}</div>` : "";
  }

  _readoutHtml(names) {
    const slot = this._tapped;
    if (!slot) return `<div class="readout"></div>`;
    const range = `${fmtClock(new Date(slot.start))}–${fmtClock(new Date(slot.end))}`;
    const costs = slot.costs || {};
    const entries = Object.keys(costs)
      .filter((id) => costs[id] != null)
      .map((id) => ({ name: (names && names[id]) || id, cost: costs[id] }))
      .sort((p, q) => p.cost - q.cost);
    const line = entries.map((e) => `${esc(e.name)}: ${fmt1(e.cost)} c`).join(" · ");
    const est = slot.estimated ? " (estimated)" : "";
    const partial = slot.partial ? " · heat-pump cost unknown (no temperature forecast)" : "";
    return `<div class="readout">${range} — ${line || "no data"}${est}${partial}</div>`;
  }

  _forecastHtml(mainState, forecastState) {
    const a = forecastState && forecastState.attributes;
    const slots = a && Array.isArray(a.slots) ? a.slots : [];
    if (!forecastState || !slots.length) {
      return `<div class="hint">No forecast available yet.</div>`;
    }
    const names = a.names || {};
    const hours = this._hours();
    const now = new Date();
    const horizonEnd = new Date(now.getTime() + hours * 3600000);
    const shown = slots
      .filter((s) => new Date(s.end) > now && new Date(s.start) < horizonEnd)
      .sort((x, y) => new Date(x.start) - new Date(y.start));
    if (!shown.length) {
      return `<div class="hint">No forecast available yet.</div>`;
    }
    this._ensureColors(mainState, forecastState);
    const rangeStart = now;
    const lastEnd = new Date(shown[shown.length - 1].end);
    const rangeEnd = lastEnd < horizonEnd ? lastEnd : horizonEnd;
    // The tapped/hovered slot is an object from an *older* attribute
    // snapshot; re-resolve it by start so the readout shows today's costs,
    // and drop it once its slot has scrolled out of the window.
    if (this._tapped) {
      const fresh = shown.find((s) => s.start === this._tapped.start);
      if (fresh) {
        this._tapped = fresh;
      } else {
        this._tapped = null;
        this._pinned = false;
      }
    }
    this._VB_W = this._width || 800;
    // Cached so the click/hover handlers can map a pointer position back to a
    // slot without rebuilding the whole forecast.
    this._chartState = { shown, rangeStart, rangeEnd, vbW: this._VB_W };
    const svg = this._timelineSvg(shown, names, rangeStart, rangeEnd);
    const readout = this._readoutHtml(names);
    const legend = this._legendHtml(mainState, names, slots);
    return `<div class="forecast">${svg}${readout}${legend}</div>`;
  }

  // ---- Shared ---------------------------------------------------------

  _toggleHtml() {
    return `<span class="viewtoggle">
      <span class="vbtn${this._view === "now" ? " active" : ""}" data-view="now">Now</span>
      <span class="vbtn${this._view === "forecast" ? " active" : ""}" data-view="forecast">Forecast</span>
    </span>`;
  }

  _moreInfo(entityId) {
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        detail: { entityId },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // Map a viewport x back to the forecast slot under it. The viewBox is 1:1
  // with CSS pixels, but scale anyway so a zoomed/scaled card still works.
  _slotAt(strip, clientX) {
    if (!strip || !this._chartState) return null;
    const { shown, rangeStart, rangeEnd, vbW } = this._chartState;
    const rect = strip.getBoundingClientRect();
    if (!rect || !(rect.width > 0)) return null;
    const scale = vbW > 0 ? rect.width / vbW : 1;
    const px = scale > 0 ? (clientX - rect.left) / scale : 0;
    const plotW = vbW - GUTTER;
    const frac = plotW > 0 ? Math.max(0, Math.min(1, (px - GUTTER) / plotW)) : 0;
    const t = rangeStart.getTime() + frac * (rangeEnd.getTime() - rangeStart.getTime());
    return (
      shown.find((s) => t >= new Date(s.start).getTime() && t < new Date(s.end).getTime()) ||
      shown[shown.length - 1] ||
      null
    );
  }

  _onClick(e) {
    const vbtn = e.target.closest("[data-view]");
    if (vbtn) {
      this._view = vbtn.dataset.view;
      this._sig = null;
      this._render();
      return;
    }
    const mi = e.target.closest("[data-more-info]");
    if (mi && mi.dataset && mi.dataset.moreInfo) {
      this._moreInfo(mi.dataset.moreInfo);
      return;
    }
    const strip = e.target.closest("svg.strip");
    if (strip) {
      const slot = this._slotAt(strip, e.clientX);
      if (!slot) return;
      // Tapping the already-pinned slot unpins it and hands control back to
      // hover; tapping a different one moves the pin.
      if (this._pinned && this._tapped && this._tapped.start === slot.start) {
        this._pinned = false;
      } else {
        this._tapped = slot;
        this._pinned = true;
      }
      this._sig = null;
      this._render();
    }
  }

  _onPointerMove(e) {
    if (this._pinned) return;
    const strip = e.target.closest && e.target.closest("svg.strip");
    if (!strip) return;
    const slot = this._slotAt(strip, e.clientX);
    if (!slot || (this._tapped && this._tapped.start === slot.start)) return;
    this._tapped = slot;
    this._render();
  }

  _onKeyDown(e) {
    if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
    const target = e.target.closest && e.target.closest("[role=button]");
    if (!target || !target.dataset || !target.dataset.moreInfo) return;
    if (e.preventDefault) e.preventDefault();
    this._moreInfo(target.dataset.moreInfo);
  }

  // Everything a render depends on. HA replaces a state object whenever that
  // entity changes and keeps the same object otherwise, so comparing the two
  // state objects by identity is exact — no JSON.stringify of the (large)
  // ranking/slots attributes on every hass update.
  _signature() {
    const mainId = this._mainEntityId();
    const fcId = this._forecastEntityId();
    return [
      this._config,
      this._view,
      Math.floor(Date.now() / 60000),
      Math.round(this._width || 0),
      mainId ? this._hass.states[mainId] : null,
      fcId ? this._hass.states[fcId] : null,
      this._tapped ? this._tapped.start : "",
      this._pinned,
    ];
  }

  _sameSignature(a, b) {
    return !!a && !!b && a.length === b.length && a.every((v, i) => v === b[i]);
  }

  // The more-info key of the focused element inside this card, if any.
  _focusedKey() {
    const root = typeof this.getRootNode === "function" ? this.getRootNode() : null;
    const active = (root && root.activeElement) || document.activeElement;
    if (!active || !this._content || !this._content.contains(active)) return null;
    return active.dataset ? active.dataset.moreInfo || null : null;
  }

  _render() {
    if (!this._hass || !this._config) return;
    this._measureOnce();
    if (!this._card) {
      // Custom elements are `display: inline` by default, and an inline box
      // has no measurable width (ResizeObserver reports 0) — the forecast
      // SVG is sized from this element, so it must be block-level.
      if (this.style) this.style.display = "block";
      this._card = document.createElement("ha-card");
      this.appendChild(this._card);
      // The stylesheet is created once; only the content container below is
      // re-rendered, so the browser doesn't reparse the CSS on every update.
      const style = document.createElement("style");
      style.textContent = CARD_CSS;
      this._card.appendChild(style);
      this._content = document.createElement("div");
      this._card.appendChild(this._content);
      this._card.addEventListener("click", (e) => this._onClick(e));
      this._card.addEventListener("pointermove", (e) => this._onPointerMove(e));
      this._card.addEventListener("keydown", (e) => this._onKeyDown(e));
    }
    const sig = this._signature();
    if (this._sameSignature(sig, this._sig)) return;
    this._sig = sig;

    const showForecast = this._config.show_forecast !== false;
    if (!showForecast) this._view = "now";

    const mainId = this._mainEntityId();
    const mainState = mainId ? this._hass.states[mainId] : null;
    const fcId = this._forecastEntityId();
    const fcState = fcId ? this._hass.states[fcId] : null;

    const head = this._headHtml(mainState, showForecast);
    const body =
      this._view === "forecast"
        ? this._forecastHtml(mainState, fcState)
        : this._nowHtml(mainState);

    // Re-rendering replaces the focusable rows, which would throw keyboard
    // focus back to <body> on every minute tick; put it back on the element
    // for the same more-info target.
    const focusKey = this._focusedKey();
    this._content.innerHTML = `${head}${body}`;
    if (focusKey) {
      const target = [...this._content.querySelectorAll("[tabindex]")].find(
        (el) => el.dataset && el.dataset.moreInfo === focusKey,
      );
      if (target && typeof target.focus === "function") target.focus();
    }
  }
}

/* ------------------------------------------------------------------ *
 * UI editor
 * ------------------------------------------------------------------ */

class LiveHeatingCostsCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _schema() {
    return [
      {
        name: "entity",
        selector: { entity: { filter: { integration: "live_heating_costs", domain: "sensor" } } },
      },
      {
        name: "forecast_entity",
        selector: { entity: { filter: { integration: "live_heating_costs", domain: "sensor" } } },
      },
      { name: "title", selector: { text: {} } },
      {
        name: "hours",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              { value: "24", label: "24 hours" },
              { value: "48", label: "48 hours" },
            ],
          },
        },
      },
      { name: "show_forecast", selector: { boolean: {} } },
    ];
  }

  _labels() {
    return {
      entity: "Cheapest-source sensor (auto if empty)",
      forecast_entity: "Forecast sensor (auto if empty)",
      title: "Title (optional)",
      hours: "Forecast horizon",
      show_forecast: "Show the Forecast view",
    };
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this.appendChild(this._form);
      this._form.computeLabel = (s) => this._labels()[s.name] || s.name;
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        const next = { ...ev.detail.value };
        if (next.hours !== undefined) next.hours = Number(next.hours) === 48 ? 48 : 24;
        if (JSON.stringify(next) === JSON.stringify(this._config)) return;
        this._config = next;
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config: next },
            bubbles: true,
            composed: true,
          }),
        );
      });
    }
    this._form.hass = this._hass;
    this._form.schema = this._schema();
    this._form.data = {
      ...this._config,
      hours: String(this._config.hours === 48 ? 48 : 24),
      show_forecast: this._config.show_forecast !== false,
    };
  }
}

define("live-heating-costs-card", LiveHeatingCostsCard);
define("live-heating-costs-card-editor", LiveHeatingCostsCardEditor);

registerCard({
  type: "live-heating-costs-card",
  name: "Live Heating Costs Card",
  description: "Compare heat sources by delivered-heat cost, now and forecast.",
});
