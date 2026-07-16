/*
 * dashboard-logic.js - pure, dependency-free helpers for turning
 * dashboard_data.json's upcoming_forecast list into "Today" / "Tomorrow"
 * forecast cards, using Europe/Zurich calendar dates rather than the
 * viewer's own device timezone or UTC.
 *
 * Loaded via a plain <script src="./dashboard-logic.js"> tag before the
 * main inline script in index.html - no build step, no bundler, no
 * framework. The `module.exports` guard at the bottom only fires under
 * Node (used by tests/test_dashboard_logic.py, which shells out to
 * `node` to exercise this file directly); it's a no-op in the browser
 * since `module` is undefined there.
 *
 * Why not just check `new Date()` in the viewer's local timezone: this
 * dashboard's target_time strings are naive Europe/Zurich wall-clock
 * strings (see parseNaiveLocal below), and "today"/"tomorrow" must be
 * computed against that same Zurich calendar, not the browser's own
 * timezone (Europe/Zurich is UTC+1 in winter/CET, UTC+2 in summer/CEST -
 * a viewer in e.g. US Pacific time or Sydney would get the wrong day
 * entirely if we used their local midnight, and even a UTC-based
 * calculation is wrong for a few hours around Zurich midnight whenever
 * Zurich's offset isn't 0).
 */

// target_time is a naive Europe/Zurich wall-clock string ("YYYY-MM-DDTHH:MM",
// no offset) - parse its components directly via Date.UTC and always format
// with timeZone:'UTC' so the displayed hour matches Zurich regardless of the
// viewer's own clock. The returned Date is a "pseudo-UTC" stand-in for a
// Zurich wall-clock instant - only ever compare it against other values
// produced the same way (e.g. zurichNowPseudoUtc()), never against a real
// UTC timestamp.
function parseNaiveLocal(s) {
  const [datePart, timePart] = s.split('T');
  const [y, m, d] = datePart.split('-').map(Number);
  const [hh, mm] = timePart.split(':').map(Number);
  return new Date(Date.UTC(y, m - 1, d, hh, mm));
}

// The current wall-clock date/time in Europe/Zurich, as plain numeric parts -
// Intl's timeZone option already knows the CET/CEST transition dates, so
// this is correct year-round without any manual DST math.
function zurichDateParts(referenceDate) {
  referenceDate = referenceDate || new Date();
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Europe/Zurich', hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
  const parts = {};
  for (const { type, value } of fmt.formatToParts(referenceDate)) {
    parts[type] = value;
  }
  let hour = Number(parts.hour);
  if (hour === 24) hour = 0; // some locales report midnight as "24"
  return {
    year: Number(parts.year), month: Number(parts.month), day: Number(parts.day),
    hour, minute: Number(parts.minute), second: Number(parts.second),
  };
}

function _pad2(n) { return String(n).padStart(2, '0'); }

// "YYYY-MM-DD" for the given instant's Europe/Zurich calendar date.
function zurichDateString(referenceDate) {
  const p = zurichDateParts(referenceDate);
  return `${p.year}-${_pad2(p.month)}-${_pad2(p.day)}`;
}

function zurichTodayDateString(referenceDate) {
  return zurichDateString(referenceDate);
}

// Adds `days` (may be negative) to a "YYYY-MM-DD" string, returning a new
// "YYYY-MM-DD" string. Uses UTC noon internally so adding/subtracting days
// never shifts by an hour around a DST transition.
function addDaysToDateString(dateStr, days) {
  const [y, m, d] = dateStr.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d, 12));
  dt.setUTCDate(dt.getUTCDate() + days);
  return dt.toISOString().slice(0, 10);
}

function zurichTomorrowDateString(referenceDate) {
  return addDaysToDateString(zurichTodayDateString(referenceDate), 1);
}

// A "pseudo-UTC" Date for the current instant, built the same way
// parseNaiveLocal() builds one from a target_time string - so the two are
// directly comparable with </>/<=/>= despite neither being real UTC.
function zurichNowPseudoUtc(referenceDate) {
  const p = zurichDateParts(referenceDate);
  return new Date(Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second));
}

// Groups upcoming_forecast rows by their Zurich calendar date
// (target_time.slice(0,10)), keeping only hours in [startHour, endHour]
// (inclusive), sorted chronologically within each date. Returns a plain
// object: {"YYYY-MM-DD": [row, ...]}. Never picks "the earliest date with
// data" as a stand-in for today - callers must look up a specific date key.
function groupForecastByWindow(upcomingForecast, startHour, endHour) {
  const byDate = {};
  for (const row of (upcomingForecast || [])) {
    const hour = parseNaiveLocal(row.target_time).getUTCHours();
    if (hour < startHour || hour > endHour) continue;
    const day = row.target_time.slice(0, 10);
    (byDate[day] = byDate[day] || []).push(row);
  }
  for (const day in byDate) {
    byDate[day].sort((a, b) => a.target_time.localeCompare(b.target_time));
  }
  return byDate;
}

// Keeps only cards whose target_time is strictly after `now` (a pseudo-UTC
// Date from zurichNowPseudoUtc()) - i.e. hours that haven't started yet.
function filterRemaining(cards, now) {
  return (cards || []).filter(c => parseNaiveLocal(c.target_time) > now);
}

// The card with the highest probability in a list (ties keep the first).
// Returns null for an empty list.
function selectBestCard(cards) {
  if (!cards || !cards.length) return null;
  return cards.reduce((best, c) => (c.probability ?? 0) > (best.probability ?? 0) ? c : best, cards[0]);
}

const FORECAST_WINDOW_START_HOUR = 14;
const FORECAST_WINDOW_END_HOUR = 18;

// Splits an upcoming_forecast array into "today's remaining 14:00-18:00
// hours" and "tomorrow's full 14:00-18:00 hours", using Europe/Zurich
// calendar dates - this is the fix for the visibility bug where the
// dashboard picked "the earliest date with any forecast data" as a stand-in
// for today, which silently hid tomorrow's forecast for as long as even one
// hour of today's window was still in the list. `referenceDate` defaults to
// the real current time but can be overridden (tests do this) to exercise
// specific times of day without waiting for them.
function getForecastByDay(upcomingForecast, referenceDate) {
  const byDate = groupForecastByWindow(upcomingForecast, FORECAST_WINDOW_START_HOUR, FORECAST_WINDOW_END_HOUR);
  const now = zurichNowPseudoUtc(referenceDate);
  const todayStr = zurichTodayDateString(referenceDate);
  const tomorrowStr = zurichTomorrowDateString(referenceDate);
  const todayAllCards = byDate[todayStr] || [];
  return {
    todayStr, tomorrowStr,
    todayAllCards,
    todayRemaining: filterRemaining(todayAllCards, now),
    tomorrowCards: byDate[tomorrowStr] || [],
  };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    parseNaiveLocal,
    zurichDateParts,
    zurichDateString,
    zurichTodayDateString,
    addDaysToDateString,
    zurichTomorrowDateString,
    zurichNowPseudoUtc,
    groupForecastByWindow,
    filterRemaining,
    selectBestCard,
    getForecastByDay,
    FORECAST_WINDOW_START_HOUR,
    FORECAST_WINDOW_END_HOUR,
  };
}
