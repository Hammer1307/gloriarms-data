#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
businesscycle_updater.py  -  GLORIARMS Business Cycle Monitor data builder
==========================================================================
Erzeugt businesscycle_data.json in dem Schema, das die Webseiten-Komponente
(BusinessCycleMonitor.astro) laedt von:
    https://hammer1307.github.io/gloriarms-data/businesscycle_data.json

Ausgabeschema (unveraendert):
{
  "meta": { "title","source","unit","last_updated"(YYYY-MM-DD),
            "latest_month"(YYYY-MM), "months":[12 x "YYYY-MM"], "note" },
  "countries": [ { "code","name","flag", "cli":[12 floats], "bci":[12 floats|null] }, ... 16 ]
}

DATENQUELLEN (frei, mit Quellenangabe weiterverwendbar) - Stand 07/2026:
  * OECD SDMX API (Originalquelle, KEIN API-Key noetig):
      Dataflow OECD.SDD.STES,DSD_STES@DF_CLI
      - CLI  = MEASURE "LI"     (Composite leading indicator, amplitude-adjusted)
      - BCI  = MEASURE "BCICP"  (Composite business confidence, amplitude-adjusted)
      deckt 14 Laender ab (US, DE, FR, IT, ES, GB, JP, CN, IN, AU, BR, CA, MX, KR)
  * EU-Kommission (GD ECFIN) BCS-Datei fuer Eurozone + Irland:
      ESI als Fruehindikator-Proxy - die OECD fuehrt weder ein Euroraum-Aggregat
      noch Irland im CLI.

WARUM NICHT FRED:
  Die frueher genutzten FRED-Spiegel sind tot bzw. blockiert:
  - BSCICP03..M665S (BCI) endet ueberall im Januar 2024.
  - EA19LOLITOAASTSAM / OECDLOLITOAASTSAM (CLI Euroraum) enden im November 2022.
  - IESILEIVSMEI (Irland) liefert 404.
  - Der fredgraph.csv-Endpunkt wird von Rechenzentren (GitHub Actions) blockiert.
  Die OECD-SDMX-API loest all das an der Quelle.

Sicherung: Werden weniger als BC_MIN_CLI_OK (Standard 12) Laender mit CLI-Daten
gefunden, bricht das Skript ab - damit nie eine leere/lueckenhafte Datei
veroeffentlicht wird ("letzter guter Stand" bleibt online).

