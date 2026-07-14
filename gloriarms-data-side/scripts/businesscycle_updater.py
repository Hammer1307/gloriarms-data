#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
businesscycle_updater.py  -  GLORIARMS Business Cycle Monitor data builder
==========================================================================
Produces  businesscycle_data.json  in the schema the website component
(BusinessCycleMonitor.astro) fetches from:
    https://hammer1307.github.io/gloriarms-data/businesscycle_data.json

Output schema (must match exactly):
{
  "meta": { "title","source","unit","last_updated"(YYYY-MM-DD),
            "latest_month"(YYYY-MM), "months":[12 x "YYYY-MM"], "note" },
  "countries": [ { "code","name","flag", "cli":[12 floats], "bci":[12 floats|null] }, ... 16 ]
}

DATA SOURCES (all free / redistributable with attribution):
  * OECD Composite Leading Indicator (CLI, amplitude-adjusted, long-term average = 100)
  * OECD Business Confidence Indicator (BCI, amplitude-adjusted, LT avg = 100)
    -> pulled from FRED (public CSV endpoint).  Ireland is NOT in the OECD CLI ->
       European Commission ESI (Eurostat) is used as Ireland's leading-indicator proxy.

  >>> FIRST-RUN VERIFICATION (do this once) <<<
  FRED is progressively retiring some OECD MEI-based series.  Before the first
  production run, open each series id below in FRED and confirm it still updates
  (or take the current id from the OECD Data Explorer, dataflow
   OECD.SDD.STES,DSD_STES@DF_CLI / DF_BCI).  Update SERIES_CLI / SERIES_BCI as needed.
  The script is fault-tolerant: a missing/failed series becomes null and the run
  continues, so one dead id never blocks the others.

