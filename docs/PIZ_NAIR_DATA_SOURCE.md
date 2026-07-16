# Piz Nair data source investigation

This document records a **bounded** investigation into whether Piz Nair
(near St. Moritz/Corviglia) has a stable, machine-readable, permitted data
source this project could use as a second real summit station alongside
Corvatsch (COV). It is not an open-ended scraper project - see the
explicit constraints in this PR's task specification.

## Conclusion

**`no_source_found`** (bounded by this session's tooling - see "Tooling
constraint encountered" below; this is a report on what could and could
not be established, not a definitive claim that no such feed exists
anywhere).

`config/stations.json`'s `piz_nair` entry stays exactly as it already was:
`provider: "engadin_st_moritz_mountains"`, `verification: "unverified"`,
`enabled: false`. **No provider module was implemented, no data was
archived, and `piz_nair` was not added to the summit/competing_flow roles
as a real data source.** Piz Nair observations are never represented using
Corvatsch data - the two remain entirely independent entries.

## Tooling constraint encountered

This investigation was carried out in a sandboxed session where:

- Direct HTTP access (`curl`, and this repo's own `requests`-based
  fetchers) to essentially all external hosts is blocked at the network
  proxy level (confirmed independently for `data.geo.admin.ch` earlier in
  this same PR - see `historical_data.py sync`'s own `403 ProxyError` log
  - and reconfirmed here for resort/aggregator domains).
- The `WebFetch` tool (which would normally let an agent read a page's
  rendered content) returned `403 Forbidden` for **every** URL attempted
  in this session, including a sanity-check fetch of `https://example.com`
  - i.e. this is a session-wide tooling limitation, not something specific
  to any resort or aggregator site being hostile to this investigation.
- Only `WebSearch` (search-engine snippets, no raw page content, no
  ability to inspect actual network requests a real browser would make)
  was available.

This means the investigation below is based entirely on search-result
snippets, not on actually loading any page, inspecting its DOM, or
watching its real network traffic (which is what Part 7 of this task
asks for as the primary method). **A follow-up pass in a genuinely
network-enabled environment - opening each candidate page in a real
browser and inspecting its network tab, exactly as instructed - is
required before any conclusion here can be upgraded.**

## Candidates checked (via search snippets only)

