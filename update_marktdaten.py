#!/usr/bin/env python3
"""
Makro-Monitor - Datenupdater
============================
Holt alle (lizenzfreien) Quellen, berechnet die Renditespreads, prueft die
Werte auf Plausibilitaet und schreibt daraus marktdaten.json.

Design-Prinzipien:
- "Letzter guter Stand": marktdaten.json wird NUR geschrieben, wenn alle
  kritischen Quellen erfolgreich waren UND die Werte die Plausibilitaets-
  pruefung bestehen. Sonst Abbruch mit Fehlercode -> der CI-Workflow laedt
  nichts hoch, der alte Stand in R2 bleibt unangetastet.
- Bei jedem Fehlschlag geht eine E-Mail-Benachrichtigung raus (Resend API).

QUELLEN (Stand 07/2026) - ausschliesslich amtlich/offiziell, KEIN Yahoo:
  VIX          Cboe via FRED                (~1-2 Tage Nachlauf)
  Brent/WTI/Gas  U.S. EIA via FRED (Spot)   (~3-4 Werktage Nachlauf)
  Gold         LBMA Gold Price PM           (~1 Tag Nachlauf)
  Kupfer       FRED Weltmarktpreis          (MONATLICH, USD/t -> USD/lb)
  US-Arbeitsmarkt  FRED
  10J-Renditen/Spreads  EZB (Fallback OECD/FRED), monatlich
  EU-Fruehindikatoren   EC-BCS-Dateifeed, monatlich

Hinweis zum Oel-/Gas-Nachlauf: Die EIA veroeffentlicht ihre Spotpreise selbst mit
3-4 Werktagen Verzoegerung. Direkt bei der EIA abgefragt (hist_xls) ist der Stand
identisch mit FRED - der Umweg ueber FRED kostet also nichts. Tagesaktuelle Werte
gaebe es nur ueber (lizenzpflichtige) Boersen-/Futures-Daten.

Ausgabe: marktdaten.json. Exit 0 = Erfolg (Datei geschrieben), 1 = Fehler.
"""
import os, sys, io, csv, json, zipfile, datetime, traceback
import requests

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}&cosd={start}"
FRED_API = ("https://api.stlouisfed.org/fred/series/observations"
            "?series_id={id}&observation_start={start}&api_key={key}&file_type=json")
LBMA_GOLD = "https://prices.lbma.org.uk/json/gold_pm.json"   # offizieller LBMA Gold Price PM
LB_PER_TON = 2204.62262                                      # Umrechnung USD/t -> USD/lb
EC_BASE = "https://ec.europa.eu/economy_finance/db_indicators/surveys/documents/series/nace2_ecfin_{vint}/main_indicators_sa_nace2.zip"
UA = {"User-Agent": "Mozilla/5.0 (compatible; MakroMonitor/1.0)"}
MONTHS_DE = ["Jan","Feb","Maer","Apr","Mai","Jun","Jul","Aug","Sep","Okt","Nov","Dez"]
MONTHS_DE[2] = "Mär"
TIMEOUT = 60

# ---------------------------------------------------------------- fetch helpers
def _get(url, attempts=3, timeout=TIMEOUT):
    """HTTP-GET mit Wiederholversuchen. Schuetzt gegen Timeouts/Aussetzer der Quellen."""
    import time as _t
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            if i < attempts - 1:
                print("[warn] Abruf Versuch %d/%d fehlgeschlagen (%s) - neuer Versuch: %s"
                      % (i + 1, attempts, e, url[:80]), file=sys.stderr)
                _t.sleep(3 * (i + 1))
    raise last

