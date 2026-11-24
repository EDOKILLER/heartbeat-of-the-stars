# -*- coding: utf-8 -*-
"""
IL BATTITO DELLE STELLE 1.16 - MOTORE (astrosismologia)
=====================================================
© 2026 Edoardo Battistel

Senza finestra: risolve il nome della stella, scarica da MAST le curve di luce Kepler/TESS,
le pulisce, calcola lo spettro di potenza (DFT esatta sui tempi veri, tramite FFT + sviluppo
di Taylor degli scarti dalla griglia), trova νmax e Δν, ricava massa, raggio, gravità, densità
con le relazioni di scala e l'età con le tracce MIST, confronta con APOKASC-3/APOKASC-2/Yu+2018,
e scrive scheda PDF/PNG (in inglese), CSV e suono WAV.
"""

import csv
import io
import json
import math
import os
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore", message="Covariance of the parameters could not be estimated")

VERSIONE = "1.16"
FIRMA = "© 2026 Edoardo Battistel"
UA = "BattitoStelle/1.16 (astrosismologia)"

NUMAX_SOLE = 3090.0      # µHz
DNU_SOLE = 135.1         # µHz
TEFF_SOLE = 5777.0       # K
LOGG_SOLE = 4.438

MAST = "https://mast.stsci.edu/api/v0/invoke"
MAST_DL = "https://mast.stsci.edu/api/v0.1/Download/file?uri="
VIZIER = "https://vizier.cfa.harvard.edu/viz-bin/asu-tsv"
GAIA_ESA = "https://gea.esac.esa.int/tap-server/tap/sync"
MIST_URL = "https://waps.cfa.harvard.edu/MIST/data/tarballs_v1.2/MIST_v1.2_feh_{feh}_afe_p0.0_vvcrit0.4_EEPS.txz"
MIST_FEH = [-2.0, -1.75, -1.5, -1.25, -1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5]

CARTELLA_DATI = Path.home() / ".battito_stelle"
CACHE = CARTELLA_DATI / "cache"

# bit di QUALITY scartati (uguali per Kepler e TESS): assetto, safe mode, puntamento grossolano,
# puntamento a Terra, Argabrightening, desaturazione, esclusione manuale
BIT_CATTIVI = 1 | 2 | 4 | 8 | 16 | 32 | 128


class Interrotto(Exception):
    pass


def cartella_risultati_predefinita():
    for nome in ("Scrivania", "Desktop"):
        d = Path.home() / nome
        if d.is_dir():
            return d / "Astronomia" / "Battito_stelle"
    return Path.home() / "Battito_stelle"


# =============================================================================
# RETE
# =============================================================================
def richiesta(url, dati=None, tempo=60, intestazioni=None, tentativi=2, ferma=None):
    """GET/POST con tempo massimo e una riprova dopo un errore di rete o 5xx."""
    h = {"User-Agent": UA}
    if intestazioni:
        h.update(intestazioni)
    if dati is not None and not isinstance(dati, bytes):
        dati = urllib.parse.urlencode(dati).encode()
    ultimo = None
    for i in range(tentativi):
        if ferma is not None and ferma.is_set():
            raise Interrotto()
        try:
            req = urllib.request.Request(url, data=dati, headers=h)
            with urllib.request.urlopen(req, timeout=tempo) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            ultimo = e
        if i < tentativi - 1:
            time.sleep(5)
    raise ultimo


def mast(servizio, params, tempo=90, ferma=None):
    corpo = {"service": servizio, "format": "json", "params": params}
    b = richiesta(MAST, {"request": json.dumps(corpo)}, tempo=tempo, ferma=ferma)
    d = json.loads(b.decode("utf-8"))
    st = str(d.get("status") or "") if isinstance(d, dict) else ""
    if st.upper().startswith("ERROR"):
        raise RuntimeError(f"MAST {servizio}: {d.get('msg')}")
    return d


# ---------------------------------------------------------------- VizieR TSV
def leggi_tsv_vizier(testo):
    """Divide una risposta asu-tsv in tabelle: lista di (nome_tabella, colonne, righe di dict)."""
    tabelle, nome, linee = [], "", testo.splitlines()
    i = 0
    while i < len(linee):
        l = linee[i]
        if l.startswith("#Name:"):
            nome = l.split(":", 1)[1].strip()
        if l and not l.startswith("#"):
            colonne = [c.strip() for c in l.split("\t")]
            i += 1
            while i < len(linee) and linee[i] and not linee[i].startswith("-") and not linee[i].startswith("#"):
                i += 1          # riga delle unità
            if i < len(linee) and linee[i].startswith("-"):
                i += 1
            righe = []
            while i < len(linee) and linee[i].strip() and not linee[i].startswith("#"):
                valori = linee[i].split("\t")
                righe.append({c: (valori[k].strip() if k < len(valori) else "") for k, c in enumerate(colonne)})
                i += 1
            tabelle.append((nome, colonne, righe))
            continue
        i += 1
    return tabelle


def numero(x):
    try:
        v = float(str(x).strip())
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


CATALOGHI = [
    # (sorgente VizieR, etichetta, mappa campo -> colonne possibili)
    ("J/ApJS/276/69", "APOKASC-3", {"numax": ["Numax"], "dnu": ["DNu"], "mass": ["Mass"], "radius": ["Radius"],
                                     "teff": ["Teff"], "feh": ["[Fe/H]"], "state": ["EvolSt"],
                                     "age_rgb": ["AgeRGB"], "age_rc": ["AgeRC"], "gaia": ["GaiaDR3"]}),
    ("J/ApJS/239/32", "APOKASC-2", {"numax": ["Numax"], "dnu": ["Dnu"], "mass": ["M(cor)"], "radius": ["R(cor)"],
                                     "teff": ["Teff"], "feh": ["FeH"], "state": ["ES"], "logage": ["LogAge"]}),
    ("J/ApJS/236/42", "Yu+2018", {"numax": ["numax"], "dnu": ["Delnu"], "mass": ["NoCorM"], "radius": ["NoCorR"],
                                   "teff": ["Teff"], "feh": ["[Fe/H]"], "phase": ["Phase"]}),
]


def cerca_cataloghi(kic, log=print, ferma=None):
    """Valori pubblicati per un KIC (VizieR di Harvard). Restituisce {etichetta: {campo: valore}}.
    Scrive nel registro l'esito di ogni catalogo; se la risposta è vuota o strana riprova una volta."""
    trovati = {}
    for sorgente, etichetta, mappa in CATALOGHI:
        esito = "nessuna risposta"
        for tentativo in range(2):
            try:
                par = {"-source": sorgente, "-out": "**", "-out.max": "5", "KIC": str(int(kic))}
                t = richiesta(VIZIER + "?" + urllib.parse.urlencode(par), tempo=60, ferma=ferma).decode("utf-8", "replace")
            except Interrotto:
                raise
            except Exception as e:
                esito = f"non raggiungibile ({type(e).__name__}: {e})"
                continue
            if "#Name:" not in t and "#RESOURCE" not in t:
                esito = f"risposta inattesa ({len(t)} caratteri: {t[:80]!r})"
                time.sleep(3)
                continue
            esito = "stella non presente"
            for nome, colonne, righe in leggi_tsv_vizier(t):
                if not righe or "KIC" not in colonne:
                    continue
                r = righe[0]
                val = {}
                for campo, possibili in mappa.items():
                    for c in possibili:
                        if c in r and r[c] != "":
                            val[campo] = r[c] if campo in ("state", "gaia") else numero(r[c])
                            break
                if val.get("numax") or val.get("dnu"):
                    val["tabella"] = nome
                    trovati[etichetta] = val
                    esito = f"trovata (tabella {nome})"
                    break
            break
        log(f"   {etichetta}: {esito}")
    return trovati


def testo_fase(v):
    """Fase evolutiva scritta in parole (APOKASC: RGB/RC; Yu+2018: 0 non classificata, 1 RGB, 2 HeB)."""
    if v.get("state"):
        return str(v["state"])
    p = v.get("phase")
    if p is None:
        return "–"
    return {0: "non classificata", 1: "RGB", 2: "RC (combustione dell'elio)"}.get(int(p), str(p))


def stato_dai_cataloghi(cataloghi):
    """RGB o RC se un catalogo lo dice (misurato con i modi misti), altrimenti None."""
    for nome in ("APOKASC-3", "APOKASC-2"):
        s_ = str(cataloghi.get(nome, {}).get("state") or "").upper()
        if s_.startswith("RGB"):
            return "RGB"
        if s_.startswith("RC") or "CL" in s_:
            return "RC"
    p = cataloghi.get("Yu+2018", {}).get("phase")
    if p is not None and int(p) in (1, 2):
        return "RGB" if int(p) == 1 else "RC"
    return None


# =============================================================================
# TROVA STELLE (cataloghi interi tenuti sul disco)
# =============================================================================
STELLE_FAMOSE = [
    # (nome mostrato, testo per l'analisi, descrizione)
    ("16 Cyg A", "KIC 12069424", "quasi gemella del Sole (Kepler, 1 min)"),
    ("16 Cyg B", "KIC 12069449", "la compagna di 16 Cyg A, un po' più piccola"),
    ("Gemma", "KIC 11026764", "subgigante con modi misti"),
    ("Perky", "KIC 6106415", "stella tipo Sole"),
    ("Doris", "KIC 8006161", "stella tipo Sole ricca di metalli"),
    ("Kepler-444", "KIC 6278762", "stella antica (circa 11 miliardi di anni) con cinque piccoli pianeti"),
    ("Kepler-56", "KIC 6448890", "gigante rossa con pianeti su orbite inclinate"),
    ("Kepler-91", "KIC 8219268", "gigante rossa con un pianeta molto vicino"),
    ("KIC 4448777", "KIC 4448777", "giovane gigante rossa: misurata la rotazione del nucleo"),
    ("KIC 8410637", "KIC 8410637", "gigante rossa in un sistema doppio a eclisse"),
    ("KIC 757137", "KIC 757137", "gigante rossa da manuale (prova del programma)"),
    ("ν Indi", "nu Indi", "TESS: stella antica dell'alone della Galassia"),
    ("TOI-197", "HD 221416", "TESS: subgigante con un pianeta simile a Saturno"),
    ("β Hyi", "beta Hyi", "TESS: subgigante vicina, come sarà il Sole tra qualche miliardo di anni"),
]

CATALOGHI_INTERI = {
    "yu": ("J/ApJS/236/42", ("numax", "Phase"), "Yu+2018 (circa 16 000 giganti rosse di Kepler)"),
    "apokasc3": ("J/ApJS/276/69", ("Numax", "EvolSt"), "APOKASC-3 (giganti di Kepler con età)"),
}


def scarica_catalogo(chiave, log=print, ferma=None, giorni_validi=90):
    """Catalogo intero da VizieR Harvard, tenuto in ~/.battito_stelle/cache/cataloghi (rinnovato ogni
    90 giorni). Restituisce le righe (dict) della tabella che ha le colonne attese."""
    sorgente, colonne_attese, nome = CATALOGHI_INTERI[chiave]
    d = CACHE / "cataloghi"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{chiave}.tsv"
    testo = None
    if f.exists() and (time.time() - f.stat().st_mtime) < giorni_validi * 86400 and f.stat().st_size > 10000:
        testo = f.read_text(encoding="utf-8", errors="replace")
    else:
        log(f"Scarico il catalogo {nome} (solo la prima volta, qualche MB)…")
        par = {"-source": sorgente, "-out": "**", "-out.max": "unlimited"}
        testo = richiesta(VIZIER + "?" + urllib.parse.urlencode(par), tempo=300, ferma=ferma).decode("utf-8", "replace")
        if "#Name:" not in testo:
            raise RuntimeError("risposta inattesa da VizieR")
        f.write_text(testo, encoding="utf-8")
    for nome_t, colonne, righe in leggi_tsv_vizier(testo):
        if all(c in colonne for c in colonne_attese) and "KIC" in colonne and righe:
            return righe
    raise RuntimeError(f"nel catalogo {nome} non trovo la tabella con {', '.join(colonne_attese)}")


def _stella_da_riga(chiave, r):
    kic = numero(r.get("KIC"))
    if not kic:
        return None
    if chiave == "yu":
        p = numero(r.get("Phase"))
        fase = {1: "RGB", 2: "RC"}.get(int(p)) if p is not None else None
        return {"testo": f"KIC {int(kic)}", "nome": f"KIC {int(kic)}", "numax": numero(r.get("numax")),
                "dnu": numero(r.get("Delnu")), "fase": fase, "mag": numero(r.get("Kpmag")),
                "massa": numero(r.get("NoCorM")), "eta": None, "teff": numero(r.get("Teff")), "nota": "Yu+2018"}
    stato = str(r.get("EvolSt") or "").strip().upper()
    fase = "RGB" if stato.startswith("RGB") else "RC" if stato.startswith("RC") else None
    eta = numero(r.get("AgeRC")) if fase == "RC" else numero(r.get("AgeRGB"))
    if eta is None:
        eta = numero(r.get("AgeRGB")) or numero(r.get("AgeRC"))
    return {"testo": f"KIC {int(kic)}", "nome": f"KIC {int(kic)}", "numax": numero(r.get("Numax")),
            "dnu": numero(r.get("DNu")), "fase": fase, "mag": None, "massa": numero(r.get("Mass")),
            "eta": eta, "teff": numero(r.get("Teff")), "nota": "APOKASC-3"}


def filtra_stelle(stelle, fase=None, numax_min=None, numax_max=None, mag_max=None, eta_min=None, eta_max=None,
                  ordine="caso", quante=10, seme=None):
    """Filtri semplici e ordine: caso | brillanti | numax | vecchie | giovani."""
    out = []
    for s_ in stelle:
        if s_ is None or s_.get("numax") is None:
            continue
        if fase and s_.get("fase") != fase:
            continue
        if numax_min is not None and s_["numax"] < numax_min:
            continue
        if numax_max is not None and s_["numax"] > numax_max:
            continue
        if mag_max is not None and (s_.get("mag") is None or s_["mag"] > mag_max):
            continue
        if eta_min is not None and (s_.get("eta") is None or s_["eta"] < eta_min):
            continue
        if eta_max is not None and (s_.get("eta") is None or s_["eta"] > eta_max):
            continue
        out.append(s_)
    rng = np.random.default_rng(seme)
    if ordine == "brillanti":
        out.sort(key=lambda s_: (s_.get("mag") is None, s_.get("mag") or 99))
    elif ordine == "numax":
        out.sort(key=lambda s_: s_["numax"])
    elif ordine == "vecchie":
        out.sort(key=lambda s_: -(s_.get("eta") or -1))
    elif ordine == "giovani":
        out.sort(key=lambda s_: (s_.get("eta") is None, s_.get("eta") or 99))
    else:
        if len(out) > quante:
            out = [out[i] for i in rng.choice(len(out), quante, replace=False)]
    return out[:quante], len([1 for _ in out]) if ordine != "caso" else None


