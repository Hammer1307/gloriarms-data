# Daten-Seite Umbau — Handover (gloriarms-data)

**Ziel:** Die Website (bereits live) zieht ihre Zahlen aus dem Daten-Repo
`Hammer1307/gloriarms-data`. Damit **Macro Monitor** und **Business Cycle Monitor**
echte Daten zeigen — und AGB-/Lizenzsauber sind — muss das Daten-Repo umgebaut werden.
Alles hier ist **lizenzfrei** (FRED, U.S. EIA, OECD, EU-Kommission, COCHILCO/Banco Central de Chile).

Was die Website erwartet (Fetch beim Build):
- `https://hammer1307.github.io/gloriarms-data/marktdaten.json` → Macro Monitor
- `https://hammer1307.github.io/gloriarms-data/businesscycle_data.json` → Business Cycle Monitor

---

## Inhalt dieses Pakets
```
scripts/
  update_marktdaten.py         Macro-Updater — dein bestehender, VOLLSTÄNDIG umgebaut (Yahoo raus)
  businesscycle_updater.py     NEU — baut businesscycle_data.json (OECD CLI/BCI, 16 Länder)
  requirements.txt             requests, openpyxl
.github/workflows/
  update-gloriarms-data.yml    Actions: baut beide JSONs, committet, triggert Site-Rebuild
```

---

## 1) Macro Monitor — Yahoo raus (nur 6 Kacheln ändern sich)

Dein **bestehender** `marktdaten_updater.py` bleibt fast unverändert. Nur die sechs
früheren Yahoo-Kacheln werden neu bezogen; alles andere (BTP/OAT = EZB-Monatsschnitt,
NFP/Arbeitslosenquote/Claims/Löhne = BLS/DOL via FRED, ESI/Confidence = EU-Kommission)
war schon frei und bleibt.

| Kachel | Neue Quelle | ID |
|---|---|---|
| vix | Cboe **via FRED** | `VIXCLS` |
| brent | U.S. EIA **via FRED** | `DCOILBRENTEU` |
| wti | U.S. EIA **via FRED** | `DCOILWTICO` |
| gas (Henry Hub) | U.S. EIA **via FRED** | `DHHNGSP` |
| gold | **COCHILCO** (Chile, La Rueda Diaria) | datos.gob.cl (CKAN) |
| copper | **Banco Central de Chile** (BDE) | SIETE-Serie `PRE_TCO` |

**Umsetzung:** `scripts/update_marktdaten.py` ist **deine bestehende Datei, fertig umgebaut** —
einfach die aktuelle `update_marktdaten.py` im Daten-Repo (bzw. `gloriarms-data-automation/` und
`_ops/`) damit **ersetzen**. Konkret geändert wurde nur:
- `YAHOO`-Konstante, `yahoo()` und `daily()` **entfernt**.
- VIX/Brent/WTI/Erdgas holen jetzt **direkt** `fred("VIXCLS"/"DCOILBRENTEU"/"DCOILWTICO"/"DHHNGSP")`.
- **`cochilco_gold()`** (Gold via datos.gob.cl) und **`bcch_copper()`** (Kupfer via BCCh PRE_TCO, cents/lb→USD/lb) neu.
- Optionales `card["src"]` je Kachel gesetzt (Attribution; die Website labelt ohnehin per id).
Alles andere — FRED-Arbeitsmarkt, EZB-Spreads, EU-BCS, Plausibilitätsprüfung, „letzter guter Stand",
Fehler-Mail, Schema — **unverändert**. Getestet: Parser + Schema stimmen; 0 funktionale Yahoo-Reste.

**marktdaten.json-Schema (unverändert):** `schema, generatedAt, ecVintage,
cards[16]{id,group,kind,unit,dec,freq,asof,raw,delta,note?,src?}, series{id:{l:[],v:[]}}`.
Optional `card.src` setzen (die Website labelt ohnehin per id — VIX „Cboe via FRED", Öl „U.S. EIA",
Gold „COCHILCO (Chile)", Kupfer „COCHILCO / Banco Central de Chile").

---

## 2) Business Cycle Monitor — neu (`businesscycle_data.json`)

