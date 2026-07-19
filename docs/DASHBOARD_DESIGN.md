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