def trova_stelle(fonte, log=print, ferma=None, **filtri):
    """fonte: famose | yu | apokasc3. Restituisce (stelle, quante soddisfano i filtri)."""
    if fonte == "famose":
        return [{"testo": t, "nome": n, "nota": d, "numax": None, "dnu": None, "fase": None, "mag": None,
                 "massa": None, "eta": None} for n, t, d in STELLE_FAMOSE], len(STELLE_FAMOSE)
    righe = scarica_catalogo(fonte, log, ferma)
    stelle = [_stella_da_riga(fonte, r) for r in righe]
    tutte = [s_ for s_ in stelle if s_]
    scelte, _ = filtra_stelle(tutte, **filtri)
    # quante stelle in tutto soddisfano i filtri (senza il limite «quante»)
    tot, _ = filtra_stelle(tutte, **{**filtri, "quante": 10 ** 9, "ordine": "numax"})
    log(f"{len(tutte)} stelle nel catalogo, {len(tot)} rispettano i filtri, ne mostro {len(scelte)}")
    return scelte, len(tot)


# =============================================================================
# STELLA: NOMI E CATALOGO TIC
# =============================================================================
def interpreta_testo(testo):
    """Capisce che cosa ha scritto l'utente. Restituisce dict con tipo kic/tic/coord/percorso/nome."""
    s = testo.strip()
    if not s:
        return None
    p = Path(os.path.expanduser(s))
    if p.exists():
        return {"tipo": "percorso", "valore": str(p)}
    m = re.fullmatch(r"(?i)\s*(kic|kplr)\s*[-_ ]?\s*0*(\d{1,9})\s*", s)
    if m:
        return {"tipo": "kic", "valore": int(m.group(2))}
    m = re.fullmatch(r"(?i)\s*tic\s*[-_ ]?\s*0*(\d{1,10})\s*", s)
    if m:
        return {"tipo": "tic", "valore": int(m.group(1))}
    m = re.fullmatch(r"\s*0*(\d{1,10})\s*", s)
    if m:
        return {"tipo": "kic", "valore": int(m.group(1))}
    m = re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*[ ,;]\s*([-+]?\d+(?:\.\d+)?)\s*", s)
    if m:
        ra, dec = float(m.group(1)), float(m.group(2))
        if 0 <= ra < 360 and -90 <= dec <= 90:
            return {"tipo": "coord", "valore": (ra, dec)}
    return {"tipo": "nome", "valore": s}


def tic_per_filtri(filtri, ferma=None):
    d = mast("Mast.Catalogs.Filtered.Tic", {"columns": "*", "filters": filtri}, tempo=60, ferma=ferma)
    return d.get("data", [])


def tic_cono(ra, dec, raggio_gradi=0.003, ferma=None):
    d = mast("Mast.Catalogs.Tic.Cone", {"ra": ra, "dec": dec, "radius": raggio_gradi}, tempo=60, ferma=ferma)
    return d.get("data", [])


def risolvi_nome(nome, ferma=None):
    d = mast("Mast.Name.Lookup", {"input": nome, "format": "json"}, tempo=30, ferma=ferma)
    r = (d.get("resolvedCoordinate") or [])
    if not r:
        raise RuntimeError(f"MAST non riconosce il nome «{nome}»")
    return float(r[0]["ra"]), float(r[0]["decl"])


def risolvi_stella(testo, log=print, ferma=None):
    """Restituisce info: nome, chiave, kic, tic, ra, dec, gaia_dr2, tic (riga del catalogo)."""
    x = interpreta_testo(testo)
    if x is None:
        raise ValueError("riga vuota")
    info = {"testo": testo.strip(), "kic": None, "tic": None, "ra": None, "dec": None, "riga_tic": None}
    righe = []
    if x["tipo"] == "kic":
        info["kic"] = x["valore"]
        righe = tic_per_filtri([{"paramName": "KIC", "values": [str(x["valore"])]}], ferma)
    elif x["tipo"] == "tic":
        info["tic"] = x["valore"]
        righe = tic_per_filtri([{"paramName": "ID", "values": [str(x["valore"])]}], ferma)
    elif x["tipo"] in ("coord", "nome"):
        if x["tipo"] == "coord":
            ra, dec = x["valore"]
        else:
            ra, dec = risolvi_nome(x["valore"], ferma)
            log(f"   «{x['valore']}» → RA {ra:.5f}°, Dec {dec:+.5f}°")
        for raggio in (0.002, 0.006):
            righe = tic_cono(ra, dec, raggio, ferma)
            if righe:
                break
        if righe:
            # la più vicina, ma per i nomi preferisce la più brillante tra quelle entro 15″
            righe.sort(key=lambda r: (r.get("dstArcSec") or 99))
            if x["tipo"] == "nome":
                vicine = [r for r in righe if (r.get("dstArcSec") or 99) < 15 and r.get("Tmag") is not None]
                if vicine:
                    righe = sorted(vicine, key=lambda r: r["Tmag"])
        info["ra"], info["dec"] = ra, dec
    if righe:
        r = righe[0]
        info["riga_tic"] = r
        info["tic"] = info["tic"] or (int(r["ID"]) if r.get("ID") else None)
        if not info["kic"] and r.get("KIC"):
            try:
                info["kic"] = int(float(r["KIC"]))
            except (TypeError, ValueError):
                pass
        info["ra"] = r.get("ra") if r.get("ra") is not None else info["ra"]
        info["dec"] = r.get("dec") if r.get("dec") is not None else info["dec"]
    if x["tipo"] == "nome":
        info["nome"] = x["valore"]
    elif info["kic"]:
        info["nome"] = f"KIC {info['kic']}"
    elif info["tic"]:
        info["nome"] = f"TIC {info['tic']}"
    else:
        info["nome"] = info["testo"]
    if info["kic"]:
        info["chiave"] = f"KIC_{info['kic']}"
    elif info["tic"]:
        info["chiave"] = f"TIC_{info['tic']}"
    else:
        info["chiave"] = re.sub(r"[^A-Za-z0-9_+-]+", "_", info["nome"]).strip("_") or "stella"
    if x["tipo"] == "nome" and (info["kic"] or info["tic"]):
        info["chiave"] = re.sub(r"[^A-Za-z0-9_+-]+", "_", info["nome"]).strip("_") + "_" + info["chiave"]
    return info


# =============================================================================
# CURVE DI LUCE: ELENCO, SCARICAMENTO, LETTURA
# =============================================================================
def _osservazioni(collezione, target, ferma=None):
    d = mast("Mast.Caom.Filtered",
             {"columns": "obsid,obs_collection,obs_id,target_name,t_exptime,t_min,t_max,provenance_name,"
                         "dataproduct_type,sequence_number",
              "filters": [{"paramName": "obs_collection", "values": [collezione]},
                          {"paramName": "target_name", "values": [target]}]}, tempo=90, ferma=ferma)
    return d.get("data", [])


def _prodotti(obsid, ferma=None):
    return mast("Mast.Caom.Products", {"obsid": str(obsid)}, tempo=90, ferma=ferma).get("data", [])


def elenca_curve(info, missione="auto", cadenza="auto", log=print, ferma=None):
    """Lista di file disponibili: dict(missione, uri, nome, cadenza_s, size).
    cadenza: auto | lunga | corta | veloce (TESS 20 s)."""
    file_ = []
    if missione in ("auto", "kepler") and info.get("kic"):
        oss = _osservazioni("Kepler", f"kplr{int(info['kic']):09d}", ferma)
        lc = [o for o in oss if (o.get("t_exptime") or 0) > 1000]
        sc = [o for o in oss if 0 < (o.get("t_exptime") or 0) <= 1000]
        log(f"   Kepler: {len(lc)} serie a 30 min, {len(sc)} serie a 1 min")
        corta = cadenza in ("corta", "veloce")
        logg = numero((info.get("riga_tic") or {}).get("logg"))
        if cadenza == "auto" and logg is not None and logg >= 3.5:
            log(f"   log g (TIC) = {logg:.2f}: stella nana o subgigante, servono i dati a 1 min")
            corta = True
        if corta and not sc and info.get("ra") is not None:
            # stelle molto brillanti (es. 16 Cyg A/B): osservate con aperture speciali, archiviate con altri nomi
            try:
                d = mast("Mast.Caom.Cone", {"ra": float(info["ra"]), "dec": float(info["dec"]), "radius": 0.004},
                         tempo=90, ferma=ferma)
                vicine = [o for o in d.get("data", []) if o.get("obs_collection") == "Kepler"
                          and 0 < (o.get("t_exptime") or 0) <= 1000]
                if vicine:
                    nomi = sorted({str(o.get("target_name")) for o in vicine})
                    log(f"   trovati dati a 1 min con altri nomi (apertura speciale): {', '.join(nomi)}")
                    sc = vicine
            except Interrotto:
                raise
            except Exception as e:
                log(f"   ricerca per posizione non riuscita: {e}")
        scelte = sc if corta else lc
        if corta and not sc:
            log("   nessun dato Kepler a 1 min: uso quelli a 30 min")
            scelte, corta = lc, False
        suff = "slc.fits" if corta else "llc.fits"
        for o in scelte:
            for p in _prodotti(o["obsid"], ferma):
                n = str(p.get("productFilename", ""))
                if n.endswith(suff):
                    file_.append({"missione": "Kepler", "uri": p["dataURI"], "nome": n,
                                  "cadenza_s": 58.85 if n.endswith("slc.fits") else 1765.46,
                                  "size": p.get("size")})
    if file_ or missione == "kepler":
        return file_
    if not info.get("tic"):
        return file_
    oss = [o for o in _osservazioni("TESS", str(int(info["tic"])), ferma) if o.get("dataproduct_type") == "timeseries"]
    veloci = [o for o in oss if str(o.get("obs_id", "")).endswith("fast")]
    normali = [o for o in oss if not str(o.get("obs_id", "")).endswith("fast")]
    log(f"   TESS SPOC: {len(normali)} settori a 2 min, {len(veloci)} a 20 s")
    scelte = veloci if (cadenza == "veloce" and veloci) else normali
    for o in scelte:
        for p in _prodotti(o["obsid"], ferma):
            n = str(p.get("productFilename", ""))
            if n.endswith("lc.fits") and "dvt" not in n:
                file_.append({"missione": "TESS", "uri": p["dataURI"], "nome": n,
                              "cadenza_s": 20.0 if "fast" in n else 120.0, "size": p.get("size"),
                              "settore": o.get("sequence_number")})
    if not file_:
        # curve dalle immagini a pieno campo (HLSP TESS-SPOC e QLP)
        try:
            oss = _osservazioni("HLSP", str(int(info["tic"])), ferma)
            oss = [o for o in oss if str(o.get("provenance_name", "")).upper() in ("TESS-SPOC", "QLP")]
            log(f"   TESS da immagini a pieno campo (HLSP TESS-SPOC/QLP): {len(oss)} settori")
            preferiti = [o for o in oss if str(o.get("provenance_name", "")).upper() == "TESS-SPOC"] or oss
            for o in preferiti:
                for p in _prodotti(o["obsid"], ferma):
                    n = str(p.get("productFilename", ""))
                    if n.endswith("lc.fits"):
                        file_.append({"missione": "TESS", "uri": p["dataURI"], "nome": n,
                                      "cadenza_s": float(o.get("t_exptime") or 600), "size": p.get("size"),
                                      "settore": o.get("sequence_number")})
        except Interrotto:
            raise
        except Exception as e:
            log(f"   HLSP non disponibile: {e}")
    # se ci sono cadenze diverse, tiene la più comune
    if file_:
        cad = [round(f["cadenza_s"]) for f in file_]
        piu = max(set(cad), key=cad.count)
        file_ = [f for f, c in zip(file_, cad) if c == piu]
    return file_


def scarica(f, avanzamento=None, ferma=None):
    """Scarica (o prende dalla cache) un file di MAST; restituisce il percorso locale."""
    d = CACHE / f["missione"].lower()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f["nome"]
    if dest.exists() and dest.stat().st_size > 2880:
        return dest
    url = MAST_DL + urllib.parse.quote(f["uri"], safe=":/")
    parz = dest.with_suffix(dest.suffix + ".part")
    for tentativo in range(3):
        if ferma is not None and ferma.is_set():
            raise Interrotto()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r, open(parz, "wb") as out:
                while True:
                    if ferma is not None and ferma.is_set():
                        raise Interrotto()
                    blocco = r.read(1 << 18)
                    if not blocco:
                        break
                    out.write(blocco)
            with open(parz, "rb") as fh:
                if not fh.read(6).startswith(b"SIMPLE"):
                    raise RuntimeError("il file scaricato non è un FITS")
            parz.replace(dest)
            return dest
        except Interrotto:
            parz.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            err = e
        except Exception as e:
            err = e
        time.sleep(5 * (tentativo + 1))
    raise err


COLONNE_FLUSSO = ("PDCSAP_FLUX", "KSPSAP_FLUX", "DET_FLUX", "SAP_FLUX", "FLUX")


def leggi_curva(percorso):
    """Legge una curva di luce FITS (Kepler, TESS SPOC, HLSP). Restituisce dict."""
    from astropy.io import fits
    with fits.open(percorso, memmap=False) as h:
        h0 = h[0].header
        tab = None
        for hdu in h[1:]:
            if hasattr(hdu, "columns") and "TIME" in hdu.columns.names:
                tab = hdu
                break
        if tab is None:
            raise RuntimeError(f"{Path(percorso).name}: nessuna tabella con TIME")
        nomi = tab.columns.names
        col = next((c for c in COLONNE_FLUSSO if c in nomi), None)
        if col is None:
            raise RuntimeError(f"{Path(percorso).name}: nessuna colonna di flusso")
        t = np.array(tab.data["TIME"], dtype=float)
        fl = np.array(tab.data[col], dtype=float)
        q = np.array(tab.data["QUALITY"], dtype=np.int64) if "QUALITY" in nomi else np.zeros(t.size, np.int64)
        th = tab.header
        missione = str(h0.get("TELESCOP", th.get("TELESCOP", "?"))).strip()
        ogg = str(h0.get("OBJECT", "")).strip()
        info = {"missione": missione, "oggetto": ogg, "colonna": col,
                "teff": numero(h0.get("TEFF")), "logg": numero(h0.get("LOGG")), "feh": numero(h0.get("FEH")),
                "raggio": numero(h0.get("RADIUS")), "kic": h0.get("KEPLERID"), "tic": h0.get("TICID"),
                "ra": numero(h0.get("RA_OBJ")), "dec": numero(h0.get("DEC_OBJ")),
                "quarto": h0.get("QUARTER"), "settore": h0.get("SECTOR"),
                "cadenza_s": None}
        dt = np.nanmedian(np.diff(t[np.isfinite(t)])) if np.isfinite(t).sum() > 2 else None
        if dt:
            info["cadenza_s"] = float(dt * 86400.0)
    return {"t": t, "f": fl, "q": q, "info": info}