`scripts/businesscycle_updater.py` ist **komplett** und erzeugt exakt das Website-Schema:
```
meta{ title, source, unit, last_updated(YYYY-MM-DD), latest_month(YYYY-MM), months[12], note }
countries[16]{ code, name, flag, cli[12], bci[12|null] }
```
- **OECD CLI** (amplitude-adjusted, LT-Schnitt = 100) je Land, **via FRED** (Serienmuster
  `{ISO3}LOLITOAASTSAM`).
- **OECD BCI** (`BSCICP03{ISO3}M665S`); für Länder ohne OECD-BCI (China/Indien/Brasilien)
  bleibt `bci` = null → Tabelle zeigt „n/a".
- **Irland** ist nicht im OECD-CLI → **EU-Kommission ESI** als Frühindikator-Proxy.
- 16 Länder, **Russland raus**.

**Wichtig — vor dem ersten Lauf verifizieren:** FRED stellt einige OECD-MEI-Serien nach und
nach ein. Öffne jede ID in FRED und prüfe, ob sie noch aktualisiert wird; falls nicht, nimm
die aktuelle Serie aus dem **OECD Data Explorer** (Dataflow `OECD.SDD.STES,DSD_STES@DF_CLI`
bzw. `DF_BCI`) und trage sie oben in `SERIES_CLI`/`SERIES_BCI` ein. Das Skript ist fehlertolerant:
eine tote ID wird zu `null`, der Lauf bricht nicht ab.

---

## 3) Secrets (GitHub → Repo Settings → Secrets and variables → Actions)

> **Kein FRED-API-Key nötig** — beide Updater nutzen den öffentlichen FRED-CSV-Endpunkt.

| Secret | Wofür |
|---|---|
| `BCCH_USER`, `BCCH_PASS` | Kupfer via Banco Central de Chile (si3.bcentral.cl, gratis) |
| `COCHILCO_GOLD_URL` | CKAN-URL des COCHILCO-Gold-Datensatzes inkl. `resource_id` (siehe unten) |
| `SITE_BUILD_HOOK_URL` | Cloudflare-Pages-Deploy-Hook (löst Website-Rebuild aus) |
| `RESEND_API_KEY`, `ALERT_TO`, `ALERT_FROM` | optional — Fehler-Mail des Macro-Updaters |

**COCHILCO_GOLD_URL bestimmen:** auf `https://datos.gob.cl` den COCHILCO-Gold-/„La Rueda
Diaria"-Datensatz suchen, `resource_id` kopieren, setzen auf
`https://datos.gob.cl/api/3/action/datastore_search?resource_id=<ID>&sort=fecha desc&limit=400`.
Feldnamen (`fecha`/`valor`) einmalig in `_parse_cochilco()` gegen den echten Datensatz prüfen.

**Pre-Launch (Handover-Checkliste):** exakte Lizenz des COCHILCO-Datensatzes auf datos.gob.cl
bestätigen (CC-BY vs CC0) und Attribution entsprechend; OECD-/EU-/BCCh-Attribution übernehmen;
Rechtsfreigabe (avvocato).

---

## 4) Ablauf / Deploy

1. Skripte nach `scripts/` im Daten-Repo legen, Workflow nach `.github/workflows/`.
2. Secrets setzen. Serien-IDs + COCHILCO-Resource einmalig verifizieren.
3. `businesscycle_updater.py` lokal/manuell testen (`Run workflow`) → prüfen, dass
   `businesscycle_data.json` plausible Werte um 100 liefert und `latest_month` stimmt.
4. Macro-Updater mit dem Drop-in testen → `marktdaten.json` prüfen (Gold/Kupfer/Öl/VIX real).
5. Workflow läuft dann täglich (Macro) + monatlich (OECD). Er committet die JSONs **und**
   triggert den Website-Rebuild — die Seiten zeigen dann live die echten Daten.

**Reihenfolge zum Go-Live:** erst Daten-Repo live (echte JSONs), dann ist die bereits
hochgeladene Website automatisch mit echten Zahlen versorgt. TradingEconomics-/Yahoo-Feeds
im alten Updater vollständig abschalten.

---

## Attribution (auf den Seiten bereits gesetzt — hier zur Kontrolle)
- Macro: „Cboe via FRED" (VIX) · „U.S. EIA" (Brent/WTI/Henry Hub) · „COCHILCO (Chile)" (Gold) ·
  „COCHILCO / Banco Central de Chile" (Kupfer) · 