Benoetigt: requests, openpyxl.   Lauf: python businesscycle_updater.py
"""

import os, sys, io, csv, json, zipfile, time, traceback, datetime as dt
import requests

OUT = os.environ.get("BC_OUT", "businesscycle_data.json")
MIN_CLI_OK = int(os.environ.get("BC_MIN_CLI_OK", "12"))
TIMEOUT = 60
UA = {"User-Agent": "gloriarms-data/2.0 (BusinessCycleMonitor)"}

OECD_URL = ("https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,4.1/"
            "{areas}.M.{measure}...AA...H?startPeriod={start}&format=csvfilewithlabels")
EC_BASE = ("https://ec.europa.eu/economy_finance/db_indicators/surveys/documents/"
           "series/nace2_ecfin_{vint}/main_indicators_sa_nace2.zip")

# --- 16 Laender (Russland bewusst ausgeschlossen) ---------------------------
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

# ISO2 -> OECD ISO3 (nur Laender, die die OECD im CLI fuehrt)
OECD_AREA = {
    "US": "USA", "DE": "DEU", "FR": "FRA", "IT": "ITA", "ES": "ESP", "GB": "GBR",
    "JP": "JPN", "CN": "CHN", "IN": "IND", "AU": "AUS", "BR": "BRA", "CA": "CAN",
    "MX": "MEX", "KR": "KOR",
}
ISO3_TO_ISO2 = {v: k for k, v in OECD_AREA.items()}
EC_PROXY = {"EA": "EA.ESI", "IE": "IE.ESI"}   # ESI als Proxy


def _get(url, attempts=3):
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            if i < attempts - 1:
                sys.stderr.write("WARN Abruf %d/%d fehlgeschlagen (%s) - neuer Versuch\n"
                                 % (i + 1, attempts, e))
                time.sleep(3 * (i + 1))
    raise last


def oecd_measure(measure, start):
    """Holt eine Kennzahl (LI oder BCICP) fuer alle OECD-Laender in EINEM Request.
    Rueckgabe: {iso2: {'YYYY-MM': float}}"""
    url = OECD_URL.format(areas="+".join(OECD_AREA.values()), measure=measure, start=start)
    r = _get(url)
    out = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        iso2 = ISO3_TO_ISO2.get(row.get("REF_AREA", ""))
        per, val = row.get("TIME_PERIOD", ""), row.get("OBS_VALUE", "")
        if not iso2 or not per or val in ("", None):
            continue
        try:
            out.setdefault(iso2, {})[per[:7]] = float(val)
        except (ValueError, TypeError):
            pass
    return out


def ec_esi():
    """ESI (Eurozone + Irland) aus der EC-BCS-Datei; neuester Vintage-Ordner automatisch.
    Rueckgabe: {iso2: {'YYYY-MM': float}}"""
    import openpyxl
    today = dt.date.today()
    for back in range(0, 4):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12; y -= 1
        vint = "%02d%02d" % (y % 100, m)
        try:
            r = requests.get(EC_BASE.format(vint=vint), headers=UA, timeout=TIMEOUT)
            if r.status_code != 200 or len(r.content) < 10000:
                continue
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            xname = [n for n in zf.namelist() if n.lower().endswith(".xlsx")][0]
            wb = openpyxl.load_workbook(io.BytesIO(zf.read(xname)), read_only=True, data_only=True)
            ws = wb["MONTHLY"]
            rows = ws.iter_rows(values_only=True)
            hdr = [str(h) if h is not None else "" for h in next(rows)]
            idx = {h: i for i, h in enumerate(hdr)}
            out = {k: {} for k in EC_PROXY}
            for row in rows:
                d0 = row[0]
                if not isinstance(d0, dt.datetime):
                    continue
                key = "%04d-%02d" % (d0.year, d0.month)
                for iso2, col in EC_PROXY.items():
                    if col in idx and row[idx[col]] is not None:
                        try: out[iso2][key] = float(row[idx[col]])
                        except (ValueError, TypeError): pass
            sys.stderr.write("INFO EC-BCS Vintage nace2_ecfin_%s geladen\n" % vint)
            return out
        except Exception as e:
            sys.stderr.write("WARN EC-BCS %s: %s\n" % (vint, e))
            continue
    sys.stderr.write("WARN EC-BCS nicht erreichbar - Eurozone/Irland bleiben leer\n")
    return {k: {} for k in EC_PROXY}


def send_alert(subject, body):
    """E-Mail-Benachrichtigung via Resend. Ohne Secrets wird uebersprungen."""
    key = os.environ.get("RESEND_API_KEY")
    to = os.environ.get("ALERT_TO"); frm = os.environ.get("ALERT_FROM")
    if not (key and to and frm):
        print("[ALERT] Resend nicht konfiguriert - Mail uebersprungen.", file=sys.stderr)
        return
    try:
        r = requests.post("https://api.resend.com/emails",
            headers={"Authorization": "Bearer %s" % key, "Content-Type": "application/json"},
            json={"from": frm, "to": [to], "subject": subject, "text": body}, timeout=30)
        print("[ALERT] Resend status %s" % r.status_code, file=sys.stderr)
    except Exception as e:
        print("[ALERT] Mailversand fehlgeschlagen: %s" % e, file=sys.stderr)


def build():
    start = (dt.date.today() - dt.timedelta(days=40 * 20)).strftime("%Y-%m")

    cli_maps = oecd_measure("LI", start)
    bci_maps = oecd_measure("BCICP", start)
    esi = ec_esi()
    for iso2, mp in esi.items():          # Eurozone + Irland: ESI als CLI-Proxy
        cli_maps[iso2] = mp

    cand = sorted(cli_maps.get("US", {}).keys()) or sorted(
        {k for m in cli_maps.values() for k in m})
    if not cand:
        raise RuntimeError("keine CLI-Daten geladen - OECD-API/Serien pruefen.")
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

    cli_ok = sum(1 for c in countries if any(v is not None for v in c["cli"]))
    bci_ok = sum(1 for c in countries if any(v is not None for v in c["bci"]))
    if cli_ok < MIN_CLI_OK:
        raise RuntimeError(
            "nur %d von %d Laendern mit CLI-Daten (Minimum %d) - Abbruch, damit keine "
            "leere/lueckenhafte Datei veroeffentlicht wird." % (cli_ok, len(countries), MIN_CLI_OK))

    data = {
        "meta": {
            "title": "Business Cycle Monitor - OECD Composite Leading Indicator (CLI) & Business Confidence Indicator (BCI)",
            "source": "OECD (CLI & BCI, amplitude-adjusted) - European Commission DG ECFIN (ESI for euro area & Ireland)",
            "unit": "Index (100 = long-term trend; >100 above trend, <100 below trend)",
            "last_updated": dt.date.today().isoformat(),
            "latest_month": months[-1],
            "months": months,
            "note": ("OECD CLI/BCI amplitude-adjusted (SDMX API). Euro area and Ireland are not "
                     "covered by the OECD CLI - EU Commission ESI is used as their leading-indicator "
                     "proxy (also 100 = long-term average). Reused under OECD/EU open-data terms "
                     "with attribution."),
        },
        "countries": countries,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s | latest_month=%s | countries=%d | CLI ok=%d | BCI ok=%d"
          % (OUT, months[-1], len(countries), cli_ok, bci_ok))


if __name__ == "__main__":
    try:
        build()
    except Exception as e:
        msg = ("Business-Cycle-Update FEHLGESCHLAGEN um %s UTC.\n\n%s\n\n"
               "Es wurde KEINE neue businesscycle_data.json geschrieben - der letzte gute "
               "Stand bleibt online.\n\nDetails:\n%s"
               % (dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"), e,
                  traceback.format_exc()))
        print(msg, file=sys.stderr)
        send_alert("Makro-Monitor: Business-Cycle-Update fehlgeschlagen", msg)
        sys.exit(1)