# =============================================================================
# PULIZIA
# =============================================================================
def _filtro_gauss_pesato(valori, pesi, sigma_celle):
    from scipy.ndimage import gaussian_filter1d
    num = gaussian_filter1d(valori * pesi, sigma_celle, mode="constant")
    den = gaussian_filter1d(pesi.astype(float), sigma_celle, mode="constant")
    with np.errstate(invalid="ignore", divide="ignore"):
        return num / den


def _andamento_lento(t, x, finestra_giorni, passo_giorni=0.02):
    """Andamento lento della curva (gaussiana pesata larga «finestra_giorni»), calcolato su una griglia
    di mezz'ora e interpolato sui tempi veri: con i dati a 1 minuto è decine di volte più veloce."""
    cella = max(passo_giorni, float(np.median(np.diff(t))) if t.size > 1 else passo_giorni)
    idx = np.floor((t - t[0]) / cella).astype(np.int64)
    n = int(idx[-1]) + 1
    v = np.bincount(idx, weights=x, minlength=n)
    w = np.bincount(idx, minlength=n).astype(float)
    tc = np.bincount(idx, weights=t, minlength=n)
    sigma = max(2.0, finestra_giorni / cella / 2.355)
    media = np.divide(v, w, out=np.zeros(n), where=w > 0)
    tr = _filtro_gauss_pesato(media, (w > 0).astype(float), sigma)
    ok = (w > 0) & np.isfinite(tr)
    if ok.sum() < 2:
        return np.full_like(x, np.nanmedian(x))
    tc = tc[ok] / w[ok]
    return np.interp(t, tc, tr[ok])


def pulisci_segmento(t, f, q, finestra_giorni):
    """Toglie punti cattivi, normalizza in ppm, toglie l'andamento lento (filtro passa-alto)."""
    ok = np.isfinite(t) & np.isfinite(f) & ((q & BIT_CATTIVI) == 0)
    t, f = t[ok], f[ok]
    if t.size < 50:
        return t[:0], f[:0]
    med = np.nanmedian(f)
    if not med or not np.isfinite(med) or med <= 0:
        return t[:0], f[:0]
    x = (f / med - 1.0) * 1e6
    for _ in range(3):
        r = x - _andamento_lento(t, x, finestra_giorni)
        c = np.nanmedian(r)
        mad = 1.4826 * np.nanmedian(np.abs(r - c))
        buoni = np.abs(r - c) < 4.5 * mad if mad > 0 else np.ones(r.size, bool)
        if buoni.all():
            break
        t, x = t[buoni], x[buoni]
    r = x - _andamento_lento(t, x, finestra_giorni)
    ok = np.isfinite(r)
    return t[ok], r[ok]


def unisci_segmenti(curve, log=print):
    """curve = lista di dict di leggi_curva. Restituisce t (giorni), x (ppm), dt (giorni), dettagli."""
    if not curve:
        raise RuntimeError("nessuna curva di luce")
    cad = np.median([c["info"]["cadenza_s"] or 1800 for c in curve])
    # finestra del filtro passa-alto: 8 giorni (Kepler) / 4 giorni (TESS, orbite di 13,7 d)
    kepler = any("KEPLER" in str(c["info"]["missione"]).upper() for c in curve)
    finestra = 8.0 if kepler else 4.0
    T, X = [], []
    for c in curve:
        t, x = pulisci_segmento(c["t"], c["f"], c["q"], finestra)
        if t.size:
            T.append(t)
            X.append(x)
    if not T:
        raise RuntimeError("dopo la pulizia non resta nessun punto")
    t = np.concatenate(T)
    x = np.concatenate(X)
    o = np.argsort(t)
    t, x = t[o], x[o]
    # tempi doppi (quarti sovrapposti): tiene il primo
    tieni = np.concatenate([[True], np.diff(t) > 1e-6])
    t, x = t[tieni], x[tieni]
    x = x - np.mean(x)
    return t, x, float(cad) / 86400.0, {"finestra_filtro_giorni": finestra, "n_segmenti": len(T)}


def pesi_rumore(t, x, giorni=10.0):
    """Pesi 1/σ² con σ = rumore locale (differenze tra punti vicini, stima robusta) in blocchi di
    «giorni»: i tratti più rumorosi della curva di luce contano meno. Media dei pesi = 1."""
    if t.size < 20:
        return np.ones_like(x)
    blocco = np.floor((t - t[0]) / giorni).astype(np.int64)
    d = np.abs(np.diff(x))
    b = blocco[1:]
    stesso = blocco[1:] == blocco[:-1]
    d, b = d[stesso], b[stesso]
    nb = int(blocco[-1]) + 1
    sigma = np.full(nb, np.nan)
    ordine = np.argsort(b, kind="stable")
    d, b = d[ordine], b[ordine]
    inizi = np.searchsorted(b, np.arange(nb))
    fini = np.searchsorted(b, np.arange(nb), side="right")
    for i in range(nb):
        if fini[i] - inizi[i] >= 5:
            sigma[i] = 1.4826 * np.median(d[inizi[i]:fini[i]]) / math.sqrt(2)
    ok = np.isfinite(sigma) & (sigma > 0)
    if not np.any(ok):
        return np.ones_like(x)
    sigma[~ok] = np.median(sigma[ok])
    w = 1.0 / sigma[blocco] ** 2
    return w / np.mean(w)


# =============================================================================
# SPETTRO DI POTENZA
# =============================================================================
def spettro_potenza(t, x, dt, sovracampionamento=None, n_taylor=None, nu_max_uHz=None, pesi=None):
    """Spettro di potenza esatto sui tempi veri (DFT), calcolato con FFT su griglia regolare più
    lo sviluppo di Taylor degli scarti δ = t - griglia (scarti ≤ dt/2).
    Restituisce freq (µHz), psd (ppm²/µHz), nyquist (µHz), risoluzione (µHz)."""
    from scipy.fft import rfft, next_fast_len
    t0 = t[0]
    k = np.round((t - t0) / dt).astype(np.int64)
    delta = (t - t0) - k * dt
    n = int(k[-1]) + 1
    if sovracampionamento is None:
        sovracampionamento = 2 if n < 3_000_000 else 1
    npad = next_fast_len(int(n * sovracampionamento))
    m = npad // 2 + 1
    freq_d = np.arange(m) / (npad * dt)                  # 1/giorno
    nyq_d = 0.5 / dt
    if nu_max_uHz:
        m = min(m, int(nu_max_uHz * 0.0864 * npad * dt) + 2)
        freq_d = freq_d[:m]
    if n_taylor is None:
        dmax = np.max(np.abs(delta)) if delta.size else 0.0
        z = 2 * np.pi * freq_d[-1] * dmax
        n_taylor = 1
        termine = 1.0
        while termine > 1e-7 and n_taylor < 40:
            termine *= z / n_taylor
            n_taylor += 1
    w = np.ones_like(x) if pesi is None else np.asarray(pesi, float)
    xw = x * w
    X = np.zeros(m, complex)
    coeff = np.ones(m, complex)
    fattore = -2j * np.pi * freq_d
    potenza_delta = np.ones_like(delta)
    for j in range(n_taylor):
        if j > 0:
            coeff = coeff * fattore / j
            potenza_delta = potenza_delta * delta
        a = np.bincount(k, weights=xw * potenza_delta, minlength=n)
        X += coeff * rfft(a, npad)[:m]
    dt_s = dt * 86400.0
    # con i pesi: PSD = 2 dt |Σ w x e^{-iωt}|² / Σ w² (senza pesi = 2 dt |X|² / N)
    psd = 2.0 * np.abs(X) ** 2 * dt_s / float(np.sum(w ** 2)) * 1e-6      # ppm²/µHz
    freq = freq_d / 0.0864                                   # 1/giorno -> µHz
    nyq = nyq_d / 0.0864
    ris = 1.0 / ((t[-1] - t[0]) * 0.0864)
    ok = freq > 0
    return freq[ok], psd[ok], nyq, ris


# =============================================================================
# ANALISI: νmax e Δν
# =============================================================================
def dnu_atteso(numax):
    return 0.263 * numax ** 0.772           # Stello et al. 2009


def fwhm_inviluppo(numax):
    return 0.66 * numax ** 0.88             # Mosser et al. 2012


def eta2(freq, nyq):
    return np.sinc(freq / (2.0 * nyq)) ** 2


def lisciamento_log(freq, psd, larghezza_dex=0.25, nbin=1500):
    """Fondo grezzo: mediana mobile in log(ν)."""
    lf = np.log10(freq)
    bordi = np.linspace(lf[0], lf[-1], nbin + 1)
    idx = np.clip(np.digitize(lf, bordi) - 1, 0, nbin - 1)
    somma = np.bincount(idx, weights=psd, minlength=nbin)
    cont = np.bincount(idx, minlength=nbin)
    media = np.where(cont > 0, somma / np.maximum(cont, 1), np.nan)
    centri = 0.5 * (bordi[1:] + bordi[:-1])
    passo = bordi[1] - bordi[0]
    w = max(3, int(larghezza_dex / passo))
    from scipy.ndimage import median_filter
    valido = np.isfinite(media)
    m2 = np.interp(centri, centri[valido], media[valido])
    lis = median_filter(np.log10(np.maximum(m2, 1e-30)), size=2 * w + 1, mode="nearest")
    return 10 ** np.interp(lf, centri, lis)


def _acf_normalizzata(s):
    s = s - np.mean(s)
    n = s.size
    from scipy.fft import rfft, irfft, next_fast_len
    nf = next_fast_len(2 * n)
    a = irfft(np.abs(rfft(s, nf)) ** 2, nf)[:n]
    return a / a[0] if a[0] > 0 else a


def pettine(freq, snr, centro, dnu_e, sovr, mezza=None, f_min=0.0, l1f=0.75, l2f=1.3):
    """Autocorrelazione dello spettro (appena lisciato) attorno a «centro»: cerca il picco alla
    distanza Δν (il «pettine» dei modi). Restituisce (Δν, picco, z, lag, acf) o None."""
    from scipy.ndimage import gaussian_filter1d
    df = freq[1] - freq[0]
    mezza = mezza or max(fwhm_inviluppo(centro), 3.0 * dnu_e)
    sel = (freq > max(centro - mezza, f_min)) & (freq < centro + mezza)
    s = snr[sel]
    if s.size < 30:
        return None
    sig = max(1.0, 0.02 * dnu_e / df)
    s = gaussian_filter1d(s - 1.0, sig)
    a = _acf_normalizzata(s)
    lag = np.arange(a.size) * df
    l1, l2 = int(l1f * dnu_e / df), min(int(l2f * dnu_e / df) + 1, a.size - 1)
    if l1 < 2 or l2 <= l1 + 2:
        return None
    j = l1 + int(np.argmax(a[l1:l2]))
    lung_corr = math.hypot(sovr, 3.5 * sig)
    z = float(a[j]) * math.sqrt(s.size / lung_corr)
    # rifinitura del picco con una gaussiana
    w = max(2, int(0.08 * dnu_e / df))
    i1, i2 = max(j - w, 1), min(j + w + 1, a.size)
    xx, yy = lag[i1:i2], a[i1:i2]
    d = float(lag[j])
    try:
        from scipy.optimize import curve_fit
        p, _ = curve_fit(_gauss, xx, yy, p0=[a[j] - yy.min(), lag[j], (xx[-1] - xx[0]) / 4, yy.min()], maxfev=3000)
        if xx[0] <= p[1] <= xx[-1]:
            d = float(p[1])
    except Exception:
        pass
    return d, float(a[j]), z, lag, a


def cerca_candidati(freq, psd, nyq, ris, sovr=2, quanti=3, intervallo=None):
    """Prova molti valori di νmax con due indicatori:
    - eccesso di potenza: rapporto medio spettro/fondo grezzo entro la larghezza attesa della gobba;
    - pettine: autocorrelazione dello spettro alla distanza Δν attesa.
    Restituisce i candidati migliori (νmax, Δν, z_pettine, z_eccesso), distinti tra loro."""
    fondo = lisciamento_log(freq, psd)
    snr = psd / fondo
    df = freq[1] - freq[0]
    nu_basso = max(4.0, (10 * ris / 0.263) ** (1 / 0.772))
    nu_alto = 0.9 * nyq
    if intervallo:
        nu_basso, nu_alto = max(nu_basso, intervallo[0]), min(nu_alto, intervallo[1])
    if nu_basso >= nu_alto:
        return [], None, None
    griglia = np.exp(np.linspace(np.log(nu_basso), np.log(nu_alto), 220))
    z_pet = np.zeros(griglia.size)
    z_ecc = np.zeros(griglia.size)
    dnus = np.zeros(griglia.size)
    cs = np.concatenate([[0.0], np.cumsum(snr)])
    for i, nu in enumerate(griglia):
        r = pettine(freq, snr, nu, dnu_atteso(nu), sovr)
        if r:
            dnus[i], z_pet[i] = r[0], r[2]
        sg = fwhm_inviluppo(nu) / 2.355
        i1, i2 = np.searchsorted(freq, [nu - sg, nu + sg])
        n = i2 - i1
        if n > 5:
            m = (cs[i2] - cs[i1]) / n
            z_ecc[i] = (m - 1.0) * math.sqrt(n / sovr)
    from scipy.ndimage import uniform_filter1d
    zp = uniform_filter1d(z_pet, 3)
    # punteggio combinato: il pettine conta di più; l'eccesso (limitato) aiuta con i dati corti
    punteggio = zp + 0.5 * np.clip(z_ecc, 0, 12)
    scelti = []
    for chiave in (punteggio, zp, z_ecc):
        for i in np.argsort(chiave)[::-1][:12]:
            if chiave[i] <= 0:
                break
            if all(abs(math.log(griglia[i] / c[0])) > math.log(1.3) for c in scelti):
                scelti.append((float(griglia[i]), float(dnus[i]), float(z_pet[i]), float(z_ecc[i])))
                break
    return scelti[:quanti], griglia, punteggio


ZETA = 2 * math.sqrt(2) / math.pi


def modello_fondo(freq, nyq, W, a1, b1, a2, b2, H=0.0, numax=1.0, sigma=1.0, inviluppo=True):
    e2 = eta2(freq, nyq)
    harvey = ZETA * a1 ** 2 / b1 / (1 + (freq / b1) ** 4) + ZETA * a2 ** 2 / b2 / (1 + (freq / b2) ** 4)
    m = W + e2 * harvey
    if inviluppo:
        m = m + e2 * H * np.exp(-0.5 * ((freq - numax) / sigma) ** 2)
    return m


def binna_log(freq, psd, lo, hi, nb=400):
    sel = (freq > lo) & (freq < hi)
    f, p = freq[sel], psd[sel]
    bordi = np.exp(np.linspace(np.log(f[0]), np.log(f[-1]), nb + 1))
    idx = np.clip(np.digitize(f, bordi) - 1, 0, nb - 1)
    cont = np.bincount(idx, minlength=nb)
    fb = np.bincount(idx, weights=f, minlength=nb)
    pb = np.bincount(idx, weights=p, minlength=nb)
    ok = cont > 0
    return fb[ok] / cont[ok], pb[ok] / cont[ok], cont[ok]


