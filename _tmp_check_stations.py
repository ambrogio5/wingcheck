"""One-off: confirm the real MeteoSwiss SwissMetNet station codes for Lugano
and Zurich, and that they publish the sea-level pressure column (pp0qffs0)."""
import requests

CANDIDATES = {
    "Lugano": ["lug"],
    "Zurich": ["sma", "klo", "reh"],
}


def check(code):
    url = f"https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-smn/items/{code}"
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        return f"{code}: HTTP {r.status_code}"
    d = r.json()
    title = d.get("properties", {}).get("title") or d.get("id")
    assets = d.get("assets", {})
    hourly = [n for n in assets if f"_{code}_h" in n.lower() and n.lower().endswith(".csv")]
    recent_url = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{code}/ogd-smn_{code}_h_recent.csv"
    rr = requests.get(recent_url, timeout=30)
    has_pressure = "pp0qffs0" in rr.text.split("\n", 1)[0].lower() if rr.status_code == 200 else None
    return f"{code}: OK, title={title!r}, hourly_files={len(hourly)}, recent_csv_status={rr.status_code}, has_pp0qffs0_column={has_pressure}"


for label, codes in CANDIDATES.items():
    print(f"--- {label} ---")
    for code in codes:
        try:
            print(" ", check(code))
        except Exception as e:
            print(f"  {code}: ERROR {e}")