def fred(series, start="2021-01-01"):
    """Offizielle FRED-API, wenn FRED_API_KEY gesetzt ist (empfohlen!).
    Ohne Key Rueckfall auf fredgraph.csv - dieser Web-UI-Endpunkt wird aus
    Rechenzentren (z. B. GitHub-Actions-Runner) haeufig blockiert bzw. haengt."""
    key = os.environ.get("FRED_API_KEY")
    out = []
    if key:
        r = _get(FRED_API.format(id=series, start=start, key=key))
        for o in r.json().get("observations", []):
            v = o.get("value")
            if v not in (".", "", None):
                try: out.append([o["date"], float(v)])
                except (ValueError, TypeError): pass
    else:
        print("[warn] FRED_API_KEY nicht gesetzt - nutze fredgraph.csv "
              "(aus Rechenzentren oft blockiert)", file=sys.stderr)
        r = _get(FRED.format(id=series, start=start))
        for row in csv.reader(io.StringIO(r.text)):
            if len(row) >= 2 and row[1] not in (".", "", "value"):
                try: out.append([row[0], float(row[1])])
                except ValueError: pass
    if not out:
        raise ValueError("FRED %s: keine Datenpunkte" % series)
    return out

def lbma_gold():
    """Offizieller LBMA Gold Price PM (USD/oz). Frei, ohne Key, ~1 Tag Nachlauf."""
    r = _get(LBMA_GOLD)
    out = []
    for x in r.json():
        d, v = x.get("d"), x.get("v")
        if d and v and v[0] is not None:
            try: out.append([d, round(float(v[0]), 2)])
            except (ValueError, TypeError): pass
    out.sort()
    if not out:
        raise ValueError("LBMA Gold: keine Datenpunkte")
    return out


def fred_copper_lb(start="2021-01-01"):
    """Weltmarktpreis Kupfer (FRED PCOPPUSDM, USD/Tonne, MONATLICH) -> USD/lb."""
    return [[d, round(v / LB_PER_TON, 3)] for d, v in fred("PCOPPUSDM", start)]


def ecb_irs(area, start="2021-01"):
    """Monatliche 10J-Konvergenzrendite (Maastricht) der EZB, ~1 Monat Nachlauf."""
    url = ("https://data-api.ecb.europa.eu/service/data/IRS/"
           "M.%s.L.L40.CI.0000.EUR.N.Z?startPeriod=%s&format=csvdata") % (area, start)
    r = _get(url)
    rows = list(csv.reader(io.StringIO(r.text)))
    hdr = rows[0]; ti = hdr.index("TIME_PERIOD"); vi = hdr.index("OBS_VALUE")
    out = []
    for row in rows[1:]:
        if row and len(row) > vi and row[vi]:
            out.append([row[ti] + "-01", float(row[vi])])
    out.sort()
    if not out:
        raise ValueError("ECB IRS %s: keine Datenpunkte" % area)
    return out

def yields_country(area, fred_id):
    """Bevorzugt EZB-Konvergenzrendite (frischer, ~1 Monat), sonst OECD via FRED (~2-3 Monate)."""
    try:
        return ecb_irs(area)
    except Exception as e:
        print("[warn] ECB IRS %s fehlgeschlagen (%s); Fallback FRED %s" % (area, e, fred_id), file=sys.stderr)
        return fred(fred_id)

def ec_bcs():
    """Laedt die EC-BCS-Datei aus dem jeweils neuesten Vintage-Ordner (Autoerkennung)."""
    today = datetime.date.today()
    tried = []
    for back in range(0, 4):
        y = today.year; m = today.month - back
        while m <= 0: m += 12; y -= 1
        vint = "%02d%02d" % (y % 100, m)
        url = EC_BASE.format(vint=vint); tried.append(vint)
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            if r.status_code != 200 or len(r.content) < 10000:
                continue
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            xname = [n for n in zf.namelist() if n.lower().endswith(".xlsx")][0]
            data = _parse_ec_xlsx(zf.read(xname))
            data["_vintage"] = "nace2_ecfin_%s" % vint
            return data
        except Exception:
            continue
    raise RuntimeError("EC-BCS-Datei nicht gefunden. Versuchte Vintages: %s" % tried)