| Candidate | Owner | Station identity | Fields (as far as could be told) | Update interval | Works without cookies | Reuse terms | Historical availability | Conclusion |
|---|---|---|---|---|---|---|---|---|
| MeteoNews "Weather Piz Nair" page | MeteoNews AG (private forecast provider) | Unclear whether this is a real station reading or a model/interpolated forecast point labeled "Piz Nair" | Unknown - page content could not be inspected (WebFetch blocked) | Unknown | Unknown | Unknown - a commercial provider's page, reuse terms not established | Unknown | `public_but_terms_unclear` at best - likely `aggregator_only`, unconfirmed |
| meteoblue "Weather Piz Nair" page | meteoblue AG (private forecast provider, model-based) | This is a well-known GLOBAL FORECAST MODEL product, not a station network - meteoblue does not operate physical stations at every named summit it forecasts for | N/A - model output, not observation | N/A | N/A | meteoblue's public site is a forecast display, not a documented reuse API for this purpose | N/A | `aggregator_only` (forecast model, not a real observation) - explicitly not usable as a "station" |
| mountain-forecast.com "Piz Nair Weather Forecast" | Third-party mountain-forecast aggregator | Same as above - a forecast product, not a station reading | N/A | N/A | N/A | Unclear | N/A | `aggregator_only`, not usable |
| Engadin St. Moritz Mountains AG / mountains.ch webcams (Corviglia & Muottas Muragl) | Engadin St. Moritz Mountains AG (the resort operator explicitly named in this task) | Webcam network, not a documented weather-data API | N/A - webcam imagery, no numeric station data found via search | N/A | Unknown - page could not be loaded | Unknown | N/A | `webcam_only` - and explicitly out of scope per this task's "do not scrape webcam text or HTML... unless there is no other source and explicit permission is documented" (no such permission was found or sought) |
| `engadin.stmoritz.ch/winter/en/wetter-engadin` ("current weather in the Engadin") | Engadin St. Moritz Mountains AG / Engadin St. Moritz tourism | Likely a resort weather-overview page; could not be loaded to inspect station identity or underlying feed | Unknown - `WebFetch` returned 403 | Unknown | Unknown | Unknown | Unknown | `browser_session_only` at best - genuinely undetermined without a real browser session |
| `infosnow.ch` ski-resort weather/piste-report aggregator (Engadin St. Moritz entry) | Infosnow (third-party ski-resort data aggregator used across many Swiss resorts) | Could not be loaded to inspect | Unknown | Unknown | Unknown | Unknown | Unknown | `aggregator_only` at best, undetermined |
| bergfex.com weather page for Engadin St. Moritz | Bergfex (Austrian ski/mountain weather aggregator, explicitly permitted by this task to help IDENTIFY an underlying provider, not to be used directly) | Could not be loaded to inspect | Unknown | Unknown | Unknown | Unknown | Unknown | Investigated only to identify an underlying provider - none could be confirmed since the page could not be loaded |
| Windy.com "Stations" network | Windy.com (crowdsourced + official station aggregator, some stations have a public API) | Windy's own search result confirms a general public stations API exists (`api.windy.com`), but no specific Piz Nair/Corviglia station entry was confirmed present or absent via search alone | Unknown | Unknown | Windy's API has documented terms in general, but no specific station for this location was confirmed to exist | Unknown | Undetermined - would need the Windy stations map inspected directly in a browser |
| Official MeteoSwiss SwissMetNet network (opendata.swiss / MeteoSwiss's own automatic-station documentation) | MeteoSwiss | Search results describe the ~260-station SwissMetNet network in general but returned **no confirmation that a station exists specifically at Piz Nair or Corviglia** | N/A | N/A | N/A (would be MeteoSwiss Open Data if it existed) | N/A | Consistent with this task's own given fact: **Piz Nair is NOT a confirmed MeteoSwiss OGD station** - nothing found here contradicts that |

## What would change this conclusion

Per this task's explicit acceptance criteria, `piz_nair` may only be
enabled once ALL of the following are established (not yet done):

1. A stable, machine-readable endpoint (not a webpage meant for human
   eyes, not a webcam).
2. Current, timestamped wind observations (not a forecast-model value
   mislabeled as "Piz Nair," e.g. the meteoblue/mountain-forecast
   candidates above appear to be exactly this trap).
3. A real, confirmed station location and elevation.
4. Clear public access or explicit documented permission to reuse the
   data.
5. Successful retrieval without browser session cookies (i.e. a genuine
   API/data endpoint, not something that only renders inside a logged-in
   or JS-rendered browser session).
6. Acceptable historical or at least live continuity.

None of these could be confirmed or denied conclusively in this session
given the tooling constraint above. The next investigation pass should:
- Open each candidate URL in an actual browser (or a network-enabled
  `WebFetch`/headless-browser session) and inspect the Network tab for
  any XHR/fetch calls returning JSON/XML with numeric station data.
- Specifically distinguish a genuine physical station reading from a
  forecast-model value that merely displays the place name "Piz Nair" -
  meteoblue and mountain-forecast.com's pages are the most likely
  candidates for this trap, based on how those services are generally
  known to work (global forecast models, not station networks).
- Check whether Engadin St. Moritz Mountains AG (mountains.ch /
  engadin.stmoritz.ch) publishes any documented open-data or reuse policy
  for its own weather page, separate from its webcam network.

## What this PR does NOT do

- No provider module was written for Piz Nair.
- No data was fetched, archived, or normalized for `piz_nair`.
- `config/stations.json`'s `piz_nair` entry is unchanged: `enabled: false`,
  `verification: "unverified"`.
- Piz Nair's summit/competing_flow roles are represented by **zero**
  stations today (Corvatsch alone covers the `summit`/`competing_flow`
  roles once it's confirmed) - Piz Nair data is never substituted with
  Corvatsch data to make a role look more populated than it is.
