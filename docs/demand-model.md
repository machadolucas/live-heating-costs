# Thermal demand model and shadow validation

Live Heating Costs remains advisory. Its existing degree-hour/UA demand model is
active by default. The optional two-state thermal model first runs in **shadow**:
it publishes an estimate, records its inputs and compares one-hour predictions
with later observations. It never controls a climate, switch, fan or ventilation
heater. Neither an offline fit nor a successful validation automatically activates
it.

## What the estimate means

The thermal model uses two temperatures: representative indoor air and effective
building fabric. Envelope conductance is in kW/°C; air/fabric heat capacities are
in kWh/°C. Direct heating enters the air node. Sources configured with a positive
`response_minutes` enter the fabric node, representing slower floor or masonry
response. This is a simplified two-state prior, not a calibrated surface or
multizone building simulation.

The target defaults to **21 °C**, with a **one-hour** recovery horizon. The estimate
answers how much constant delivered heat would bring the air temperature to the
target at that horizon. Stable implicit substeps integrate losses, gains and
storage. Air and fabric storage are accounted for once inside that calculation;
the displayed storage component must not be added to total demand again.

- **Total:** required direct air heat plus already committed fabric heat.
- **Expected delivery:** all current direct and fabric heat if present operation continues.
- **Continuation:** the part of required direct air heat already being supplied.
- **Remaining:** additional direct air heat required after continuation.
- **Fabric heat:** the separate committed slow-source contribution.

If current heating is unknown, continuation and remaining are unknown. A target
outside the model solver’s 100 kW bound is reported as unreachable; actual source
capacity shortfalls are reported separately by the constrained allocation. These are
recommendations with quality/provenance attributes, not thermostat commands.

The legacy fitted UA includes average ventilation and gains. It must not be reused
as an envelope-only coefficient while also adding explicit ventilation losses:
that would count some heat losses twice. Start with an independently justified
envelope prior and retain the legacy model as a comparator.

## Measurements and provenance

Indoor readings have explicit weights and offsets. A missing, stale, implausible
or unavailable room sensor reduces weighted coverage; it never becomes 0 °C.
Readings use °C at the HA boundary. Changing indoor membership, model inputs or
parameters invalidates old validation evidence through the profile fingerprint.
Calibrated offsets mean offsets checked against a suitable reference, not simply
entered numbers.

Electrical counters are normalized to kWh before differencing. Nonpositive
intervals, unavailable readings, resets, sampling gaps and increments exceeding
rated power × elapsed time are rejected. The next interval starts from a fresh
baseline. Use **independent floor meters**, not an aggregate counter with an
unverified recorder sum: a reset/reappearance in a summed `total_increasing`
entity can produce a large historical artifact even when individual meters are
sound. Meter IDs must not be assigned to multiple heat sources.

Resistance heat is based on metered electricity at COP 1. Heat-pump electricity
is measured, but delivered heat obtained by multiplying by COP is **modelled**.
The entity attribute called `measured_cop` is a curve reconciled against electric
energy; without an independent heat meter it does not measure thermal output.
`delivered_power_entity` can provide an independent delivered-heat measurement
when such a meter actually exists.

Fuel timers, sauna-ready events and temperature-rise indicators identify activity;
they do not independently quantify delivered house heat. Unmetered radiators,
unknown wood/sauna contribution and missing ventilation remain visible as missing
inputs. They cannot be silently treated as zero for calibration. Indoor comfort,
heat accounting and event review all matter: a temperature fit alone cannot
identify a reliable envelope coefficient if unknown heat sources explain the same
observations.

Occupancy-derived gains are estimates. A home-zone resident count may miss guests.
PV output is a sunlight proxy, not a direct measure of solar gain through windows.
Equipment gain meters must exclude heat already counted as a heating source.

## Ventilation input

Supply and extract fan control percentages are distinct from fan speed presets,
electrical watts and airflow. For the bundled Parmair 120 manufacturer data,
pressure/flow/power curves belong to the documented fan/model. Confirm that the
installed unit and controller scaling match those curves before commissioning.
`fan` percentage may merely encode speed 1–5; use the actual supply and extract
output sensors for fan-curve inputs.

Each fan needs its own installed-system resistance coefficient, in
Pa/(L/s)². The curve intersection estimates flow under that pressure assumption.
Changing filters, duct dampers or operating configuration can invalidate it.
Whole-unit electrical power is a consistency check only when heater state and
auxiliary consumption are known; it is not an independent airflow measurement.
A diagnostic preheater-output signal may exist even when its HA entity is disabled.
Enable/read appropriate diagnostics deliberately; an absent signal is unknown.