def _parse_ec_xlsx(xbytes):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(xbytes), read_only=True, data_only=True)
    ws = wb["MONTHLY"]
    rows = ws.iter_rows(values_only=True)
    hdr = [str(h) if h is not None else "" for h in next(rows)]
    idx = {h: i for i, h in enumerate(hdr)}
    want = {"ESI": "EA.ESI", "IndConf": "EA.INDU", "ConsConf": "EA.CONS", "SvcConf": "EA.SERV"}
    for col in want.values():
        if col not in idx:
            raise ValueError("EC-XLSX: Spalte %s fehlt" % col)
    series = {k: [] for k in want}
    for r in rows:
        dt = r[0]
        if not isinstance(dt, datetime.datetime) or dt.year < 2021:
            continue
        key = "%d-%02d-01" % (dt.year, dt.month)
        for k, col in want.items():
            v = r[idx[col]]
            if v is not None:
                try: series[k].append([key, round(float(v), 2)])
                except (ValueError, TypeError): pass
    for k in series:
        series[k].sort()
    return series

# ---------------------------------------------------------------- computation
def compute_spreads(d):
    def m(s): return {x[0][:7]: x[1] for x in s}
    de, it, fr = m(d["Y_DE"]), m(d["Y_IT"]), m(d["Y_FR"])
    months = sorted(set(de) & set(it) & set(fr))
    if not months:
        raise ValueError("Spreads: keine gemeinsamen Monate DE/IT/FR")
    d["BTP_Bund"] = [[mm + "-01", round((it[mm] - de[mm]) * 100, 1)] for mm in months]
    d["OAT_Bund"] = [[mm + "-01", round((fr[mm] - de[mm]) * 100, 1)] for mm in months]

# ---------------------------------------------------------------- plausibility
BOUNDS = {
    "VIX": (5, 150), "Brent": (10, 250), "WTI": (10, 250), "Gold": (300, 15000),
    "HenryHub": (0.3, 60), "Copper": (0.5, 30), "Unemp": (0.5, 30),
    "Claims": (50000, 3000000), "ESI": (40, 140), "IndConf": (-60, 40),
    "ConsConf": (-60, 40), "SvcConf": (-60, 40),
    "BTP_Bund": (-50, 1500), "OAT_Bund": (-50, 1500),
}
def sanity(d):
    problems = []
    for k, (lo, hi) in BOUNDS.items():
        if k not in d or not d[k]:
            problems.append("%s: fehlt/leer" % k); continue
        v = d[k][-1][1]
        if not (lo <= v <= hi):
            problems.append("%s: %s ausserhalb [%s,%s]" % (k, v, lo, hi))
    return problems

# ---------------------------------------------------------------- payload build
def fmt(v, dec=2): return ("{:,.%df}" % dec).format(v).replace(",", "X").replace(".", ",").replace("X", ".")
def last(s): return s[-1][1]
def prev(s, n=1): return s[-1-n][1] if len(s) > n else s[0][1]
def spark(s, n=60): return [x[1] for x in s[-n:]]
def dates(s, n=60): return [x[0] for x in s[-n:]]
def asof(s, freq):
    y, mo, dd = s[-1][0].split("-")
    return ("%s %s" % (MONTHS_DE[int(mo)-1], y)) if freq == "m" else ("%s.%s.%s" % (dd, mo, y))