def _adatta_binnato(fb, pb, cont, nyq, numax0, W0, sovr=1, q_iniziale=None):
    from scipy.optimize import least_squares
    a0 = 3382 * numax0 ** -0.609
    b10, b20 = 0.317 * numax0 ** 0.970, 0.948 * numax0 ** 0.992
    s0 = fwhm_inviluppo(numax0) / 2.355
    fondo0 = modello_fondo(np.array([numax0]), nyq, W0, a0, b10, a0, b20, inviluppo=False)[0]
    vicino = np.abs(fb - numax0) < s0
    H0 = max(float(np.mean(pb[vicino]) - fondo0) if np.any(vicino) else fondo0, 0.05 * fondo0)
    peso = np.sqrt(np.maximum(cont / max(sovr, 1), 1.0))

    def residui(q):
        W, a1, b1, a2, b2, H, nm, sg = np.exp(q)
        m = modello_fondo(fb, nyq, W, a1, b1, a2, b2, H, nm, sg)
        return (np.log(pb) - np.log(m)) * peso

    bassi = np.log([1e-9, a0 / 30, b10 / 4, a0 / 30, b20 / 4, H0 / 300, numax0 / 1.5, s0 / 1.7])
    alti = np.log([W0 * 100 + 1, a0 * 30, b10 * 4, a0 * 30, b20 * 4, H0 * 300, numax0 * 1.5, s0 * 1.7])
    q0 = q_iniziale if q_iniziale is not None else np.log([max(W0, 1e-9), a0, b10, a0, b20, H0, numax0, s0])
    q0 = np.clip(q0, bassi + 1e-6, alti - 1e-6)
    r = least_squares(residui, q0, bounds=(bassi, alti), max_nfev=3000)
    return r


def adatta_fondo(freq, psd, nyq, numax0, sovr=1):
    """Adatta fondo (2 Harvey + rumore bianco) + inviluppo gaussiano sullo spettro binnato in log."""
    lo = max(freq[0] * 3, numax0 / 12, 2.0)
    hi = nyq * 0.995
    fb, pb, cont = binna_log(freq, psd, lo, hi)
    alto = fb > 0.85 * fb[-1]
    W0 = float(np.median(pb[alto])) if np.any(alto) else float(np.min(pb))
    r = _adatta_binnato(fb, pb, cont, nyq, numax0, W0, sovr)
    W, a1, b1, a2, b2, H, nm, sg = np.exp(r.x)
    if b1 > b2:
        a1, b1, a2, b2 = a2, b2, a1, b1
    return {"W": W, "a1": a1, "b1": b1, "a2": a2, "b2": b2, "H": H, "numax": nm, "sigma": sg,
            "ok": bool(r.success), "binnato": (fb, pb), "cont": cont, "f_min": lo, "q": r.x, "W0": W0,
            "costo": float(r.cost)}


def _gauss(x, A, mu, s, c):
    return c + A * np.exp(-0.5 * ((x - mu) / s) ** 2)


def _prova_candidato(freq, psd, nyq, sovr, numax0):
    fondo = adatta_fondo(freq, psd, nyq, numax0, sovr)
    bg = modello_fondo(freq, nyq, fondo["W"], fondo["a1"], fondo["b1"], fondo["a2"], fondo["b2"], inviluppo=False)
    snr = psd / bg
    nm = fondo["numax"]
    d0 = dnu_atteso(nm)
    r = pettine(freq, snr, nm, d0, sovr, f_min=fondo["f_min"], l1f=0.7, l2f=1.4)
    if r is not None:
        # seconda passata centrata sul Δν trovato
        r2 = pettine(freq, snr, nm, r[0], sovr, f_min=fondo["f_min"], l1f=0.85, l2f=1.15)
        r = r2 or r
    return fondo, bg, snr, r


def nitidezza_piegatura(fs, snr, nm, d, quanti=4, nbin=60):
    """Spettro «piegato» modulo d attorno a νmax: massimo/media del profilo (più alto = creste allineate)."""
    sel = (fs > nm - quanti * d) & (fs < nm + quanti * d)
    if np.sum(sel) < 3 * nbin:
        return float("nan")
    i = ((fs[sel] % d) / d * nbin).astype(int) % nbin
    cont = np.bincount(i, minlength=nbin)
    prof = np.bincount(i, weights=snr[sel], minlength=nbin) / np.maximum(cont, 1)
    return float(prof.max() / prof.mean())


def dnu_piegatura(fs, snr, nm, d0, ampiezza=0.08, prove=321):
    """Rifinisce Δν cercando, entro ±8 % del valore dell'autocorrelazione, il passo che allinea meglio
    le creste dei modi nel diagramma échelle (utile con i modi misti delle subgiganti)."""
    from scipy.ndimage import gaussian_filter1d
    df = fs[1] - fs[0]
    sl = gaussian_filter1d(snr, max(1.0, 0.01 * d0 / df))
    passi = np.linspace((1 - ampiezza) * d0, (1 + ampiezza) * d0, prove)
    v = np.array([nitidezza_piegatura(fs, sl, nm, d) for d in passi])
    if not np.any(np.isfinite(v)):
        return None, None, None
    j = int(np.nanargmax(v))
    return float(passi[j]), float(v[j]), float(nitidezza_piegatura(fs, sl, nm, d0))