Ventilation uses signed sensible boundary heat. Supply air warmer than indoors can
produce a gain; colder supply and airflow imbalance can create a loss. Supply-side
recovery efficiency alone cannot validate a balanced heat-recovery energy balance.
Temperature calibration and actual airflow commissioning remain necessary.

The manufacturer curve domain is bounded. Unsupported controls/pressures are not
extrapolated to create a confident answer. `model_verified` and
`controller_verified` are explicit checks, not automatic deductions from a name.

## Configuration

Reconfigure the hub, choose its demand settings, and provide the optional
`shadow_model` object. Empty configuration disables the advanced shadow model.
The following example uses generic entity IDs and illustrative priors. Replace
source keys with the actual **config subentry IDs**, available in diagnostics.
Do not copy these commissioning values as facts about a different house.

```json
{
  "target_c": 21,
  "activate": false,
  "calibrated": false,
  "event_coverage_reviewed": false,
  "indoor": [
    {
      "entity_id": "sensor.main_room_temperature",
      "weight": 2,
      "offset_c": 0,
      "calibrated": false
    },
    {
      "entity_id": "sensor.bedroom_temperature",
      "weight": 1,
      "offset_c": 0,
      "calibrated": false
    }
  ],
  "parameters": {
    "envelope_kw_per_c": 0.14,
    "air_capacity_kwh_per_c": 0.5,
    "fabric_capacity_kwh_per_c": 10,
    "coupling_kw_per_c": 0.5
  },
  "ventilation": {
    "supply_control_entity": "sensor.hrv_supply_fan_output",
    "extract_control_entity": "sensor.hrv_extract_fan_output",
    "supply_temp_entity": "sensor.hrv_supply_temperature",
    "power_entity": "sensor.hrv_electric_power",
    "preheater_entity": "sensor.hrv_preheater_output",
    "postheater_entity": "sensor.hrv_postheater_output",
    "supply_resistance_pa_per_ls2": 0.05,
    "extract_resistance_pa_per_ls2": 0.05,
    "auxiliary_power_w": 0,
    "model_verified": false,
    "controller_verified": false
  },
  "sources": {
    "FLOOR_SOURCE_SUBENTRY_ID": {
      "energy_entities": [
        "sensor.floor_zone_a_energy",
        "sensor.floor_zone_b_energy"
      ],
      "max_electric_kw": 2,
      "local_capacity_kw": 0.5,
      "response_minutes": 60
    },
    "PUMP_SOURCE_SUBENTRY_ID": {
      "energy_entities": ["sensor.pump_electric_energy"],
      "max_electric_kw": 3,
      "local_capacity_kw": 0,
      "response_minutes": 0
    }
  },
  "gains": {
    "people_entity": "zone.home",
    "watts_per_person": 75,
    "max_people": 10,
    "equipment_power_entity": "sensor.non_heating_indoor_power",
    "solar_power_entity": "sensor.pv_power",
    "solar_factor": 0,
    "max_kw": 3
  },
  "geometry": {
    "schema_version": 1,
    "model_id": "portable-house-model",
    "fingerprint": "source-revision-fingerprint",
    "conditioned_area_m2": 120
  }
}
```

`local_capacity_kw` reserves source capacity for local loads before assigning
common-zone demand. `local_demand_entity` can provide a separate local demand
input; source eligibility and headroom remain constrained by the runtime's
available, known-cost sources. A sauna or a heater serving another zone should
not automatically satisfy common-room recovery demand.

Geometry is optional portable metadata, suitable for an export from a separate
house model. Its conditioned area, model identity and fingerprint document the
prior's origin. Geometry does not automatically calibrate material properties,
COP, window solar gains or ventilation. A changed geometry/profile requires
fresh validation. No household model or private geometry is bundled in the
integration.

## Retained feature schema

Runtime Store data contains `data.shadow.calibration`; a standalone export may
contain `shadow.calibration`, `calibration`, or the calibration object itself.
The `features` list retains at most 90 days / 25,920 five-minute intervals.
Timestamps are aware UTC and chronological. Each row represents the interval
ending at `at`; interval energy is in kWh, ventilation/gains in kW.

```json
{
  "features": [
    {
      "at": "2026-01-01T00:05:00+00:00",
      "indoor_c": 21,
      "outdoor_c": 0,
      "indoor_coverage": 1,
      "electric_kwh": 0.1,
      "measured_heat_kwh": 0.02,
      "modelled_heat_kwh": 0.3,
      "ventilation_kw": -0.2,
      "gains_kw": 0.15,
      "valid": true,
      "events": ["cold"],
      "fabric_heat_kwh": 0.02
    }
  ],
  "pending": [],
  "scores": []
}
```

