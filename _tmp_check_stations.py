"""One-off: dump the real CSV header for Lugano/Zurich recent data to find
the actual sea-level pressure column name (pp0qffs0 turned out to be wrong)."""
import requests

for label, code in [("Lugano", "lug"), ("Zurich/Fluntern", "sma")]:
    url = f"https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/{code}/ogd-smn_{code}_h_recent.csv"
    r = requests.get(url, timeout=30)
    lines = r.text.split("\n")
    print(f"--- {label} ({code}), status {r.status_code} ---")
    print("header:", lines[0])
    if len(lines) > 1:
        print("row 1: ", lines[1])
    print()

# Also check the STAC item's own metadata/parameter description assets, if any.
for label, code in [("Lugano", "lug"), ("Zurich/Fluntern", "sma")]:
    url = f"https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-smn/items/{code}"
    r = requests.get(url, timeout=30)
    d = r.json()
    print(f"--- {label} STAC assets ---")
    for name in d.get("assets", {}):
        if "meta" in name.lower() or "param" in name.lower():
            print(" ", name)