def build_payload(d):
    """Sprachneutrales Schema: IDs + rohe Zahlen; Beschriftung/Formatierung macht das Widget."""
    nfp_chg = d["NFP"][-1][1] - d["NFP"][-2][1]
    wages_yoy = (d["Wages"][-1][1] / d["Wages"][-13][1] - 1) * 100
    def af(s): return s[-1][0]                      # asof als ISO-Datum
    def dl(s): return round(last(s) - prev(s), 1)   # Veraenderung (1 Nachkommastelle)
    cards = [
        {"id":"vix","group":"sent","kind":"num","dec":2,"unit":"pts","freq":"d","asof":af(d["VIX"]),"raw":round(last(d["VIX"]),2),"delta":dl(d["VIX"])},
        {"id":"btp","group":"sent","kind":"num","dec":1,"unit":"bps","freq":"m","asof":af(d["BTP_Bund"]),"raw":round(last(d["BTP_Bund"]),1),"delta":dl(d["BTP_Bund"]),"note":"ecbAvg"},
        {"id":"oat","group":"sent","kind":"num","dec":1,"unit":"bps","freq":"m","asof":af(d["OAT_Bund"]),"raw":round(last(d["OAT_Bund"]),1),"delta":dl(d["OAT_Bund"]),"note":"ecbAvg"},
        {"id":"brent","group":"comm","kind":"cur","dec":2,"unit":"usdbbl","freq":"d","asof":af(d["Brent"]),"raw":round(last(d["Brent"]),2),"delta":dl(d["Brent"]),"note":"eiaLag"},
        {"id":"wti","group":"comm","kind":"cur","dec":2,"unit":"usdbbl","freq":"d","asof":af(d["WTI"]),"raw":round(last(d["WTI"]),2),"delta":dl(d["WTI"]),"note":"eiaLag"},
        {"id":"gold","group":"comm","kind":"cur","dec":2,"unit":"usdoz","freq":"d","asof":af(d["Gold"]),"raw":round(last(d["Gold"]),2),"delta":dl(d["Gold"]),"note":"lbma"},
        {"id":"gas","group":"comm","kind":"cur","dec":2,"unit":"usdmmbtu","freq":"d","asof":af(d["HenryHub"]),"raw":round(last(d["HenryHub"]),2),"delta":dl(d["HenryHub"]),"note":"eiaLag"},
        {"id":"copper","group":"comm","kind":"cur","dec":2,"unit":"usdlb","freq":"m","asof":af(d["Copper"]),"raw":round(last(d["Copper"]),2),"delta":dl(d["Copper"]),"note":"monthly"},
        {"id":"nfp","group":"lab","kind":"signk","dec":0,"unit":"mom","freq":"m","asof":af(d["NFP"]),"raw":round(nfp_chg,0),"delta":round(nfp_chg,0)},
        {"id":"unemp","group":"lab","kind":"num","dec":1,"unit":"pct","freq":"m","asof":af(d["Unemp"]),"raw":round(last(d["Unemp"]),1),"delta":dl(d["Unemp"])},
        {"id":"claims","group":"lab","kind":"k","dec":0,"unit":"weekly","freq":"d","asof":af(d["Claims"]),"raw":round(last(d["Claims"])/1000,0),"delta":round((last(d["Claims"])-prev(d["Claims"]))/1000,1)},
        {"id":"wages","group":"lab","kind":"pct","dec":1,"unit":"usdlevel","freq":"m","asof":af(d["Wages"]),"raw":round(wages_yoy,1),"delta":None,"extra":round(last(d["Wages"]),2)},
        {"id":"esi","group":"eu","kind":"num","dec":1,"unit":"avg100","freq":"m","asof":af(d["ESI"]),"raw":round(last(d["ESI"]),1),"delta":dl(d["ESI"])},
        {"id":"ind","group":"eu","kind":"num","dec":1,"unit":"balance","freq":"m","asof":af(d["IndConf"]),"raw":round(last(d["IndConf"]),1),"delta":dl(d["IndConf"])},
        {"id":"cons","group":"eu","kind":"num","dec":1,"unit":"balance","freq":"m","asof":af(d["ConsConf"]),"raw":round(last(d["ConsConf"]),1),"delta":dl(d["ConsConf"])},
        {"id":"svc","group":"eu","kind":"num","dec":1,"unit":"balance","freq":"m","asof":af(d["SvcConf"]),"raw":round(last(d["SvcConf"]),1),"delta":dl(d["SvcConf"])},
    ]
    nfp_series = [[d["NFP"][i][0][:7], round(d["NFP"][i][1]-d["NFP"][i-1][1], 0)]
                  for i in range(len(d["NFP"])-24, len(d["NFP"]))]
    series = {
        "vix": {"l": dates(d["VIX"],120), "v": spark(d["VIX"],120)},
        "gold": {"l": dates(d["Gold"],180), "v": spark(d["Gold"],180)},
        "gas": {"l": dates(d["HenryHub"],180), "v": spark(d["HenryHub"],180)},
        "copper": {"l": dates(d["Copper"],180), "v": spark(d["Copper"],180)},
        "unemp": {"l": dates(d["Unemp"],48), "v": spark(d["Unemp"],48)},
        "claims": {"l": dates(d["Claims"],104), "v": spark(d["Claims"],104)},
        "btp": {"l": [x[0] for x in d["BTP_Bund"]], "v": [x[1] for x in d["BTP_Bund"]]},
        "oat": {"v": [x[1] for x in d["OAT_Bund"]]},
        "brent": {"l": dates(d["Brent"],250), "v": spark(d["Brent"],250)},
        "wti": {"v": spark(d["WTI"],250)},
        "nfp": {"l": [x[0] for x in nfp_series], "v": [x[1] for x in nfp_series]},
        "esi": {"l": [x[0] for x in d["ESI"]], "v": [x[1] for x in d["ESI"]]},
        "ind": {"v": [x[1] for x in d["IndConf"]]},
        "cons": {"v": [x[1] for x in d["ConsConf"]]},
        "svc": {"v": [x[1] for x in d["SvcConf"]]},
    }
    return {
        "schema": 2,
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ecVintage": d.get("_vintage", ""),
        "cards": cards,
        "series": series,
    }