Unknown values use JSON `null`. A row with unknown gains, ventilation, temperature
or heat is ineligible for fitting. `fabric_heat_kwh` is optional for compatibility
with older exports; the offline tool requires an explicit, documented fallback
partition if it is absent. Review the retained regime tags: merely observing a
`cold` or `gains` tag is not validation of defrost, openings, wood/sauna, floor
recovery, price holds or sensor dropout behavior.

## Offline candidate fitting

The offline CLI uses Python's standard library and the pure RC modules; it does
not import Home Assistant, connect to the house, edit the Store, or activate the
model. Run calibration through the production host's heavy-job guard:

```bash
~/claude/scripts/heavy-job.sh .venv313/bin/python scripts/calibrate_shadow.py \
  /path/to/exported-runtime.json \
  --parameters /path/to/shadow-parameters.json \
  --envelope-values 0.06,0.10,0.14,0.20,0.30 \
  --fabric-values 2,5,10,20,40 \
  --output /path/to/candidate-report.json
```

The parameters file accepts the four thermal parameter keys or an object with a
`parameters` member. Fixed parameters must match the profile under review. The
defaults are explicitly labeled as unsupplied priors in output. On this host,
exit 75 from the guard means stop and report; do not bypass or loop.

The search is bounded to 225 candidates. Envelope candidates are limited to
0.03–0.6 kW/°C, fabric capacity to 1–100 kWh/°C. Air capacity and air/fabric coupling
remain fixed; so do retained COP-derived heat, gains and ventilation. A parameter
fit can therefore absorb biases in those fixed quantities; it does not validate
them. If older rows lack explicit fabric heat, `--fabric-heat-fraction` supplies
a fixed fallback, default 0. The output records that assumption.

At least **14 sufficiently covered training days** and **7 later validation days**
are required. A covered day needs ≥90% of its five-minute rows to have valid,
complete inputs. Fabric starts at the first measured indoor temperature and evolves
sequentially; only past air observations are assimilated. Invalid intervals/gaps
reset state and require 24 hours of warm-up before scoring. One-hour predictions
are free trajectories without future indoor-temperature corrections. Training
predictions cannot cross into the validation week. The later week's temperature
observations never select the winning grid candidate.

The default `--driver-mode persistence` holds the latest observed weather, heat,
ventilation and gains constant for the next hour. This is a chronological replay
under a clearly stated persistence assumption, not a reconstruction of archived
weather forecasts. `--driver-mode retrospective` instead uses actual subsequent
historical drivers and labels the result **retrospective diagnostic, NOT a
forecast**. It cannot establish operational predictive accuracy.

The report includes temperature MAE versus the legacy-parameter baseline and
indoor-temperature persistence, plus heat-energy errors separated into measured,
modelled and mixed provenance. Candidate target-energy error compares modeled
one-hour target demand against the heat that was actually supplied; controller
choices and comfort changes can affect that error. It is not an independent heat
measurement. Reports always contain `ready: false`, `applied: false` and
`regime_coverage_validated: false`.

Legacy UA backfill may use hourly recorder statistics. With an optional shadow
profile, independent meter overrides apply there too; missing hours, unmetered
radiators, uncertain fuel and impossible increments block quality learning.
These hourly/daily summaries do **not** supply the five-minute telemetry or
prospective predictions required for shadow readiness or offline fitting.

Review a candidate before entering parameters into a new shadow profile. Changing
the profile fingerprint starts validation for that candidate; the offline report
is never silently substituted for prospective evidence.

## Readiness and activation

Readiness requires explicit checks for indoor offsets, envelope/capacities,
ventilation airflow, complete heat accounting and reviewed event coverage. It
also requires **28 distributed days**, with at least 90% valid five-minute feature
coverage and predictions in at least 22 distinct hours per day for the same
profile revision. Temperature MAE must improve by at least **15% against both**
the baseline and temperature-persistence comparators. Energy scoring needs ≥90%
coverage; energy MAE must not degrade materially within each observed provenance
group (current tolerance: 5% plus 0.05 kWh for a one-hour prediction).

Pending predictions are recorded before their future observations. Restarts clear
pending forecasts and energy baselines so an outage cannot manufacture a valid
interval. Successful readiness is evidence for a human decision. Activation also
requires the explicit `activate` setting; default behavior stays with legacy
advisory demand. Actuation always belongs to the user's independent automations.

Optional `ventilation.mode_entity` records the controller's recovery, summer or
defrost labels alongside pre/post-heater events. These labels support regime
review; they do not manufacture an airflow or certify a mode automatically.
For a dual-purpose source, `delivered_local_power_entity` supplies an independent
local delivery split in W/kW. Without a defensible split, additional local
allocation stays unknown rather than re-requesting all local heat.
