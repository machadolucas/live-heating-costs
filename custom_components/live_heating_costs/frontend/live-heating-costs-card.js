/*
 * Live Heating Costs card — a single dependency-free vanilla-JS Lovelace card:
 * `custom:live-heating-costs-card`.
 *
 * The integration ranks the house's heat sources (wood, resistive, heat pumps,
 * …) by delivered-heat cost every tick and exposes that as a `ranking` array
 * (plus `excluded` sources it couldn't cost) on one sensor, and a per-source
 * cost forecast (`slots`) on a sibling sensor. This card has two views,
 * switched by a small header toggle:
 *
 *   - "Now"      — one row per ranked source (capacity, COP, cost, live
 *                  allocation bar), a demand pill, and the excluded sources.
 *   - "Forecast" — an SVG timeline: a bottom band coloured by the cheapest
 *                  source per slot, cost polylines per source above it, and a
 *                  legend. Tapping the strip shows a per-slot cost readout.
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
  .head { display: flex; align-items: center; justify-content: space-between; gap: 8px;
          padding: 12px 14px 4px; }
  .head .title { font-weight: 600; font-size: 1em; min-width: 0; overflow: hidden;
          text-overflow: ellipsis; white-space: nowrap; }
  .viewtoggle { flex: 0 0 auto; display: inline-flex; border-radius: 12px; overflow: hidden;
          border: 1px solid var(--divider-color, rgba(127,127,127,0.4)); }
  .vbtn { padding: 3px 10px; font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.03em;
          cursor: pointer; user-select: none; color: var(--secondary-text-color); }
  .vbtn.active { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .sub { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 2px 14px 8px; }
  .pill { font-size: 0.74em; font-weight: 600; padding: 2px 9px; border-radius: 10px; white-space: nowrap; }
  .pill.ok { background: color-mix(in srgb, var(--success-color, #4caf50) 20%, transparent);
          color: var(--success-color, #4caf50); }
  .pill.bad { background: color-mix(in srgb, var(--error-color, #db4437) 18%, transparent);
          color: var(--error-color, #db4437); }
  .mix { font-size: 0.76em; color: var(--secondary-text-color); }
  .hint { color: var(--secondary-text-color); padding: 10px 14px 14px; font-size: 0.85em; }
  .rows { display: flex; flex-direction: column; }
  .row { padding: 6px 14px; border-left: 3px solid transparent; }
  .row.cheapest { border-left-color: var(--primary-color);
          background: color-mix(in srgb, var(--primary-color) 7%, transparent); }
  .row-top { display: flex; align-items: center; gap: 8px; font-size: 0.86em; }
  .row-top .name { flex: 1 1 auto; min-width: 0; font-weight: 600; display: flex; align-items: center;
          gap: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: #ff9800; flex: 0 0 auto;
          animation: lhc-pulse 1.6s ease-in-out infinite; }
  .chip { font-size: 0.6em; text-transform: uppercase; letter-spacing: 0.03em; padding: 1px 5px;
          border-radius: 6px; background: var(--secondary-background-color, rgba(127,127,127,0.15));
          color: var(--secondary-text-color); flex: 0 0 auto; }
  .cap, .cop { color: var(--secondary-text-color); font-size: 0.86em; white-space: nowrap; flex: 0 0 auto; }
  .cost { font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; flex: 0 0 auto;
          min-width: 5.4em; text-align: right; }
  .alloc-track { margin-top: 4px; height: 4px; border-radius: 2px;
          background: var(--divider-color, rgba(127,127,127,0.25)); overflow: hidden; }
  .alloc-bar { height: 100%; border-radius: 2px;
          background: color-mix(in srgb, var(--primary-color) 32%, transparent); }
  .alloc-fill { height: 100%; border-radius: 2px; background: var(--primary-color); }
  .excluded { padding: 6px 14px 12px; margin-top: 4px;
          border-top: 1px solid var(--divider-color, rgba(127,127,127,0.2)); }
  .exrow { display: flex; justify-content: space-between; gap: 8px; font-size: 0.76em;
          color: var(--secondary-text-color); line-height: 1.5; }
  @keyframes lhc-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

  .forecast { padding: 6px 14px 12px; }
  svg.strip { display: block; width: 100%; height: 110px; cursor: crosshair; touch-action: manipulation; }
  .axis-label { fill: var(--secondary-text-color); font-size: 9px; }
  .approx-label { fill: var(--secondary-text-color); font-size: 10px; }
  .readout { min-height: 1.3em; margin-top: 6px; font-size: 0.78em; color: var(--secondary-text-color); }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 8px; }
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
    this._colorMap = this._colorMap || new Map();
    this._uid = this._uid || Math.random().toString(36).slice(2, 8);
    this._sig = null;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    // Masonry fallback: estimate height units from the current content
    // (header + demand/mix line + one per ranked source + an excluded line).
    const id = this._hass ? this._mainEntityId() : null;
    const a = (id && this._hass.states[id] && this._hass.states[id].attributes) || {};
    const rank = Array.isArray(a.ranking) ? a.ranking.length : 5;
    const excl = Array.isArray(a.excluded) && a.excluded.length ? 1 : 0;
    return 2 + rank + excl;
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

  _mainEntityId() {
    return this._config.entity || discoverMainEntity(this._hass);
  }

  _forecastEntityId() {
    return this._config.forecast_entity || discoverForecastEntity(this._hass);
  }

  // Stable colour per subentry id: assigned once, in order of first sighting
  // (cheapest-first from the ranking, then any left over from the forecast's
  // `names`), then never reassigned for the life of this card element.
  _colorFor(id) {
    if (!this._colorMap.has(id)) {
      this._colorMap.set(id, PALETTE[this._colorMap.size % PALETTE.length]);
    }
    return this._colorMap.get(id);
  }

  _ensureColors(mainState, forecastState) {
    const ranking = (mainState && mainState.attributes && mainState.attributes.ranking) || [];
    ranking.forEach((r) => this._colorFor(r.subentry_id));
    const names = (forecastState && forecastState.attributes && forecastState.attributes.names) || {};
    Object.keys(names).forEach((id) => this._colorFor(id));
  }

  // ---- Now view ---------------------------------------------------------

  _demandPillHtml(a) {
    if (a.demand_kw == null) return "";
    const covered = a.demand_covered;
    const cls = covered ? "pill ok" : "pill bad";
    let text = `▲ ${fmt1(a.demand_kw)} kW demand`;
    if (!covered && a.shortfall_kw != null) text += ` · short ${fmt1(a.shortfall_kw)} kW`;
    return `<span class="${cls}">${text}</span>`;
  }

  _allocBarHtml(r) {
    const share = Math.max(0, Math.min(1, r.allocated_fraction || 0)) * 100;
    const util =
      r.capacity_kw > 0 ? Math.max(0, Math.min(1, r.allocated_kw / r.capacity_kw)) * 100 : 0;
    return (
      `<div class="alloc-track"><div class="alloc-bar" style="width:${share}%">` +
      `<div class="alloc-fill" style="width:${util}%"></div></div></div>`
    );
  }

  _rowHtml(r, isCheapest) {
    const dot = r.active ? `<span class="dot" title="active"></span>` : "";
    const measured = r.measured_blend ? `<span class="chip">measured</span>` : "";
    const cop = r.cop != null ? `<span class="cop">COP ${fmt1(r.cop)}</span>` : "";
    const bar = r.allocated_kw > 0 ? this._allocBarHtml(r) : "";
    return `<div class="row${isCheapest ? " cheapest" : ""}">
      <div class="row-top">
        <span class="name">${esc(r.name)}${dot}${measured}</span>
        <span class="cap">${fmt1(r.capacity_kw)} kW</span>
        ${cop}
        <span class="cost">${fmt1(r.cost_c_per_kwh)} c/kWh</span>
      </div>
      ${bar}
    </div>`;
  }

  _nowHtml(mainState) {
    if (!mainState) {
      return `<div class="hint">No heating-cost sensor found — pick one in the card editor.</div>`;
    }
    const a = mainState.attributes || {};
    const ranking = Array.isArray(a.ranking) ? a.ranking : [];
    const excluded = Array.isArray(a.excluded) ? a.excluded : [];
    const demandPill = this._demandPillHtml(a);
    const mix =
      a.blended_cost_c_per_kwh != null
        ? `<span class="mix">mix ${fmt1(a.blended_cost_c_per_kwh)} c/kWh</span>`
        : "";
    const sub = demandPill || mix ? `<div class="sub">${demandPill}${mix}</div>` : "";
    const rows = ranking.length
      ? `<div class="rows">${ranking.map((r, i) => this._rowHtml(r, i === 0)).join("")}</div>`
      : `<div class="hint">No ranked sources right now.</div>`;
    const exclHtml = excluded.length
      ? `<div class="excluded">${excluded
          .map(
            (x) =>
              `<div class="exrow"><span>${esc(x.name)}</span><span>${esc(
                reasonLabel(x.reason),
              )}</span></div>`,
          )
          .join("")}</div>`
      : "";
    return `${sub}${rows}${exclHtml}`;
  }

  // ---- Forecast view ------------------------------------------------------

  _hours() {
    return this._config.hours === 48 ? 48 : 24;
  }

  // x in [0, VB_W] over [rangeStart, rangeEnd].
  _xScale(t, rangeStart, rangeEnd) {
    const span = rangeEnd - rangeStart;
    const frac = span > 0 ? (t - rangeStart) / span : 0;
    return Math.max(0, Math.min(this._VB_W, frac * this._VB_W));
  }

  _yScale(cost, yMax) {
    const clamped = Math.max(0, Math.min(cost, yMax));
    const frac = yMax > 0 ? clamped / yMax : 0;
    return this._CHART_BOTTOM - frac * (this._CHART_BOTTOM - this._CHART_TOP);
  }

  _hourLabels(rangeStart, rangeEnd) {
    const labels = [];
    let t = new Date(rangeStart);
    t.setMinutes(0, 0, 0);
    if (t <= rangeStart) t = new Date(t.getTime() + 3600000);
    while (t.getHours() % 6 !== 0) t = new Date(t.getTime() + 3600000);
    while (t < rangeEnd) {
      labels.push(new Date(t));
      t = new Date(t.getTime() + 6 * 3600000);
    }
    return labels;
  }

  _timelineSvg(shown, names, rangeStart, rangeEnd) {
    const W = this._VB_W;
    const H = this._VB_H;
    const CT = this._CHART_TOP;
    const CB = this._CHART_BOTTOM;
    const BT = this._BAND_TOP;
    const BH = this._BAND_H;
    const BB = BT + BH;
    const hatchId = `lhc-hatch-${this._uid}`;

    // y-scale: clamp outliers at p95 * 1.3 so one spike doesn't flatten
    // every other line.
    const allCosts = [];
    shown.forEach((s) => {
      Object.values(s.costs || {}).forEach((v) => {
        if (v != null) allCosts.push(v);
      });
    });
    let yMax = 1;
    if (allCosts.length) {
      const sorted = [...allCosts].sort((x, y) => x - y);
      const p95 = sorted[Math.min(sorted.length - 1, Math.floor(0.95 * (sorted.length - 1)))];
      const actualMax = sorted[sorted.length - 1];
      const clampMax = p95 * 1.3 || actualMax || 1;
      yMax = Math.min(actualMax, clampMax) || clampMax || 1;
      if (!(yMax > 0)) yMax = 1;
    }

    const x = (t) => this._xScale(t, rangeStart, rangeEnd);
    const y = (c) => this._yScale(c, yMax);

    // Bottom band, coloured per slot by its cheapest source.
    let band = "";
    let lastRealEnd = null;
    let anyEstimated = false;
    shown.forEach((s) => {
      const s0 = Math.max(new Date(s.start).getTime(), rangeStart.getTime());
      const s1 = Math.min(new Date(s.end).getTime(), rangeEnd.getTime());
      if (s1 <= s0) return;
      const x0 = x(s0);
      const x1 = x(s1);
      const color = s.cheapest ? this._colorFor(s.cheapest) : "var(--divider-color, #888)";
      band += `<rect x="${x0}" y="${BT}" width="${Math.max(0, x1 - x0)}" height="${BH}" fill="${color}"></rect>`;
      if (s.estimated) {
        anyEstimated = true;
        band += `<rect x="${x0}" y="${BT}" width="${Math.max(0, x1 - x0)}" height="${BH}" fill="url(#${hatchId})" opacity="0.55"></rect>`;
      } else {
        lastRealEnd = lastRealEnd == null ? s1 : Math.max(lastRealEnd, s1);
      }
    });

    // Per-source cost polylines, split into segments so estimated slots can
    // be dashed independently.
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

    // "Now" tick at the left edge; hour gridlines every 6h; end-of-real-data
    // marker (with a small "≈") beyond which slots are estimated.
    const nowTick = `<line x1="0" y1="${CT}" x2="0" y2="${BB}" class="axis-label" stroke="var(--primary-color)" stroke-dasharray="2,2"></line>`;
    const hourLabels = this._hourLabels(rangeStart, rangeEnd)
      .map((d) => {
        const lx = x(d.getTime());
        return (
          `<line x1="${lx}" y1="${CT}" x2="${lx}" y2="${BB}" stroke="var(--divider-color, rgba(127,127,127,0.2))" stroke-width="1"></line>` +
          `<text x="${lx}" y="${H - 3}" class="axis-label" text-anchor="middle">${pad2(d.getHours())}:00</text>`
        );
      })
      .join("");
    let marker = "";
    if (anyEstimated && lastRealEnd != null) {
      const mx = x(lastRealEnd);
      marker =
        `<line x1="${mx}" y1="${CT}" x2="${mx}" y2="${BB}" stroke="var(--secondary-text-color)" stroke-width="1" stroke-dasharray="1,2"></line>` +
        `<text x="${mx + 4}" y="${CT + 9}" class="approx-label">≈</text>`;
    }

    return `<svg class="strip" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <defs>
        <pattern id="${hatchId}" width="4" height="4" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
          <rect width="4" height="4" fill="transparent"></rect>
          <line x1="0" y1="0" x2="0" y2="4" stroke="rgba(255,255,255,0.9)" stroke-width="1"></line>
        </pattern>
      </defs>
      ${hourLabels}
      ${band}
      ${lines}
      ${nowTick}
      ${marker}
    </svg>`;
  }

  _legendHtml(mainState, names) {
    const cheapestId =
      mainState &&
      mainState.attributes &&
      Array.isArray(mainState.attributes.ranking) &&
      mainState.attributes.ranking[0]
        ? mainState.attributes.ranking[0].subentry_id
        : null;
    const visible = new Set([
      ...Object.keys(names || {}),
      ...((mainState && mainState.attributes && mainState.attributes.ranking) || []).map(
        (r) => r.subentry_id,
      ),
    ]);
    const chips = [...this._colorMap.entries()]
      .filter(([id]) => visible.has(id))
      .map(([id, color]) => {
        const label = (names && names[id]) || id;
        const bold = id === cheapestId ? " bold" : "";
        return `<span class="legchip${bold}" style="--c:${color}"><span class="swatch"></span>${esc(
          label,
        )}</span>`;
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
    return `<div class="readout">${range} — ${line || "no data"}${est}</div>`;
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
    this._VB_W = 800;
    this._VB_H = 110;
    this._CHART_TOP = 4;
    this._CHART_BOTTOM = 78;
    this._BAND_TOP = 80;
    this._BAND_H = 14;
    // Cached so the click handler can map a pointer position back to a slot
    // without rebuilding the whole forecast.
    this._chartState = { shown, rangeStart, rangeEnd };
    const svg = this._timelineSvg(shown, names, rangeStart, rangeEnd);
    const readout = this._readoutHtml(names);
    const legend = this._legendHtml(mainState, names);
    return `<div class="forecast">${svg}${readout}${legend}</div>`;
  }

  // ---- Shared ---------------------------------------------------------

  _toggleHtml() {
    return `<span class="viewtoggle">
      <span class="vbtn${this._view === "now" ? " active" : ""}" data-view="now">Now</span>
      <span class="vbtn${this._view === "forecast" ? " active" : ""}" data-view="forecast">Forecast</span>
    </span>`;
  }

  _onClick(e) {
    const vbtn = e.target.closest("[data-view]");
    if (vbtn) {
      this._view = vbtn.dataset.view;
      this._sig = null;
      this._render();
      return;
    }
    const strip = e.target.closest("svg.strip");
    if (strip && this._chartState) {
      const rect = strip.getBoundingClientRect();
      const frac = rect.width > 0 ? Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) : 0;
      const { rangeStart, rangeEnd, shown } = this._chartState;
      const t = rangeStart.getTime() + frac * (rangeEnd.getTime() - rangeStart.getTime());
      const slot =
        shown.find((s) => t >= new Date(s.start).getTime() && t < new Date(s.end).getTime()) ||
        shown[shown.length - 1];
      this._tapped = slot;
      this._sig = null;
      this._render();
    }
  }

  _signature() {
    const mainId = this._mainEntityId();
    const mainState = mainId ? this._hass.states[mainId] : null;
    const fcId = this._forecastEntityId();
    const fcState = fcId ? this._hass.states[fcId] : null;
    return [
      JSON.stringify(this._config),
      this._view,
      Math.floor(Date.now() / 60000),
      mainId || "",
      mainState ? mainState.state + JSON.stringify(mainState.attributes) : "missing",
      fcId || "",
      fcState ? JSON.stringify(fcState.attributes) : "missing",
      this._tapped ? this._tapped.start : "",
    ].join("§");
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._card) {
      this._card = document.createElement("ha-card");
      this.appendChild(this._card);
      this._card.addEventListener("click", (e) => this._onClick(e));
    }
    const sig = this._signature();
    if (sig === this._sig) return;
    this._sig = sig;

    const showForecast = this._config.show_forecast !== false;
    if (!showForecast) this._view = "now";
    const title = this._config.title || "Heating costs";
    const head = `<div class="head"><span class="title">${esc(title)}</span>${
      showForecast ? this._toggleHtml() : ""
    }</div>`;

    const mainId = this._mainEntityId();
    const mainState = mainId ? this._hass.states[mainId] : null;
    const fcId = this._forecastEntityId();
    const fcState = fcId ? this._hass.states[fcId] : null;

    const body =
      this._view === "forecast"
        ? this._forecastHtml(mainState, fcState)
        : this._nowHtml(mainState);

    this._card.innerHTML = `<style>${CARD_CSS}</style>${head}${body}`;
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