Requires:  nothing - uses the public FRED CSV endpoint (no API key), Python stdlib only.
Run:       python businesscycle_updater.py  ->  writes ./businesscycle_data.json
"""

import os, sys, io, csv, json, time, datetime as dt
from urllib.request import urlopen, Request

OUT = os.environ.get("BC_OUT", "businesscycle_data.json")

# --- 16 countries (Russia intentionally excluded) ---------------------------
COUNTRIES = [
    ("US", "USA",            "\U0001F1FA\U0001F1F8"),
    ("EA", "Euro area",      "\U0001F1EA\U0001F1FA"),
    ("DE", "Germany",        "\U0001F1E9\U0001F1EA"),
    ("FR", "France",         "\U0001F1EB\U0001F1F7"),
    ("IT", "Italy",          "\U0001F1EE\U0001F1F9"),
    ("ES", "Spain",          "\U0001F1EA\U0001F1F8"),
    ("GB", "United Kingdom", "\U0001F1EC\U0001F1E7"),
    ("IE", "Ireland",        "\U0001F1EE\U0001F1EA"),
    ("JP", "Japan",          "\U0001F1EF\U0001F1F5"),
    ("CN", "China",          "\U0001F1E8\U0001F1F3"),
    ("IN", "India",          "\U0001F1EE\U0001F1F3"),
    ("AU", "Australia",      "\U0001F1E6\U0001F1FA"),
    ("BR", "Brazil",         "\U0001F1E7\U0001F1F7"),
    ("CA", "Canada",         "\U0001F1E8\U0001F1E6"),
    ("MX", "Mexico",         "\U0001F1F2\U0001F1FD"),
    ("KR", "South Korea",    "\U0001F1F0\U0001F1F7"),
]

# --- OECD CLI (amplitude adjusted, LT avg = 100) FRED series ids -------------
#     Pattern: {ISO3}LOLITOAASTSAM   ***VERIFY each id on first run***
SERIES_CLI = {
    "US": "USALOLITOAASTSAM", "EA": "EA19LOLITOAASTSAM", "DE": "DEULOLITOAASTSAM",
    "FR": "FRALOLITOAASTSAM", "IT": "ITALOLITOAASTSAM",  "ES": "ESPLOLITOAASTSAM",
    "GB": "GBRLOLITOAASTSAM", "JP": "JPNLOLITOAASTSAM",  "CN": "CHNLOLITOAASTSAM",
    "IN": "INDLOLITOAASTSAM", "AU": "AUSLOLITOAASTSAM",  "BR": "BRALOLITOAASTSAM",
    "CA": "CANLOLITOAASTSAM", "MX": "MEXLOLITOAASTSAM",  "KR": "KORLOLITOAASTSAM",
}
# --- OECD BCI (amplitude adjusted, LT avg = 100) FRED series ids -------------
#     Pattern: BSCICP03{ISO3}M665S   ***VERIFY / some emerging markets have no OECD BCI***
SERIES_BCI = {
    "US": "BSCICP03USM665S", "EA": "BSCICP03EZM665S", "DE": "BSCICP03DEM665S",
    "FR": "BSCICP03FRM665S", "IT": "BSCICP03ITM665S", "ES": "BSCICP03ESM665S",
    "GB": "BSCICP03GBM665S", "JP": "BSCICP03JPM665S", "AU": "BSCICP03AUM665S",
    "CA": "BSCICP03CAM665S", "MX": "BSCICP03MXM665S", "KR": "BSCICP03KRM665S",
}
# Ireland leading-indicator proxy = EU Commission ESI for Ireland (Eurostat on FRED).
# ***VERIFY*** id; alternatively Eurostat dataset ei_bssi_m_r2 (geo=IE, indic=BS-ESI-I).
IE_ESI_FRED = "IESILEIVSMEI"


def fred_series(series_id, start):
    """Return list[(date_str 'YYYY-MM-DD', float)] ascending, or [] on failure.
    Uses the public FRED CSV endpoint (no API key)."""
    if not series_id:
        return []
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s"
           % (series_id, start))
    try:
        req = Request(url, headers={"User-Agent": "gloriarms-data/1.0"})
        with urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8")
        out = []
        for row in csv.reader(io.StringIO(text)):
            if (len(row) >= 2 and row[1] not in (".", "", "value")
                    and not row[0].lower().startswith(("date", "observation"))):
                try: out.append((row[0], float(row[1])))
                except ValueError: pass
        return out
    except Exception as e:
        sys.stderr.write("WARN fred %s: %s\n" % (series_id, e))
        return []


def month_key(date_str):
    return date_str[:7]


def monthly_map(obs):
    d = {}
    for date_str, val in obs:
        d[month_key(date_str)] = val
    return d


def build():
    start = (dt.date.today() - dt.timedelta(days=40 * 20)).isoformat()

    cli_maps, bci_maps = {}, {}
    for code, _, _ in COUNTRIES:
        sid = IE_ESI_FRED if code == "IE" else SERIES_CLI.get(code, "")
        cli_maps[code] = monthly_map(fred_series(sid, start))
        time.sleep(0.15)
        bci_maps[code] = monthly_map(fred_series(SERIES_BCI.get(code, ""), start))
        time.sleep(0.15)

    anchor = cli_maps.get("US", {})
    cand = sorted(anchor.keys()) if anchor else sorted(
        {k for m in cli_maps.values() for k in m})
    if not cand:
        raise SystemExit("ERROR: no CLI data fetched - check series ids / network.")
    latest = cand[-1]

    y, m = int(latest[:4]), int(latest[5:7])
    months = []
    for _ in range(12):
        months.append("%04d-%02d" % (y, m))
        m -= 1
        if m == 0: m, y = 12, y - 1
    months = list(reversed(months))

    def series_for(mp):
        out, prev = [], None
        for mk in months:
            v = mp.get(mk, prev)
            out.append(round(v, 1) if v is not None else None)
            if v is not None: prev = v
        first = next((x for x in out if x is not None), None)
        return [x if x is not None else first for x in out]

    countries = []
    for code, name, flag in COUNTRIES:
        cli = series_for(cli_maps.get(code, {}))
        bmap = bci_maps.get(code, {})
        bci = series_for(bmap) if bmap else [None] * 12
        countries.append({"code": code, "name": name, "flag": flag,
                          "cli": cli, "bci": bci})

    data = {
        "meta": {
            "title": "Business Cycle Monitor - OECD Composite Leading Indicator (CLI) & Business Confidence Indicator (BCI)",
            "source": "OECD (CLI & BCI) - European Commission (euro area & Ireland) - where shown: China NBS, Japan Cabinet Office",
            "unit": "Index (100 = long-term trend; >100 above trend, <100 below trend)",
            "last_updated": dt.date.today().isoformat(),
            "latest_month": months[-1],
            "months": months,
            "note": "OECD CLI/BCI amplitude-adjusted; Ireland via EU Commission ESI. Reused under OECD/EU open-data terms with attribution.",
        },
        "countries": countries,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s | latest_month=%s | countries=%d | CLI ok=%d | BCI ok=%d"
          % (OUT, months[-1], len(countries),
             sum(1 for c in countries if any(v is not None for v in c["cli"])),
             sum(1 for c in countries if any(v is not None for v in c["bci"]))))


if __name__ == "__main__":
    build()
