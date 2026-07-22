import unittest

import meteoswiss_local_forecast as mlf


class FakeResponse:
    def __init__(self, *, payload=None, text=""):
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, catalog, files):
        self.catalog = catalog
        self.files = files

    def get(self, url, timeout):
        if url == mlf.STAC_ITEMS_URL:
            return FakeResponse(payload=self.catalog)
        return FakeResponse(text=self.files[url])


class LocalForecastTests(unittest.TestCase):
    def test_point_parser_only_returns_silvaplana(self):
        source = "point_id;point_type_id;Date;tre200h0\n1;1;202607221000;5.0\n751300;2;202607221000;14.2\n"
        self.assertEqual(mlf._point_values(source, "tre200h0"), {"202607221000": 14.2})

    def test_fetch_combines_parameters_by_hour(self):
        assets = {}
        files = {}
        for parameter in mlf.PARAMETERS:
            url = f"https://example.test/vnut12.lssw.202607221000.{parameter}.csv"
            assets[parameter] = {"href": url}
            files[url] = f"point_id;point_type_id;Date;{parameter}\n751300;2;202607221100;12.0\n"
        result = mlf.fetch_forecast(FakeSession({"features": [{"assets": assets}]}, files))
        self.assertEqual(result["location"], "Silvaplana")
        self.assertEqual(result["issued_at"], "2026-07-22T10:00:00+00:00")
        self.assertEqual(len(result["hours"]), 1)
        self.assertEqual(result["hours"][0]["temp_c"], 12.0)
        self.assertEqual(result["hours"][0]["gust_kmh"], 12.0)


if __name__ == "__main__":
    unittest.main()