# ---------------------------------------------------------------- alerting
def send_alert(subject, body):
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

# ---------------------------------------------------------------- main
def main():
    d = {}
    fred_map = {"NFP":"PAYEMS","Unemp":"UNRATE","Claims":"ICSA","Wages":"CES0500000003"}
    for name, sid in fred_map.items():
        d[name] = fred(sid)
    # 10J-Renditen DE/IT/FR fuer die Spreads: EZB (Juni-Stand) mit FRED-Fallback
    import time as _t
    d["Y_DE"] = yields_country("DE", "IRLTLT01DEM156N"); _t.sleep(1.5)
    d["Y_IT"] = yields_country("IT", "IRLTLT01ITM156N"); _t.sleep(1.5)
    d["Y_FR"] = yields_country("FR", "IRLTLT01FRM156N")
    # Marktpreise - ausschliesslich amtliche/offizielle Quellen (kein Yahoo):
    #   VIX      Cboe via FRED            (~1-2 Tage Nachlauf)
    #   Oel/Gas  EIA via FRED (Spotpreis) (~3-4 Werktage Nachlauf - so veroeffentlicht
    #            die EIA; direkt bei der EIA abgefragt ist es exakt derselbe Stand)
    #   Gold     LBMA Gold Price PM       (~1 Tag Nachlauf)
    #   Kupfer   FRED Weltmarktpreis      (MONATLICH, USD/t -> USD/lb)
    d["VIX"] = fred("VIXCLS")
    d["Brent"] = fred("DCOILBRENTEU")
    d["WTI"] = fred("DCOILWTICO")
    d["HenryHub"] = fred("DHHNGSP")
    d["Gold"] = lbma_gold()
    d["Copper"] = fred_copper_lb()
    ec = ec_bcs()
    for k in ("ESI","IndConf","ConsConf","SvcConf"):
        d[k] = ec[k]
    d["_vintage"] = ec["_vintage"]
    compute_spreads(d)

    problems = sanity(d)
    if problems:
        raise ValueError("Plausibilitaetspruefung fehlgeschlagen:\n- " + "\n- ".join(problems))

    payload = build_payload(d)
    tmp = "marktdaten.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, "marktdaten.json")
    print("OK - marktdaten.json geschrieben. EC-Vintage %s, generatedAt %s, %d Kacheln." %
          (payload["ecVintage"], payload["generatedAt"], len(payload["cards"])))
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        msg = ("Makro-Monitor Update FEHLGESCHLAGEN um %s UTC.\n\n%s\n\n"
               "Es wurde KEINE neue Datei geschrieben - der letzte gute Stand bleibt online.\n\n"
               "Details:\n%s") % (datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M"), e, traceback.format_exc())
        print(msg, file=sys.stderr)
        send_alert("Makro-Monitor: Datenupdate fehlgeschlagen", msg)
        sys.exit(1)
