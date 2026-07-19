# Dashboard design system

Wingcheck's dashboard ships three selectable skins - `CLASSIC` (the original
presentation, and the default), `TECH` (a cyan CRT terminal), and `RETRO` (a
late-1970s/1980s automotive instrument console). All three are built with
static HTML, CSS, and JavaScript and deliberately keep the operational
dashboard's real data contract and rendering functions separate from their
visual skin. This document describes the `RETRO` skin.

## Source of truth

- `dashboard_data.json` remains the only runtime data source.
- `index.html` retains the forecast, recommendation, live verification,
  station health, reference-station, evaluation, ablation, and technical
  renderers.
- `retro-dashboard.css` provides the presentation layer and central design
  tokens. No prototype transit data or reference artwork is shipped.
- The `CLASSIC` control removes the `data-skin` attribute and restores the
  original dashboard presentation (the default); `TECH` and `RETRO` set
  `data-skin="tech"` / `data-skin="retro"`. The chosen skin persists in
  `localStorage` under `wingcheck-skin`.

## Instrument cluster (retro hero)

The retro skin opens with a full-width **instrument cluster** — a late-1970s
operator-console reimagining of the same real data, rendered by
`renderRetroCluster()` in `index.html` and shown only when
`data-skin="retro"` (the CLASSIC and TECH skins keep it `display:none`):

- **Airflow** — the wind's real down-valley path (Val Bregaglia → Maloja Pass
  → Sils/Segl-Maria → Silvaplana lake → Samedan), the lake marked as the
  target, annotated with the latest SIA reading and station ages.
- **Session-likelihood dial** — a swept tick arc whose value comes from the
  day's `session_forecast.event_probability`, zone-colored by the model's own
  tier thresholds, with peak-wind, model-agreement and expected-gust readouts.
- **Area map** — an animated wireframe of the Engadin/Bregaglia valley (lakes,
  Corvatsch massif, station nodes) with wind streamlines whose flow speed is
  scaled to the forecast wind. The animation is pure CSS/SVG and stops under
  `prefers-reduced-motion`.
- **Systems** — real per-station health (OK / STALE / OFFLINE) from
  `station_health`.
- **Next rideable window** — the next scored hour, its likelihood, wind/gust,
  tier and time-to-go.
- **Day profile** — an hourly-likelihood sparkline plus a 12–18h tick strip.

Every value is read from `dashboard_data.json`; missing fields render as
`--`. No prototype telemetry or reference artwork is shipped.

## Visual mapping

| Real Wingcheck content | Instrument-console treatment |
| --- | --- |
| Today and tomorrow probabilities | Primary amber telemetry bank |
| Daily recommendation | Compact decision readout |
| Session outlook | Diagnostic signal panel |
| Lake station temperature | Auxiliary environmental gauge |
| Live verification | Operational performance module |
| Station and ingestion health | Systems-status panel |
| Frozen evaluation and ablation | Calibration modules |
| Technical charts and weights | Expandable engineering readouts |

The palette, typography, panel borders, status colors, and chart colors are
defined as CSS custom properties near the top of `retro-dashboard.css`. Extend
those tokens instead of introducing one-off colors.

## Responsive and accessible behavior

- Desktop uses a 12-column console grid.
- Portrait tablets and phones use the same information in a single-column
  hierarchy; no real metric is removed.
- Dense tables remain horizontally scrollable.
- Status is communicated by text as well as color.
- The dashboard includes a skip link, visible keyboard focus, semantic regions,
  accessible chart labels, reduced-motion support, and high-contrast fallbacks.
- The brief power-on animation runs once and is disabled when reduced motion is
  requested.

When adding a new module, preserve the existing data/state handling, add a
named `panel-*` grid class, provide a compact missing-data state, and test it at
1280 px, 768 px, and 390 px widths.