def _raggruppa(freq, psd, b):
    """Media di b punti consecutivi dello spettro (spettro di lavoro più leggero)."""
    if b <= 1:
        return freq, psd
    n = (freq.size // b) * b
    return freq[:n].reshape(-1, b).mean(axis=1), psd[:n].reshape(-1, b).mean(axis=1)


def _valuta_candidato(freq, psd, nyq, sovr, c):
    fondo, bg, snr, r = _prova_candidato(freq, psd, nyq, sovr, c[0])
    if r is None:
        return None
    nm_c, sg_c = fondo["numax"], fondo["sigma"]
    i1, i2 = np.searchsorted(freq, [nm_c - sg_c, nm_c + sg_c])
    z_e = (float(np.mean(snr[i1:i2])) - 1.0) * math.sqrt(max(i2 - i1, 1) / sovr) if i2 > i1 + 5 else 0.0
    valore = r[2] + 0.5 * min(max(z_e, 0.0), 12.0)
    return (c, fondo, bg, snr, r, valore, z_e)


def analizza_spettro(freq_tutto, psd_tutto, nyq, ris, sovr=2, n_mc=40, seme=1, log=print, numax_guida=None,
                     intervallo=None):
    """Tutta l'analisi sullo spettro. Restituisce dict dei risultati (anche se non trova oscillazioni).
    Per velocità lavora su spettri raggruppati: al massimo ~150 000 punti per la ricerca dei candidati,
    ~250 punti per Δν per l'analisi di ciascun candidato (le statistiche ne tengono conto)."""
    ris_d = {"rilevato": False, "nyquist": nyq, "risoluzione": ris}
    df0 = freq_tutto[1] - freq_tutto[0]
    if numax_guida:
        candidati, griglia, punteggi = [(float(numax_guida), dnu_atteso(numax_guida), float("nan"))], None, None
    else:
        b1 = max(1, int(math.ceil(freq_tutto.size / 150000)))
        f1, p1 = _raggruppa(freq_tutto, psd_tutto, b1)
        candidati, griglia, punteggi = cerca_candidati(f1, p1, nyq, ris, sovr / b1, intervallo=intervallo)
    if not candidati:
        ris_d["motivo"] = "spettro troppo corto o troppo rumoroso per cercare le oscillazioni"
        return ris_d
    ris_d.update({"griglia_ricerca": griglia, "punteggi_ricerca": punteggi, "candidati": candidati})
    migliore, lavoro = None, None
    for c in candidati:
        b2 = max(1, int(dnu_atteso(c[0]) / (250 * df0)))
        f2, p2 = _raggruppa(freq_tutto, psd_tutto, b2)
        try:
            v = _valuta_candidato(f2, p2, nyq, sovr / b2, c)
        except Exception:
            v = None
        if v is not None and (migliore is None or v[5] > migliore[5]):
            migliore, lavoro = v, (f2, p2, sovr / b2)
    if migliore is None:
        ris_d["motivo"] = "non riesco a misurare Δν"
        return ris_d
    freq, psd, sovr = lavoro
    c, fondo, bg, snr, (d, picco, z, lag, acf), valore, z_e = migliore
    nm, sg = fondo["numax"], fondo["sigma"]
    ris_d.update({"numax_candidato": c[0], "dnu_candidato": c[1], "z_pettine": c[2]})
    ris_d["fondo"] = {k: v for k, v in fondo.items() if k not in ("binnato", "cont", "q")}
    ris_d["binnato"] = fondo["binnato"]
    # rifinitura di Δν con la piegatura dello spettro (se allinea le creste chiaramente meglio)
    d_acf = d
    d_pieg, v_pieg, v_acf = dnu_piegatura(freq, snr, nm, d)
    if d_pieg and v_acf and v_pieg > 1.4 * v_acf:
        d = d_pieg
    snr_medio = float(np.mean(snr[(freq > nm - sg) & (freq < nm + sg)])) if sg > 0 else float("nan")
    rapporto = d / dnu_atteso(nm)
    forma_ok = (snr_medio > 1.1) and 0.6 < rapporto < 1.5
    rilevato = forma_ok and ((z >= 5.5) or (z >= 4.5 and z_e >= 10.0))
    probabile = forma_ok and not rilevato and z >= 2.0 and z_e >= 12.0
    giudizio = "sicuro" if rilevato else ("probabile" if probabile else "dubbio")

    # incertezze (Monte Carlo): νmax rifacendo l'adattamento su spettri binnati simulati
    # (media di χ² con 2 gradi di libertà = distribuzione Gamma), Δν rifacendo l'autocorrelazione
    rng = np.random.default_rng(seme)
    fb, pb = fondo["binnato"]
    cont = fondo["cont"]
    modello_b = modello_fondo(fb, nyq, fondo["W"], fondo["a1"], fondo["b1"], fondo["a2"], fondo["b2"],
                              fondo["H"], nm, sg)
    forma = np.maximum(cont / max(sovr, 1), 1.0)
    nms = []
    for _ in range(max(n_mc // 2, 8)):
        sim = modello_b * rng.gamma(forma, 1.0 / forma)
        try:
            rr = _adatta_binnato(fb, sim, cont, nyq, nm, fondo["W0"], sovr, q_iniziale=fondo["q"])
            nms.append(float(np.exp(rr.x[6])))
        except Exception:
            pass
    from scipy.ndimage import gaussian_filter1d
    df = freq[1] - freq[0]
    mezza = max(fwhm_inviluppo(nm), 3.0 * d)
    sel = (freq > max(nm - mezza - d, fondo["f_min"])) & (freq < nm + mezza + d)
    fs, bgs = freq[sel], bg[sel]
    limite = gaussian_filter1d(psd[sel], max(1.0, d / 25 / df))
    ds = []
    for _ in range(n_mc):
        k = max(1.0, 1.0 / sovr)
        sim = limite * rng.gamma(k, 1.0 / k, fs.size)
        r = pettine(fs, sim / bgs, nm, d, sovr, l1f=0.85, l2f=1.15)
        if r is not None:
            ds.append(r[0])
    e_nm = float(np.std(nms)) if len(nms) > 4 else 0.05 * nm
    e_d = float(np.std(ds)) if len(ds) > 5 else 0.02 * d
    e_nm = max(e_nm, 0.01 * nm, df)
    e_d = max(e_d, 0.003 * d, df / 2, abs(d - d_acf) / 2)
    # ampiezza dei modi radiali al νmax (Kjeldsen et al. 2008, c ≈ 3,04)
    amp = math.sqrt(max(fondo["H"], 0) * d / 3.04)

    bg_tutto = modello_fondo(freq_tutto, nyq, fondo["W"], fondo["a1"], fondo["b1"], fondo["a2"], fondo["b2"],
                             inviluppo=False)
    ris_d.update({"rilevato": bool(rilevato), "giudizio": giudizio, "numax": nm, "e_numax": e_nm, "dnu": d, "e_dnu": e_d,
                  "sigma_inviluppo": sg, "dnu_acf": d_acf, "z_dnu": z, "z_eccesso": z_e, "snr_medio": snr_medio, "ampiezza_ppm": amp,
                  "acf_lag": lag, "acf": acf, "bg": bg_tutto, "snr": snr, "f_snr": freq, "n_mc": len(ds)})
    if probabile:
        ris_d["motivo"] = (f"gobba delle oscillazioni chiara (eccesso z = {z_e:.1f}) ma pettine debole (z = {z:.1f}): "
                           f"νmax affidabile, Δν da controllare")
    elif not rilevato:
        ris_d["motivo"] = (f"segnale debole o dubbio (pettine z = {z:.1f}, eccesso z = {z_e:.1f}, rapporto medio al νmax "
                           f"{snr_medio:.2f}, Δν/Δν atteso = {rapporto:.2f})")
    return ris_d


# =============================================================================
# RELAZIONI DI SCALA
# =============================================================================
def relazioni_scala(numax, e_numax, dnu, e_dnu, teff, e_teff):
    a, ea = numax / NUMAX_SOLE, e_numax / numax
    b, eb = dnu / DNU_SOLE, e_dnu / dnu
    c, ec = teff / TEFF_SOLE, e_teff / teff
    R = a * b ** -2 * c ** 0.5
    M = a ** 3 * b ** -4 * c ** 1.5
    eR = R * math.sqrt(ea ** 2 + (2 * eb) ** 2 + (0.5 * ec) ** 2)
    eM = M * math.sqrt((3 * ea) ** 2 + (4 * eb) ** 2 + (1.5 * ec) ** 2)
    logg = LOGG_SOLE + math.log10(a) + 0.5 * math.log10(c)
    elogg = math.sqrt(ea ** 2 + (0.5 * ec) ** 2) / math.log(10)
    rho = b ** 2
    erho = rho * 2 * eb
    L = R ** 2 * c ** 4
    eL = L * math.sqrt((2 * eR / R) ** 2 + (4 * ec) ** 2)
    return {"R": R, "e_R": eR, "M": M, "e_M": eM, "logg": logg, "e_logg": elogg,
            "rho": rho, "e_rho": erho, "L": L, "e_L": eL}


# =============================================================================
# TEMPERATURA E METALLICITÀ (Gaia DR3, TIC, cataloghi)
# =============================================================================
def gaia_dr3(info, log=print, ferma=None):
    """Riga Gaia DR3 + parametri astrofisici. Usa l'ID DR3 dei cataloghi, poi la DR2 del TIC, poi il cono."""
    sel = ("g.source_id, g.ra, g.dec, g.parallax, g.parallax_error, g.phot_g_mean_mag, g.bp_rp, "
           "a.teff_gspphot, a.logg_gspphot, a.mh_gspphot, a.teff_gspspec, a.mh_gspspec, a.logg_gspspec")
    da = "gaiadr3.gaia_source AS g LEFT OUTER JOIN gaiadr3.astrophysical_parameters AS a ON g.source_id = a.source_id"
    domande = []
    if info.get("gaia_dr3"):
        domande.append(f"SELECT TOP 1 {sel} FROM {da} WHERE g.source_id = {int(info['gaia_dr3'])}")
    rt = info.get("riga_tic") or {}
    if rt.get("GAIA"):
        try:
            dr2 = int(float(rt["GAIA"]))
            domande.append(f"SELECT TOP 1 {sel} FROM gaiadr3.dr2_neighbourhood AS n JOIN gaiadr3.gaia_source AS g "
                           f"ON g.source_id = n.dr3_source_id LEFT OUTER JOIN gaiadr3.astrophysical_parameters AS a "
                           f"ON g.source_id = a.source_id WHERE n.dr2_source_id = {dr2} ORDER BY n.angular_distance")
        except (TypeError, ValueError):
            pass
    if info.get("ra") is not None:
        domande.append(f"SELECT TOP 1 {sel} FROM {da} WHERE 1=CONTAINS(POINT('ICRS',g.ra,g.dec),"
                       f"CIRCLE('ICRS',{info['ra']},{info['dec']},0.004)) ORDER BY g.phot_g_mean_mag")
    for q in domande:
        try:
            b = richiesta(GAIA_ESA, {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": q},
                          tempo=120, ferma=ferma)
            righe = list(csv.DictReader(io.StringIO(b.decode("utf-8", "replace"))))
            if righe:
                return {k: (numero(v) if k != "source_id" else v) for k, v in righe[0].items()}
        except Interrotto:
            raise
        except urllib.error.HTTPError as e:
            log(f"   Gaia: HTTP {e.code} (provo un'altra interrogazione)")
        except Exception as e:
            log(f"   Gaia: {type(e).__name__}: {e}")
            break      # se l'archivio non risponde è inutile riprovare con le altre domande
    return None


def scegli_teff_feh(info, cataloghi, gaia, intest, teff_manuale=None, feh_manuale=None):
    """Sceglie Teff e [Fe/H] con la loro provenienza (spettroscopia prima, fotometria dopo)."""
    rt = info.get("riga_tic") or {}
    candidati_t = []
    if teff_manuale:
        candidati_t.append((teff_manuale, 80.0, "manual"))
    for nome in ("APOKASC-3", "APOKASC-2"):
        v = cataloghi.get(nome, {}).get("teff")
        if v:
            candidati_t.append((v, 60.0, f"{nome} (APOGEE spectroscopy)"))
    v = cataloghi.get("Yu+2018", {}).get("teff")
    if v:
        candidati_t.append((v, 100.0, "Yu+2018"))
    if numero(rt.get("Teff")):
        e = numero(rt.get("e_Teff")) or 120.0
        candidati_t.append((numero(rt["Teff"]), max(e, 80.0), "TIC v8"))
    # GSP-Spec non calibrato: per le giganti la Teff può uscire bassa di ~100-200 K
    if gaia and gaia.get("teff_gspspec"):
        candidati_t.append((gaia["teff_gspspec"], 150.0, "Gaia DR3 GSP-Spec"))
    if gaia and gaia.get("teff_gspphot"):
        candidati_t.append((gaia["teff_gspphot"], 150.0, "Gaia DR3 GSP-Phot"))
    if intest.get("teff"):
        candidati_t.append((intest["teff"], 150.0, "FITS header (KIC/TIC)"))
    teff = candidati_t[0] if candidati_t else (None, None, "none")

    candidati_f = []
    if feh_manuale is not None:
        candidati_f.append((feh_manuale, 0.1, "manual"))
    for nome in ("APOKASC-3", "APOKASC-2"):
        v = cataloghi.get(nome, {}).get("feh")
        if v is not None:
            candidati_f.append((v, 0.05, f"{nome} (APOGEE spectroscopy)"))
    v = cataloghi.get("Yu+2018", {}).get("feh")
    if v is not None:
        candidati_f.append((v, 0.3, "Yu+2018"))
    if numero(rt.get("MH")) is not None:
        candidati_f.append((numero(rt["MH"]), 0.2, "TIC v8"))
    if gaia and gaia.get("mh_gspspec") is not None:
        candidati_f.append((gaia["mh_gspspec"], 0.2, "Gaia DR3 GSP-Spec"))
    if gaia and gaia.get("mh_gspphot") is not None:
        candidati_f.append((gaia["mh_gspphot"], 0.3, "Gaia DR3 GSP-Phot"))
    if intest.get("feh") is not None:
        candidati_f.append((intest["feh"], 0.3, "FITS header"))
    feh = candidati_f[0] if candidati_f else (0.0, 0.3, "assumed solar")
    return teff, feh


# =============================================================================
# MIST: TRACCE EVOLUTIVE ED ETÀ
# =============================================================================
def etichetta_feh(feh):
    v = min(MIST_FEH, key=lambda g: abs(g - feh))
    return ("p" if v >= 0 else "m") + f"{abs(v):.2f}", v


def leggi_eep(testo):
    """Legge un file MIST *.track.eep. Restituisce dict colonna -> array."""
    intest = None
    righe = []
    for l in testo.splitlines():
        if l.startswith("#"):
            if "star_age" in l and "star_mass" in l:
                intest = l.lstrip("#").split()
            continue
        if l.strip():
            righe.append(l)
    if intest is None or not righe:
        raise RuntimeError("file EEP senza intestazione")
    dati = np.loadtxt(io.StringIO("\n".join(righe)))
    if dati.ndim == 1:
        dati = dati[None, :]
    return {n: dati[:, i] for i, n in enumerate(intest) if i < dati.shape[1]}


def griglia_mist(feh, avanzamento=None, log=print, ferma=None, m_min=0.6, m_max=4.0):
    """Restituisce la griglia MIST (tracce EEP) per la metallicità più vicina, scaricandola la prima volta."""
    lab, v = etichetta_feh(feh)
    d = CACHE / "mist"
    d.mkdir(parents=True, exist_ok=True)
    npz = d / f"mist_v1.2_feh_{lab}.npz"
    if npz.exists():
        g = dict(np.load(npz))
        g["feh_griglia"] = v
        return g
    url = MIST_URL.format(feh=lab)
    tar = d / Path(url).name
    if not (tar.exists() and tar.stat().st_size > 0):
        log(f"   MIST: scarico le tracce [Fe/H] = {v:+.2f} (circa 110 MB, solo la prima volta)…")
        parz = tar.with_suffix(".part")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r, open(parz, "wb") as out:
            tot = int(r.headers.get("Content-Length") or 0)
            fatti = 0
            while True:
                if ferma is not None and ferma.is_set():
                    raise Interrotto()
                b = r.read(1 << 20)
                if not b:
                    break
                out.write(b)
                fatti += len(b)
                if avanzamento:
                    avanzamento(fatti, tot)
        parz.replace(tar)
    log("   MIST: estraggo le tracce da 0,6 a 4 masse solari…")
    pezzi = {k: [] for k in ("m_ini", "age", "mass", "logT", "logg", "logR", "phase")}
    with tarfile.open(tar, mode="r|xz") as tf:
        for membro in tf:
            if ferma is not None and ferma.is_set():
                raise Interrotto()
            m = re.search(r"(\d{5})M\.track\.eep$", membro.name)
            if not m:
                continue
            mi = int(m.group(1)) / 100.0
            if not (m_min <= mi <= m_max):
                continue
            testo = tf.extractfile(membro).read().decode("ascii", "replace")
            c = leggi_eep(testo)
            n = c["star_age"].size
            pezzi["m_ini"].append(np.full(n, mi))
            pezzi["age"].append(c["star_age"])
            pezzi["mass"].append(c["star_mass"])
            pezzi["logT"].append(c["log_Teff"])
            pezzi["logg"].append(c["log_g"])
            pezzi["logR"].append(c["log_R"])
            pezzi["phase"].append(c["phase"])
    if not pezzi["m_ini"]:
        raise RuntimeError("nessuna traccia trovata nel pacchetto MIST")
    g = {k: np.concatenate(v).astype(np.float64 if k == "age" else np.float32) for k, v in pezzi.items()}
    np.savez_compressed(npz, **g)
    tar.unlink(missing_ok=True)
    g["feh_griglia"] = v
    return g


def griglia_fine(g, passi=8):
    """Aggiunge tracce intermedie tra masse vicine interpolando riga per riga: nei file EEP di MIST la
    stessa riga è la stessa fase evolutiva (EEP) per tutte le masse. Età interpolata in logaritmo."""
    if "_fine" in g:
        return g["_fine"]
    chiavi = ("age", "mass", "logT", "logg", "logR", "phase")
    m_ini = g["m_ini"].astype(float)
    masse = np.unique(m_ini)
    tracce = []
    for m in masse:
        j = np.nonzero(m_ini == m)[0]
        tracce.append({k: g[k][j].astype(float) for k in chiavi})
    pezzi = {k: [] for k in ("m_ini",) + chiavi}

    def aggiungi(tr, m):
        n = tr["age"].size
        pezzi["m_ini"].append(np.full(n, m))
        for k in chiavi:
            pezzi[k].append(tr[k])

    for i, m in enumerate(masse):
        A = tracce[i]
        aggiungi(A, m)
        if i + 1 < masse.size:
            B, m2 = tracce[i + 1], masse[i + 1]
            n = min(A["age"].size, B["age"].size)
            la, lb = np.log10(np.maximum(A["age"][:n], 1.0)), np.log10(np.maximum(B["age"][:n], 1.0))
            for f in np.arange(1, passi) / passi:
                C = {k: (1 - f) * A[k][:n] + f * B[k][:n] for k in ("mass", "logT", "logg", "logR")}
                C["age"] = 10 ** ((1 - f) * la + f * lb)
                C["phase"] = A["phase"][:n] if f < 0.5 else B["phase"][:n]
                aggiungi(C, m + (m2 - m) * f)
    fine = {k: np.concatenate(v) for k, v in pezzi.items()}
    fine["feh_griglia"] = g.get("feh_griglia")
    g["_fine"] = fine
    return fine


def eta_da_griglia(g, numax, e_numax, dnu, e_dnu, teff, e_teff, stato_noto=None):
    """Modellazione su griglia: confronta νmax, Δν e Teff osservati con quelli delle tracce
    (relazioni di scala applicate ai modelli). Pesi = verosimiglianza × tempo passato in ogni punto
    × IMF di Salpeter × passo in massa. Restituisce età, massa, raggio (mediana e 16-84 %) e
    probabilità delle fasi (sequenza principale, ramo delle giganti rosse, combustione dell'elio)."""
    g = griglia_fine(g)
    m_ini = g["m_ini"].astype(float)
    age = g["age"].astype(float)
    mass = g["mass"].astype(float)
    T = 10 ** g["logT"].astype(float)
    R = 10 ** g["logR"].astype(float)
    logg = g["logg"].astype(float)
    fase = np.round(g["phase"]).astype(int)
    numax_m = NUMAX_SOLE * 10 ** (logg - LOGG_SOLE) * (T / TEFF_SOLE) ** -0.5
    dnu_m = DNU_SOLE * np.sqrt(mass / R ** 3)
    # incertezze gonfiate con una parte sistematica delle relazioni di scala
    s_nm = math.hypot(e_numax, 0.02 * numax)
    s_d = math.hypot(e_dnu, 0.01 * dnu)
    s_t = max(e_teff, 80.0)
    chi2 = ((numax_m - numax) / s_nm) ** 2 + ((dnu_m - dnu) / s_d) ** 2 + ((T - teff) / s_t) ** 2
    ammessi = np.isin(fase, (0, 2, 3)) & (age < 14e9) & (age > 1e7)
    if stato_noto == "RGB":
        ammessi &= fase != 3
    elif stato_noto == "RC":
        ammessi &= fase == 3
    # tempo passato in ogni punto (prior uniforme in età), traccia per traccia; IMF × passo in massa
    dtempo = np.zeros_like(age)
    masse = np.unique(m_ini)
    for mi in masse:
        j = np.nonzero(m_ini == mi)[0]
        if j.size > 1:
            dtempo[j] = np.maximum(np.gradient(age[j]), 0)
    passo = np.gradient(masse) if masse.size > 1 else np.ones(1)
    passo_punto = np.interp(m_ini, masse, passo)
    peso = np.zeros_like(age)
    if np.any(ammessi):
        c0 = np.min(chi2[ammessi])
        peso[ammessi] = np.exp(-0.5 * (chi2[ammessi] - c0))
    peso *= dtempo * m_ini ** -2.35 * passo_punto
    tot = peso.sum()
    if not np.isfinite(tot) or tot <= 0:
        return None
    peso /= tot

    def quantili(v):
        o = np.argsort(v)
        c = np.cumsum(peso[o])
        return [float(np.interp(q, c, v[o])) for q in (0.16, 0.5, 0.84)]

    a16, a50, a84 = quantili(age / 1e9)
    m16, m50, m84 = quantili(mass)
    r16, r50, r84 = quantili(R)
    prob = {"MS": float(peso[fase == 0].sum()), "RGB": float(peso[fase == 2].sum()),
            "CHeB": float(peso[fase == 3].sum())}
    return {"eta": a50, "eta_meno": a50 - a16, "eta_piu": a84 - a50,
            "massa": m50, "e_massa": 0.5 * (m84 - m16), "raggio": r50, "e_raggio": 0.5 * (r84 - r16),
            "prob_fasi": prob, "chi2_min": float(np.min(chi2[ammessi])) if np.any(ammessi) else None,
            "feh_griglia": g.get("feh_griglia")}


# =============================================================================
# SUONO
# =============================================================================
def nota_per_numax(numax):
    """Nota (Hz) a cui si porta νmax: abbastanza acuta per le casse di un portatile (circa 600 Hz per una
    gigante con νmax 30 µHz, più acuta per le stelle tipo Sole), tra 200 e 3000 Hz."""
    return float(min(max(600.0 * (numax / 30.0) ** 0.3, 200.0), 3000.0))


def _scrivi_wav(y, percorso, campionamento=44100, sfuma=0.3):
    picco = np.percentile(np.abs(y), 99.9) if y.size else 1.0
    y = np.clip(y / (picco or 1.0) * 0.8, -1, 1)
    sf = int(sfuma * campionamento)
    if y.size > 2 * sf:
        y[:sf] *= np.linspace(0, 1, sf)
        y[-sf:] *= np.linspace(1, 0, sf)
    with wave.open(str(percorso), "wb") as wv:
        wv.setnchannels(1)
        wv.setsampwidth(2)
        wv.setframerate(campionamento)
        wv.writeframes((y * 32767).astype("<i2").tobytes())
    return y.size / campionamento


def suono_modi(an, percorso, durata=14.0, campionamento=44100, seme=7, max_modi=30):
    """«Accordo dei modi»: i picchi dei modi di oscillazione trovati nello spettro diventano note pulite,
    alle stesse frequenze relative (stesso fattore k del suono vero) e con la loro intensità; ogni nota
    sale e scende lentamente, come i modi della stella che vengono eccitati e smorzati di continuo."""
    from scipy.ndimage import gaussian_filter1d
    from scipy.signal import find_peaks
    nm, d = an["numax"], an["dnu"]
    fs, snr = an["f_snr"], an["snr"]
    df = fs[1] - fs[0]
    sel = (fs > nm - 4.5 * d) & (fs < nm + 4.5 * d)
    f, s_ = fs[sel], gaussian_filter1d(snr[sel], max(1.0, 0.01 * d / df))
    picchi, prop = find_peaks(s_, height=max(4.0, np.percentile(s_, 90)), distance=max(1, int(0.06 * d / df)))
    if picchi.size == 0:
        raise RuntimeError("nessun modo abbastanza forte per l'accordo")
    ordine = np.argsort(prop["peak_heights"])[::-1][:max_modi]
    freq_m, alt = f[picchi[ordine]], prop["peak_heights"][ordine]
    nota = nota_per_numax(nm)
    k = nota / nm                                    # stesso rapporto: νmax → nota
    rng = np.random.default_rng(seme)
    n = int(durata * campionamento)
    tt = np.arange(n) / campionamento
    y = np.zeros(n)
    tg = np.linspace(0, durata, int(durata * 50) + 1)        # inviluppo calcolato a 50 punti al secondo
    for fm, h in zip(freq_m, alt):
        # inviluppo lento e casuale (eccitazione e smorzamento), tempo scala ~1,5 s
        nodi = rng.rayleigh(1.0, int(durata / 0.75) + 3)
        inv = gaussian_filter1d(np.interp(tg, np.linspace(0, durata, nodi.size), nodi), 0.3 * 50)
        inv = np.interp(tt, tg, inv)
        y += np.sqrt(max(h - 1.0, 0.1)) * inv * np.sin(2 * np.pi * fm * k * tt + rng.uniform(0, 2 * np.pi))
    durata_vera = _scrivi_wav(y, percorso, campionamento, sfuma=0.8)
    return {"n_modi": int(freq_m.size), "nota_hz": nota, "durata_s": durata_vera,
            "da_hz": float(freq_m.min() * k), "a_hz": float(freq_m.max() * k)}


def suono(t, x, dt, numax, sigma_env, percorso, durata_max=30.0, campionamento=44100):
    """Le oscillazioni portate nell'udibile: si filtra la curva di luce attorno a νmax e la si
    accelera di un fattore k, scelto in modo che νmax diventi una nota tra ~90 Hz e ~2 kHz.
    Restituisce dict con fattore k, frequenza della nota per νmax e durata."""
    from scipy.fft import rfft, irfft, next_fast_len
    f_audio = nota_per_numax(numax)
    k = f_audio / (numax * 1e-6)
    dt_s = dt * 86400.0
    celle_max = int(durata_max * k / dt_s)
    idx = np.round((t - t[0]) / dt).astype(np.int64)
    n = int(idx[-1]) + 1
    v = np.zeros(n)
    w = np.zeros(n)
    np.add.at(v, idx, x)
    np.add.at(w, idx, 1.0)
    v = np.divide(v, w, out=np.zeros(n), where=w > 0)
    if n > celle_max:
        # prende il tratto con più dati
        cs = np.concatenate([[0], np.cumsum(w > 0)])
        cop = cs[celle_max:] - cs[:-celle_max]
        i0 = int(np.argmax(cop))
        v = v[i0:i0 + celle_max]
        n = v.size
    nf = next_fast_len(n)
    S = rfft(v, nf)
    fr = np.arange(S.size) / (nf * dt_s) * 1e6      # µHz
    larg = max(3.0 * sigma_env, 2 * dnu_atteso(numax))
    filtro = np.exp(-0.5 * ((fr - numax) / larg) ** 4)
    sr_dati = k / dt_s                                # campioni al secondo dopo l'accelerazione
    up = max(1, int(math.ceil(campionamento / sr_dati)))
    y = irfft(S * filtro, nf * up)[: n * up] * up
    durata = n * dt_s / k
    tempi_audio = np.arange(int(durata * campionamento)) / campionamento
    y = np.interp(tempi_audio, np.arange(y.size) / (sr_dati * up), y)
    if y.size < campionamento * 4 and y.size > 0:
        ripeti = int(math.ceil(campionamento * 4 / y.size))
        y = np.tile(y, ripeti)
    _scrivi_wav(y, percorso, campionamento)
    return {"k": k, "nota_hz": f_audio, "durata_s": y.size / campionamento}


# =============================================================================
# GRAFICI E SCHEDA (in inglese)
# =============================================================================
PANNELLI = ("lc", "ps", "zm", "ec", "acf")


def disegna_pannelli(fig, ris, compatto=False, solo=None):
    """Disegna curva di luce, spettro, zoom con il pettine, échelle e autocorrelazione.
    solo = uno di PANNELLI: disegna soltanto quel grafico, grande (gli altri vanno su una figura scartata)."""
    fig.clear()
    a_ingr = None
    if solo in PANNELLI:
        from matplotlib.figure import Figure as _Figura
        scarto = _Figura()
        assi = {k: (fig.add_subplot(111) if k == solo else scarto.add_subplot(111)) for k in PANNELLI}
        a_lc, a_ps, a_zm, a_ec, a_acf = (assi[k] for k in PANNELLI)
    elif compatto:
        assi = fig.subplots(2, 2)
        a_lc, a_ps, a_zm, a_ec = assi[0, 0], assi[0, 1], assi[1, 0], assi[1, 1]
        a_acf = None
    else:
        gs = fig.add_gridspec(3, 3, height_ratios=[1, 1.15, 1.15], hspace=0.55, wspace=0.32,
                              left=0.075, right=0.98, top=0.865, bottom=0.095)
        a_lc = fig.add_subplot(gs[0, :2])
        a_ps = fig.add_subplot(gs[1, :2])
        a_zm = fig.add_subplot(gs[2, :2])
        a_ec = fig.add_subplot(gs[1:, 2])
        a_acf = fig.add_subplot(gs[0, 2])
    t, x = ris["t"], ris["x"]
    passo = max(1, t.size // 40000)
    curva_di_luce_interattiva(a_lc, t, x, grande=(solo == "lc"))
    a_lc.set_xlabel("Time (days)")
    a_lc.set_ylabel("Flux (ppm)")
    lim = np.percentile(np.abs(x), 99.5)
    a_lc.set_ylim(-1.2 * lim, 1.2 * lim)
    a_lc.set_title(f"Light curve – {ris.get('missione', '')} {ris.get('cadenza_testo', '')}", fontsize="medium")
    if a_ingr is not None:
        disegna_tratto(a_ingr, t, x, ris.get("analisi", {}).get("numax"), lim)

    f, p = ris["freq"], ris["psd"]
    passo = max(1, f.size // 60000)
    a_ps.loglog(f[::passo], p[::passo], color="#b0b0b0", lw=0.4, rasterized=True)
    an = ris["analisi"]
    if "binnato" in an:
        fb, pb = an["binnato"]
        a_ps.loglog(fb, pb, color="#222222", lw=1.0, label="binned")
    if "fondo" in an:
        fo = an["fondo"]
        bg = modello_fondo(f, ris["nyquist"], fo["W"], fo["a1"], fo["b1"], fo["a2"], fo["b2"], inviluppo=False)
        tot = modello_fondo(f, ris["nyquist"], fo["W"], fo["a1"], fo["b1"], fo["a2"], fo["b2"],
                            fo["H"], an.get("numax", fo["numax"]), an.get("sigma_inviluppo", fo["sigma"]))
        a_ps.loglog(f[::passo], bg[::passo], color="#1f77b4", lw=1.2, label="background")
        a_ps.loglog(f[::passo], tot[::passo], color="#d62728", lw=1.0, ls="--", label="background + envelope")
    if an.get("numax"):
        a_ps.axvline(an["numax"], color="#e8a400", lw=1.2, label=f"νmax = {an['numax']:.2f} µHz")
    a_ps.set_xlim(max(f[0], 0.5), ris["nyquist"])
    a_ps.set_xlabel("Frequency (µHz)")
    a_ps.set_ylabel("PSD (ppm²/µHz)")
    if a_ps.get_legend_handles_labels()[0]:
        a_ps.legend(fontsize="x-small", loc="lower left")
    a_ps.set_title("Power density spectrum", fontsize="medium")

    if an.get("numax") and an.get("dnu"):
        nm, d = an["numax"], an["dnu"]
        f = an.get("f_snr", f)
        sel = (f > nm - 5 * d) & (f < nm + 5 * d)
        snr = an["snr"][sel]
        fs = f[sel]
        from scipy.ndimage import gaussian_filter1d
        dfreq = f[1] - f[0]
        a_zm.plot(fs, snr, color="#9a9a9a", lw=0.5)
        a_zm.plot(fs, gaussian_filter1d(snr, max(1.0, 0.02 * d / dfreq)), color="#222222", lw=0.9)
        for j in range(-5, 6):
            a_zm.axvline(nm + j * d, color="#e8a400", lw=0.5, alpha=0.6)
        a_zm.set_xlim(nm - 5 * d, nm + 5 * d)
        a_zm.set_xlabel("Frequency (µHz)")
        a_zm.set_ylabel("Signal / background")
        a_zm.set_title(f"Oscillation modes around νmax (orange lines every Δν = {d:.3f} µHz)", fontsize="medium")

        # échelle
        lo, hi = nm - 6 * d, nm + 6 * d
        sel = (f > lo) & (f < hi)
        fs = f[sel]
        sl = gaussian_filter1d(an["snr"][sel], max(1.0, 0.012 * d / dfreq))
        nx = max(40, int(d / dfreq))
        righe = []
        y0 = np.floor(lo / d) * d
        ys = []
        k0 = 0
        while y0 + k0 * d < hi:
            base = y0 + k0 * d
            xs = base + np.linspace(0, d, nx, endpoint=False)
            righe.append(np.interp(xs, fs, sl, left=np.nan, right=np.nan))
            ys.append(base)
            k0 += 1
        img = np.array(righe)
        img2 = np.hstack([img, img])
        a_ec.imshow(img2, aspect="auto", origin="lower", cmap="magma",
                    extent=[0, 2 * d, ys[0], ys[-1] + d], vmin=0, vmax=np.nanpercentile(img, 99.5),
                    interpolation="nearest")
        a_ec.set_facecolor("black")
        a_ec.set_ylim(lo, hi)
        a_ec.axvline(d, color="white", lw=0.6, ls=":")
        a_ec.set_xlabel(f"Frequency mod Δν (µHz), shown twice")
        a_ec.set_ylabel("Frequency (µHz)")
        a_ec.set_title("Échelle diagram", fontsize="medium")
        if a_acf is not None and an.get("acf") is not None:
            lag, acf = an["acf_lag"], an["acf"]
            m = lag < 2.5 * d
            a_acf.plot(lag[m], acf[m], color="#3a6ea5", lw=0.8)
            a_acf.axvline(d, color="#e8a400", lw=1.0)
            a_acf.set_xlabel("Lag (µHz)")
            a_acf.set_ylabel("ACF")
            a_acf.set_title(f"Autocorrelation of the spectrum: Δν = {d:.3f} µHz", fontsize="medium")
    else:
        for a in (a_zm, a_ec) + ((a_acf,) if a_acf is not None else ()):
            a.text(0.5, 0.5, "No clear oscillations detected", ha="center", va="center", transform=a.transAxes)
            a.set_xticks([])
            a.set_yticks([])
    return fig


def curva_di_luce_interattiva(ax, t, x, grande=False, massimo=40000):
    """Puntini azzurri della curva di luce. Nel tratto visibile si disegnano tutti i punti (al massimo
    «massimo», altrimenti uno ogni n); quando si ingrandisce o si sposta la vista il grafico si ridisegna:
    con pochi punti visibili diventano più grandi e sono uniti da una linea sottile (si vede l'oscillazione)."""
    punti, = ax.plot([], [], ".", mew=0, alpha=0.8, color="#2f8fe8", rasterized=True)
    linea, = ax.plot([], [], "-", lw=0.6, color="#2f8fe8", alpha=0.6)

    def aggiorna(asse=None):
        lo, hi = ax.get_xlim()
        i1, i2 = np.searchsorted(t, [lo, hi])
        i1, i2 = max(0, i1 - 1), min(t.size, i2 + 1)
        n = max(i2 - i1, 1)
        passo = max(1, n // massimo)
        ts, xs = t[i1:i2:passo], x[i1:i2:passo]
        visibili = ts.size
        if visibili > 20000:
            dim = 3.2 if grande else 2.2
        elif visibili > 3000:
            dim = 4.0 if grande else 2.8
        else:
            dim = 5.5 if grande else 3.5
        punti.set_data(ts, xs)
        punti.set_markersize(dim)
        if visibili <= 3000:
            linea.set_data(ts, xs)
        else:
            linea.set_data([], [])

    ax.set_xlim(t[0] - 0.01 * (t[-1] - t[0]), t[-1] + 0.01 * (t[-1] - t[0]))
    aggiorna()
    ax.callbacks.connect("xlim_changed", aggiorna)
    return punti


def disegna_tratto(ax, t, x, numax, lim):
    """Tratto continuo della curva di luce lungo circa 40 oscillazioni (40/νmax), con i punti uniti:
    lì si vede la stella che pulsa. Senza νmax: 5 % della durata."""
    dt = float(np.median(np.diff(t))) if t.size > 1 else 0.02
    lungo = (40.0 / (numax * 1e-6) / 86400.0) if numax else 0.05 * (t[-1] - t[0])
    lungo = float(min(max(lungo, 30 * dt), t[-1] - t[0]))
    # inizio del tratto con meno buchi
    buchi = np.diff(t) > 3 * dt
    inizi = np.concatenate([[0], np.nonzero(buchi)[0] + 1])
    fini = np.concatenate([np.nonzero(buchi)[0], [t.size - 1]])
    j = int(np.argmax(t[fini] - t[inizi]))
    t0 = t[inizi[j]] + max(0.0, 0.5 * ((t[fini[j]] - t[inizi[j]]) - lungo))
    sel = (t >= t0) & (t <= t0 + lungo)
    ts, xs = t[sel] - t0, x[sel]
    unita, fattore = ("hours", 24.0) if lungo < 2 else ("days", 1.0)
    ax.plot(ts * fattore, xs, "-", lw=0.8, color="#2b5d9a")
    ax.plot(ts * fattore, xs, ".", ms=3, mew=0, color="#163a66")
    lim_tratto = float(np.percentile(np.abs(xs), 99.5)) if xs.size > 10 else lim
    ax.set_ylim(-1.25 * lim_tratto, 1.25 * lim_tratto)        # scala propria del tratto
    ax.set_xlabel(f"Time from day {t0:.1f} ({unita})")
    ax.set_ylabel("Flux (ppm)")
    periodo = (1.0 / (numax * 1e-6) / 3600.0) if numax else None
    ax.set_title("Zoom: the star's heartbeat" + (f" (one oscillation at νmax ≈ {periodo:.1f} h)" if periodo and periodo >= 1
                 else f" (one oscillation at νmax ≈ {periodo * 60:.1f} min)" if periodo else ""), fontsize="medium")


def _fmt(v, e=None, cifre=3):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    if e is None:
        return f"{v:.{cifre}g}"
    return f"{v:.{cifre}g} ± {e:.2g}"


def testo_risultati_inglese(ris):
    an = ris["analisi"]
    righe = [f"{ris['nome']}   ({ris['chiave'].replace('_', ' ')})"]
    righe.append(f"Data: {ris.get('missione', '')} {ris.get('cadenza_testo', '')}, {ris.get('n_file', 0)} files, "
                 f"{ris['t'].size} points over {ris['t'][-1] - ris['t'][0]:.0f} days; "
                 f"Nyquist {ris['nyquist']:.0f} µHz, resolution {ris['risoluzione']:.3f} µHz")
    if an.get("numax"):
        righe.append(f"νmax = {an['numax']:.3f} ± {an['e_numax']:.3f} µHz     Δν = {an['dnu']:.4f} ± {an['e_dnu']:.4f} µHz")
        giud = {"sicuro": "YES", "probabile": "PROBABLE (νmax reliable, Δν to be checked)"}.get(an.get("giudizio"), "DOUBTFUL")
        righe.append(f"Detection: {giud} (comb z = {an['z_dnu']:.1f}, power excess z = {an.get('z_eccesso', 0):.1f}, "
                     f"mean S/B at νmax {an['snr_medio']:.2f}), amplitude ≈ {an['ampiezza_ppm']:.0f} ppm")
    else:
        if an.get("giudizio") == "fuori portata":
            righe.append("Oscillations out of reach with these data: expected νmax above the Nyquist frequency "
                         "(shorter-cadence data needed)")
        else:
            righe.append("No oscillations detected: " + str(an.get("motivo", "")))
    te = ris.get("teff")
    if te and te[0]:
        righe.append(f"Teff = {te[0]:.0f} ± {te[1]:.0f} K ({te[2]});  [Fe/H] = {ris['feh'][0]:+.2f} ({ris['feh'][2]})")
    s = ris.get("scala")
    if ris.get("raggio_catalogo"):
        righe.append(f"Catalogue radius (TIC v8, from Gaia): {ris['raggio_catalogo']:.2f} R☉")
    if s:
        righe.append(f"Scaling relations (uncorrected): M = {s['M']:.2f} ± {s['e_M']:.2f} M☉, "
                     f"R = {s['R']:.2f} ± {s['e_R']:.2f} R☉, log g = {s['logg']:.3f} ± {s['e_logg']:.3f}, "
                     f"ρ = {s['rho']:.3g} ρ☉, L = {s['L']:.3g} L☉")
    e = ris.get("eta")
    if e:
        p = e["prob_fasi"]
        righe.append(f"MIST grid ([Fe/H] = {e['feh_griglia']:+.2f}{', ' + ris['stato_catalogo'] + ' from catalogue' if ris.get('stato_catalogo') else ''}): age = {e['eta']:.2f} +{e['eta_piu']:.2f} "
                     f"−{e['eta_meno']:.2f} Gyr, M = {e['massa']:.2f} ± {e['e_massa']:.2f} M☉, "
                     f"R = {e['raggio']:.2f} ± {e['e_raggio']:.2f} R☉")
        righe.append(f"Phase probabilities: main sequence {p['MS']:.0%}, red-giant branch {p['RGB']:.0%}, "
                     f"core-helium burning (red clump) {p['CHeB']:.0%}")
    elif ris.get("eta_motivo"):
        righe.append("Age: " + ris["eta_motivo"])
    for nome, v in ris.get("cataloghi", {}).items():
        pezzi = [f"{nome}:"]
        if v.get("numax"):
            pezzi.append(f"νmax {v['numax']:.3f}")
            if an.get("numax"):
                pezzi.append(f"({(an['numax'] / v['numax'] - 1) * 100:+.1f} %)")
        if v.get("dnu"):
            pezzi.append(f"Δν {v['dnu']:.4f}")
            if an.get("dnu"):
                pezzi.append(f"({(an['dnu'] / v['dnu'] - 1) * 100:+.1f} %)")
        if v.get("mass"):
            pezzi.append(f"M {v['mass']:.2f}")
        if v.get("radius"):
            pezzi.append(f"R {v['radius']:.2f}")
        if v.get("state"):
            pezzi.append(f"state {v['state']}")
        if v.get("phase") is not None:
            pezzi.append(f"phase {['unclassified', 'RGB', 'HeB'][int(v['phase'])] if v['phase'] in (0, 1, 2) else v['phase']}")
        if v.get("age_rgb"):
            pezzi.append(f"age(RGB) {v['age_rgb']:.2f} Gyr")
        if v.get("age_rc"):
            pezzi.append(f"age(RC) {v['age_rc']:.2f} Gyr")
        righe.append(" ".join(pezzi))
    if ris.get("suono"):
        so = ris["suono"]
        righe.append(f"Sound: light curve sped up {so['k']:.3g} times, νmax → {so['nota_hz']:.0f} Hz "
                     f"({so['durata_s']:.0f} s)")
    if ris.get("suono_modi"):
        sm = ris["suono_modi"]
        righe.append(f"Mode chord: {sm['n_modi']} detected modes as pure tones, {sm['da_hz']:.0f}–{sm['a_hz']:.0f} Hz "
                     f"(same frequency ratios)")
    return righe


def scrivi_scheda(ris, cartella):
    """Scheda PDF/PNG con caratteri sempre uguali (non dipende dalla dimensione del testo della finestra)."""
    import matplotlib
    with matplotlib.rc_context({"font.size": 10.0, "lines.linewidth": 1.5}):
        return _scrivi_scheda(ris, cartella)


def _scrivi_scheda(ris, cartella):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure(figsize=(11.69, 8.27))
    FigureCanvasAgg(fig)
    disegna_pannelli(fig, ris)
    fig.suptitle(f"{ris['nome']} – asteroseismology", fontsize=15, fontweight="bold", y=0.985)
    righe = testo_risultati_inglese(ris)
    fig.text(0.06, 0.952, "\n".join(righe[1:3]), fontsize=8, va="top", family="DejaVu Sans")
    # pagina 2: testo completo
    fig2 = Figure(figsize=(11.69, 8.27))
    FigureCanvasAgg(fig2)
    fig2.text(0.06, 0.94, f"{ris['nome']} – results", fontsize=15, fontweight="bold", va="top")
    import textwrap
    corpo = []
    for r in righe:
        corpo.extend(textwrap.wrap(r, 130) or [""])
        corpo.append("")
    note = ("Notes. Power spectrum: exact DFT at the true (barycentric) times, computed with FFT plus a Taylor "
            "expansion of the offsets from a regular grid; long-term trends removed with an 8-day (Kepler) or 4-day (TESS) "
            "Gaussian high-pass filter. νmax: centre of the Gaussian envelope fitted together with the background "
            "(two Harvey components + white noise, sinc² apodization) on the log-binned spectrum. Δν: peak of the "
            "autocorrelation of the background-normalised spectrum around νmax, replaced by the step that best aligns "
            "the échelle ridges when this is much sharper (mixed modes in subgiants). Data weighted by the local noise "
            "(1/σ² in 10-day blocks). Detection: comb significance z and "
            "power-excess significance (YES: z ≥ 5.5, or z ≥ 4.5 with excess ≥ 10; PROBABLE: clear excess, weak comb). "
            "Uncertainties from Monte-Carlo resampling (refit of the binned spectrum for νmax, autocorrelation for Δν). "
            f"Scaling relations with solar references νmax☉ = {NUMAX_SOLE:.0f} µHz, Δν☉ = {DNU_SOLE} µHz, "
            f"Teff☉ = {TEFF_SOLE:.0f} K, no Δν correction (for red giants masses may be overestimated by a few %). "
            "Ages from a grid of MIST v1.2 tracks (vvcrit = 0.4) at the nearest metallicity, fitting νmax, Δν and Teff "
            "with scaling relations applied to the models; Salpeter IMF and uniform age prior, ages capped at 14 Gyr.")
    corpo.extend(textwrap.wrap(note, 130))
    fig2.text(0.06, 0.88, "\n".join(corpo), fontsize=9, va="top", family="DejaVu Sans")
    for fg in (fig, fig2):
        fg.text(0.99, 0.006, f"Il battito delle stelle {VERSIONE} – {FIRMA}", ha="right", fontsize=8, color="#555555")
        fg.text(0.01, 0.006, datetime.now().strftime("%Y-%m-%d %H:%M"), ha="left", fontsize=8, color="#555555")
    cartella.mkdir(parents=True, exist_ok=True)
    base = cartella / ris["chiave"]
    png = Path(str(base) + "_sheet.png")
    pdf = Path(str(base) + "_sheet.pdf")
    fig.savefig(png, dpi=150)
    from matplotlib.backends.backend_pdf import PdfPages
    with PdfPages(pdf) as pp:
        pp.savefig(fig)
        pp.savefig(fig2)
    return png, pdf


CAMPI_CSV = ["key", "name", "kic", "tic", "mission", "cadence_s", "n_files", "n_points", "timespan_d",
             "nyquist_uHz", "detected", "numax_uHz", "e_numax_uHz", "dnu_uHz", "e_dnu_uHz", "comb_z",
             "amplitude_ppm", "teff_K", "e_teff_K", "teff_source", "feh", "feh_source",
             "mass_scaling", "e_mass_scaling", "radius_scaling", "e_radius_scaling", "logg_scaling",
             "e_logg_scaling", "density_solar", "luminosity_solar", "age_Gyr", "age_minus_Gyr", "age_plus_Gyr",
             "mass_grid", "radius_grid", "p_MS", "p_RGB", "p_CHeB", "mist_feh",
             "cat_numax_APOKASC3", "cat_dnu_APOKASC3", "cat_numax_APOKASC2", "cat_dnu_APOKASC2",
             "cat_numax_Yu18", "cat_dnu_Yu18", "date"]


def riga_csv(ris):
    an, s, e = ris["analisi"], ris.get("scala") or {}, ris.get("eta") or {}
    c = ris.get("cataloghi", {})
    te, fe = ris.get("teff") or (None, None, ""), ris.get("feh") or (None, None, "")

    def r(v, n=5):
        return "" if v is None or (isinstance(v, float) and not math.isfinite(v)) else (round(v, n) if isinstance(v, float) else v)
    p = e.get("prob_fasi", {})
    return {"key": ris["chiave"], "name": ris["nome"], "kic": ris.get("kic") or "", "tic": ris.get("tic") or "",
            "mission": ris.get("missione", ""), "cadence_s": r(ris.get("cadenza_s"), 2), "n_files": ris.get("n_file", 0),
            "n_points": int(ris["t"].size), "timespan_d": r(float(ris["t"][-1] - ris["t"][0]), 2),
            "nyquist_uHz": r(ris["nyquist"], 2), "detected": {"sicuro": "yes", "probabile": "probable"}.get(an.get("giudizio"), "no"),
            "numax_uHz": r(an.get("numax")), "e_numax_uHz": r(an.get("e_numax")), "dnu_uHz": r(an.get("dnu")),
            "e_dnu_uHz": r(an.get("e_dnu")), "comb_z": r(an.get("z_dnu"), 2), "amplitude_ppm": r(an.get("ampiezza_ppm"), 1),
            "teff_K": r(te[0], 1), "e_teff_K": r(te[1], 1), "teff_source": te[2], "feh": r(fe[0], 3), "feh_source": fe[2],
            "mass_scaling": r(s.get("M"), 4), "e_mass_scaling": r(s.get("e_M"), 4), "radius_scaling": r(s.get("R"), 4),
            "e_radius_scaling": r(s.get("e_R"), 4), "logg_scaling": r(s.get("logg"), 4), "e_logg_scaling": r(s.get("e_logg"), 4),
            "density_solar": r(s.get("rho"), 6), "luminosity_solar": r(s.get("L"), 4),
            "age_Gyr": r(e.get("eta"), 3), "age_minus_Gyr": r(e.get("eta_meno"), 3), "age_plus_Gyr": r(e.get("eta_piu"), 3),
            "mass_grid": r(e.get("massa"), 3), "radius_grid": r(e.get("raggio"), 3),
            "p_MS": r(p.get("MS"), 3), "p_RGB": r(p.get("RGB"), 3), "p_CHeB": r(p.get("CHeB"), 3),
            "mist_feh": r(e.get("feh_griglia"), 2),
            "cat_numax_APOKASC3": r(c.get("APOKASC-3", {}).get("numax")), "cat_dnu_APOKASC3": r(c.get("APOKASC-3", {}).get("dnu")),
            "cat_numax_APOKASC2": r(c.get("APOKASC-2", {}).get("numax")), "cat_dnu_APOKASC2": r(c.get("APOKASC-2", {}).get("dnu")),
            "cat_numax_Yu18": r(c.get("Yu+2018", {}).get("numax")), "cat_dnu_Yu18": r(c.get("Yu+2018", {}).get("dnu")),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M")}


def scrivi_csv(ris, cartella_stella, cartella_principale):
    riga = riga_csv(ris)
    p = cartella_stella / f"{ris['chiave']}_results.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, CAMPI_CSV)
        w.writeheader()
        w.writerow(riga)
    # spettro (fino a 2 volte la frequenza massima utile) e curva di luce
    an = ris["analisi"]
    fmax = min(ris["nyquist"], 3 * an["numax"]) if an.get("numax") else ris["nyquist"]
    sel = ris["freq"] <= fmax
    passo = max(1, int(sel.sum() // 200000))
    idx = np.nonzero(sel)[0][::passo]
    colonne = [ris["freq"][idx], ris["psd"][idx]] + ([an["bg"][idx]] if "bg" in an else [])
    np.savetxt(cartella_stella / f"{ris['chiave']}_power_spectrum.csv", np.column_stack(colonne), delimiter=",",
               fmt=["%.5f"] + ["%.5g"] * (len(colonne) - 1), comments="",
               header="frequency_uHz,psd_ppm2_per_uHz" + (",background_ppm2_per_uHz" if "bg" in an else ""))
    if ris["t"].size <= 2_000_000:
        np.savetxt(cartella_stella / f"{ris['chiave']}_light_curve.csv", np.column_stack([ris["t"], ris["x"]]),
                   delimiter=",", fmt=["%.6f", "%.2f"], comments="", header="time_days,flux_ppm")
    # tabella generale: una riga per stella (aggiornata)
    gen = cartella_principale / "all_results.csv"
    righe = []
    if gen.exists():
        try:
            with open(gen, newline="", encoding="utf-8") as fh:
                righe = [r for r in csv.DictReader(fh) if r.get("key") != ris["chiave"]]
        except Exception:
            righe = []
    righe.append(riga)
    with open(gen, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, CAMPI_CSV, extrasaction="ignore")
        w.writeheader()
        for r in righe:
            w.writerow(r)
    return p


# =============================================================================
# ANALISI COMPLETA DI UNA STELLA
# =============================================================================
def file_locali(percorso):
    p = Path(percorso)
    if p.is_file():
        return [p]
    return sorted([q for q in p.rglob("*.fits") if "lc" in q.name.lower()] or list(p.rglob("*.fits")))


def analizza_stella(testo, opzioni, log=print, avanzamento=None, ferma=None):
    """opzioni: missione, cadenza, teff, feh, eta (bool), suono (bool), cartella (Path), numax_guida.
    Restituisce il dict dei risultati (con t, x, freq, psd per i grafici)."""
    t_inizio = time.time()
    cartella_principale = Path(opzioni.get("cartella") or cartella_risultati_predefinita())
    x_in = interpreta_testo(testo)
    if x_in and x_in["tipo"] == "percorso":
        percorsi = file_locali(x_in["valore"])
        if not percorsi:
            raise RuntimeError(f"nessun file FITS in {x_in['valore']}")
        curve = [leggi_curva(p) for p in percorsi]
        i0 = curve[0]["info"]
        info = {"testo": testo, "kic": int(i0["kic"]) if i0.get("kic") else None,
                "tic": int(i0["tic"]) if i0.get("tic") else None, "ra": i0.get("ra"), "dec": i0.get("dec"),
                "riga_tic": None, "nome": i0.get("oggetto") or Path(x_in["valore"]).name}
        info["chiave"] = (f"KIC_{info['kic']}" if info["kic"] else f"TIC_{info['tic']}" if info["tic"]
                          else re.sub(r"[^A-Za-z0-9_+-]+", "_", info["nome"]))
        log(f"   {len(curve)} file locali letti")
        if info["kic"] or info["tic"]:
            try:
                f_ = [{"paramName": "KIC", "values": [str(info["kic"])]}] if info["kic"] else \
                     [{"paramName": "ID", "values": [str(info["tic"])]}]
                r = tic_per_filtri(f_, ferma)
                if r:
                    info["riga_tic"] = r[0]
            except Exception as e:
                log(f"   TIC non raggiungibile: {e}")
    else:
        log("1/6  Cerco la stella…")
        info = risolvi_stella(testo, log, ferma)
        rt = info.get("riga_tic") or {}
        log(f"   {info['nome']}: KIC {info['kic'] or '–'}, TIC {info['tic'] or '–'}, "
            f"Tmag {rt.get('Tmag', '–')}, Teff (TIC) {rt.get('Teff', '–')}")
        log("2/6  Cerco le curve di luce su MAST…")
        elenco = elenca_curve(info, opzioni.get("missione", "auto"), opzioni.get("cadenza", "auto"), log, ferma)
        if not elenco:
            raise RuntimeError("nessuna curva di luce Kepler o TESS per questa stella")
        tot = sum(int(f.get("size") or 0) for f in elenco)
        log(f"   {len(elenco)} file ({elenco[0]['missione']}, {elenco[0]['cadenza_s']:.0f} s), "
            f"{tot / 1e6:.1f} MB (quelli già scaricati restano in cache)")
        from concurrent.futures import ThreadPoolExecutor, as_completed
        percorsi = [None] * len(elenco)
        fatti, mb, t_s = 0, 0.0, time.time()
        with ThreadPoolExecutor(max_workers=4) as ex:
            futuri = {ex.submit(scarica, f, None, ferma): i for i, f in enumerate(elenco)}
            for fu in as_completed(futuri):
                i = futuri[fu]
                percorsi[i] = fu.result()
                fatti += 1
                mb += (elenco[i].get("size") or 0) / 1e6
                if avanzamento:
                    avanzamento(fatti, len(elenco), f"Scarico {fatti}/{len(elenco)}: {elenco[i]['nome']}")
                if len(elenco) > 8 and (fatti % 10 == 0 or fatti == len(elenco)):
                    log(f"   scaricati {fatti}/{len(elenco)} file ({mb:.0f} MB, {time.time() - t_s:.0f} s)")
        curve = []
        for p in percorsi:
            try:
                curve.append(leggi_curva(p))
            except Exception as e:
                log(f"   {p.name}: non leggibile ({e}); lo cancello dalla cache")
                p.unlink(missing_ok=True)
    if ferma is not None and ferma.is_set():
        raise Interrotto()
    log("3/6  Pulisco e unisco le curve di luce…")
    t, x, dt, det = unisci_segmenti(curve, log)
    missione = curve[0]["info"]["missione"]
    cad_s = dt * 86400
    cad_testo = f"{cad_s / 60:.1f}-min cadence" if cad_s >= 60 else f"{cad_s:.0f}-s cadence"
    log(f"   {t.size} punti su {t[-1] - t[0]:.0f} giorni, cadenza {cad_s:.0f} s, dispersione {np.std(x):.0f} ppm")
    log("4/6  Calcolo lo spettro di potenza e cerco νmax e Δν…")
    pesi = pesi_rumore(t, x)
    var_pesi = float(np.max(pesi) / np.min(pesi))
    if var_pesi > 2:
        log(f"   rumore non uniforme (pesi da {np.min(pesi):.2f} a {np.max(pesi):.2f}): i tratti rumorosi contano meno")
    freq, psd, nyq, ris_f = spettro_potenza(t, x, dt, pesi=pesi)
    sovr = max(1, int(round(ris_f / (freq[1] - freq[0]))))
    # νmax atteso da log g e Teff del catalogo TIC (solo come guida: cerca tra 1/4 e 4 volte questo valore)
    rt = info.get("riga_tic") or {}
    logg_tic, teff_tic = numero(rt.get("logg")), numero(rt.get("Teff")) or 5000.0
    rad_tic, massa_tic = numero(rt.get("rad")), numero(rt.get("mass"))
    intervallo, previsto = None, None
    if not opzioni.get("numax_guida"):
        if logg_tic is not None:
            previsto = NUMAX_SOLE * 10 ** (logg_tic - LOGG_SOLE) * (teff_tic / TEFF_SOLE) ** -0.5
            log(f"   νmax atteso dal catalogo TIC (log g {logg_tic:.2f}): circa {previsto:.0f} µHz")
        elif rad_tic and rad_tic > 0:
            # giganti e subgiganti: nel TIC c'è il raggio (da Gaia) ma non log g; massa tipica 1,2 M☉
            m_ipot = massa_tic if (massa_tic and massa_tic > 0) else 1.2
            previsto = NUMAX_SOLE * m_ipot / rad_tic ** 2 * (teff_tic / TEFF_SOLE) ** -0.5
            log(f"   νmax atteso dal raggio del catalogo TIC (R = {rad_tic:.2f} R☉, M ipotizzata {m_ipot:.1f} M☉): "
                f"circa {previsto:.0f} µHz")
        if previsto:
            intervallo = (previsto / 4.0, previsto * 4.0)
    if previsto and previsto / 2.0 > 0.9 * nyq:
        an = {"rilevato": False, "giudizio": "fuori portata", "nyquist": nyq, "risoluzione": ris_f,
              "motivo": (f"νmax atteso circa {previsto:.0f} µHz, oltre il limite di questi dati "
                         f"(Nyquist {nyq:.0f} µHz): servono dati a cadenza più corta "
                         f"(Kepler 1 min o TESS 2 min / 20 s)")}
    else:
        an = analizza_spettro(freq, psd, nyq, ris_f, sovr=sovr, log=log, numax_guida=opzioni.get("numax_guida"),
                              intervallo=intervallo)
        if previsto and not an.get("numax"):
            # nessun candidato nell'intervallo: ultima prova su tutto lo spettro
            an = analizza_spettro(freq, psd, nyq, ris_f, sovr=sovr, log=log)
    # con Kepler: se νmax è troppo vicino a Nyquist (30 min) e ci sono dati a 1 min, l'utente può rifare con «corta»
    if an.get("numax"):
        log(f"   νmax = {an['numax']:.3f} ± {an['e_numax']:.3f} µHz,  Δν = {an['dnu']:.4f} ± {an['e_dnu']:.4f} µHz "
            f"(oscillazioni: {an.get('giudizio', 'dubbio').upper()}; pettine z = {an['z_dnu']:.1f}, eccesso z = {an.get('z_eccesso', 0):.1f})")
        if an.get("dnu_acf") and abs(an["dnu"] - an["dnu_acf"]) > 1e-9:
            log(f"   Δν rifinito con la piegatura dello spettro (l'autocorrelazione dava {an['dnu_acf']:.4f} µHz: "
                f"probabili modi misti)")
        if an.get("motivo"):
            log("   " + an["motivo"])
        if an["numax"] > 0.8 * nyq:
            log("   ATTENZIONE: νmax è vicino alla frequenza di Nyquist: prova con la cadenza «corta».")
    else:
        log("   Oscillazioni non trovate: " + str(an.get("motivo", "")))
    ris = {"nome": info["nome"], "chiave": info["chiave"], "kic": info.get("kic"), "tic": info.get("tic"),
           "missione": missione, "cadenza_s": cad_s, "cadenza_testo": cad_testo, "n_file": len(curve),
           "t": t, "x": x, "dt": dt, "freq": freq, "psd": psd, "nyquist": nyq, "risoluzione": ris_f,
           "analisi": an, "dettagli": det, "raggio_catalogo": rad_tic}

    log("5/6  Temperatura, metallicità, cataloghi e relazioni di scala…")
    cataloghi = {}
    if info.get("kic"):
        cataloghi = cerca_cataloghi(info["kic"], log, ferma)
        for nome, v in cataloghi.items():
            log(f"   {nome}: νmax {v.get('numax', '–')}, Δν {v.get('dnu', '–')}, fase {testo_fase(v)}")
        g3 = cataloghi.get("APOKASC-3", {}).get("gaia")
        if g3:
            info["gaia_dr3"] = g3
    ris["cataloghi"] = cataloghi
    gaia = None
    if not (opzioni.get("teff") and opzioni.get("feh") is not None):
        gaia = gaia_dr3(info, log, ferma)
    ris["gaia"] = gaia
    teff, feh = scegli_teff_feh(info, cataloghi, gaia, curve[0]["info"], opzioni.get("teff"), opzioni.get("feh"))
    ris["teff"], ris["feh"] = teff, feh
    if teff[0]:
        log(f"   Teff = {teff[0]:.0f} ± {teff[1]:.0f} K ({teff[2]}), [Fe/H] = {feh[0]:+.2f} ({feh[2]})")
    else:
        log("   Teff non trovata: scrivila a mano nelle opzioni per avere massa e raggio")
    affidabile = an.get("giudizio") in ("sicuro", "probabile")
    if an.get("numax") and teff[0] and not affidabile:
        log("   segnale dubbio: massa, raggio ed età non calcolati")
    if an.get("numax") and teff[0] and affidabile and rad_tic and rad_tic > 0:
        r_osc = (an["numax"] / NUMAX_SOLE) * (an["dnu"] / DNU_SOLE) ** -2 * (teff[0] / TEFF_SOLE) ** 0.5
        if not (1 / 1.6 < r_osc / rad_tic < 1.6):
            an["giudizio"] = "dubbio"
            an["rilevato"] = False
            an["motivo"] = (f"raggio dalle oscillazioni {r_osc:.2f} R☉ incompatibile con quello del catalogo TIC "
                            f"({rad_tic:.2f} R☉, da Gaia): probabile segnale strumentale o stella sbagliata")
            affidabile = False
            log("   ATTENZIONE: " + an["motivo"] + " → giudizio DUBBIO")
    if an.get("numax") and teff[0] and affidabile:
        ris["scala"] = relazioni_scala(an["numax"], an["e_numax"], an["dnu"], an["e_dnu"], teff[0], teff[1])
        s = ris["scala"]
        log(f"   M = {s['M']:.2f} ± {s['e_M']:.2f} M☉, R = {s['R']:.2f} ± {s['e_R']:.2f} R☉, "
            f"log g = {s['logg']:.3f}, ρ = {s['rho']:.3g} ρ☉")
    log("6/6  Età, suono e scheda…")
    if opzioni.get("eta", True) and an.get("numax") and teff[0] and affidabile:
        try:
            g = griglia_mist(feh[0], log=log, ferma=ferma,
                             avanzamento=(lambda a, b: avanzamento(a, b or 1, f"MIST: {a / 1e6:.0f} di {b / 1e6:.0f} MB"))
                             if avanzamento else None)
            stato = stato_dai_cataloghi(cataloghi)
            ris["stato_catalogo"] = stato
            ris["eta"] = eta_da_griglia(g, an["numax"], an["e_numax"], an["dnu"], an["e_dnu"], teff[0], teff[1], stato)
            if ris["eta"]:
                e = ris["eta"]
                log(f"   età {e['eta']:.2f} (+{e['eta_piu']:.2f} −{e['eta_meno']:.2f}) miliardi di anni, "
                    f"M = {e['massa']:.2f} M☉ (MIST, [Fe/H] {e['feh_griglia']:+.2f}"
                    f"{', stato ' + stato + ' dal catalogo' if stato else ''})")
            else:
                ris["eta_motivo"] = "no MIST model matches the observed values"
                log("   nessun modello MIST compatibile")
        except Interrotto:
            raise
        except Exception as e:
            ris["eta_motivo"] = f"MIST not available ({type(e).__name__})"
            log(f"   MIST non disponibile: {type(e).__name__}: {e}")
    cartella = cartella_principale / info["chiave"]
    cartella.mkdir(parents=True, exist_ok=True)
    if opzioni.get("suono", True) and an.get("numax") and affidabile:
        try:
            ris["suono"] = suono(t, x, dt, an["numax"], an.get("sigma_inviluppo") or fwhm_inviluppo(an["numax"]) / 2.355,
                                 cartella / f"{info['chiave']}_sound.wav")
            ris["wav"] = cartella / f"{info['chiave']}_sound.wav"
        except Exception as e:
            log(f"   suono non creato: {e}")
        try:
            ris["suono_modi"] = suono_modi(an, cartella / f"{info['chiave']}_modes.wav")
            ris["wav_modi"] = cartella / f"{info['chiave']}_modes.wav"
            so = ris["suono_modi"]
            log(f"   accordo dei modi: {so['n_modi']} modi, da {so['da_hz']:.0f} a {so['a_hz']:.0f} Hz")
        except Exception as e:
            log(f"   accordo dei modi non creato: {e}")
    png, pdf = scrivi_scheda(ris, cartella)
    scrivi_csv(ris, cartella, cartella_principale)
    ris["png"], ris["pdf"], ris["cartella"] = png, pdf, cartella
    log(f"   Fatto in {time.time() - t_inizio:.0f} s → {cartella}")
    return ris
