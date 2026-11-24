#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IL BATTITO DELLE STELLE 1.16 - astrosismologia con Kepler e TESS
==============================================================
© 2026 Edoardo Battistel

Scrivi il nome di una stella (o un elenco): il programma scarica le curve di luce da MAST,
trova le oscillazioni (νmax e Δν), ricava massa, raggio, gravità, densità ed età, le confronta
con i cataloghi APOKASC e Yu+2018 e prepara scheda PDF/PNG (in inglese), CSV e il suono della stella.

Uso:   python3 battito_stelle.py                      finestra
       python3 battito_stelle.py --analizza "KIC 757137" ["16 Cyg A" …] [--missione kepler|tess]
                                 [--cadenza lunga|corta|veloce] [--teff 4750] [--feh 0.1]
                                 [--senza-eta] [--senza-suono]
       python3 battito_stelle.py --autotest            prove senza rete (apre la finestra per qualche secondo)
       python3 battito_stelle.py --diagnostica         prova degli archivi in rete → diagnostica.txt
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

if getattr(sys, "frozen", False):
    QUI = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI))

import motore_battito as M  # noqa: E402

TITOLO = f"Il battito delle stelle {M.VERSIONE}"
CLASSE = "BattitoStelle"          # Tk la trasforma in «Battitostelle» (StartupWMClass)
CONFIG = Path.home() / ".battito_stelle.json"
ESEMPI = [
    ("KIC 757137 – gigante rossa (Kepler, 30 min)", "KIC 757137"),
    ("KIC 1027337 – gigante rossa più compatta", "KIC 1027337"),
    ("KIC 11026764 «Gemma» – subgigante (Kepler, 1 min)", "KIC 11026764"),
    ("16 Cygni A (KIC 12069424) – quasi gemella del Sole", "KIC 12069424"),
    ("KIC 6106415 «Perky» – stella tipo Sole", "KIC 6106415"),
]
MISSIONI = {"Automatica": "auto", "Solo Kepler": "kepler", "Solo TESS": "tess"}
CADENZE = {"Automatica": "auto", "Lunga (30 min, giganti)": "lunga",
           "Corta (1-2 min, tipo Sole)": "corta", "TESS 20 s": "veloce"}
SCALE = [f"{p} %" for p in range(100, 210, 10)]


def largo(valori, minimo=4):
    """Larghezza (in caratteri) di una tendina: quanto la voce più lunga, perché anche l'elenco a discesa
    di Tk è largo quanto la tendina e altrimenti taglia le frasi."""
    return max([minimo] + [len(str(v)) + 2 for v in valori])
GRAFICI = [("lc", "Curva di luce"), ("ps", "Spettro"), ("zm", "Modi attorno a νmax"),
           ("ec", "Diagramma échelle"), ("acf", "Autocorrelazione"), ("tutti", "Tutti insieme")]


def leggi_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def salva_config(d):
    try:
        CONFIG.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def ambiente_esterno():
    """Ambiente per i programmi esterni: toglie le librerie di PyInstaller (LD_LIBRARY_PATH)."""
    env = dict(os.environ)
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    return env


def apri_esterno(percorso):
    p = str(percorso)
    if sys.platform.startswith("win"):
        os.startfile(p)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p], env=ambiente_esterno(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def numero_utente(testo):
    t = (testo or "").strip().replace(",", ".")
    if not t:
        return None
    return float(t)


# =============================================================================
# FINESTRA
# =============================================================================
def crea_app(tk, ttk, tkfont):
    """Crea la classe della finestra (import di Tk fatto solo quando serve)."""
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    import matplotlib

    class RigaACapo(ttk.Frame):
        """Comandi in fila che vanno a capo quando la larghezza non basta. I comandi sono messi a coordinate
        calcolate (place): la riga non chiede mai più larghezza di quella che le dà la finestra, quindi
        non può spingere niente fuori dallo schermo; la sua altezza si adatta al numero di righe."""
        def __init__(self, padre, spazio=6, **kw):
            super().__init__(padre, **kw)
            self.elementi = []
            self.spazio = spazio
            self._ultima = None
            self._altezza = 10
            self.configure(height=10)
            self.bind("<Configure>", lambda e: self.disponi(e.width))

        def aggiungi(self, w):
            self.elementi.append(w)
            self.disponi()
            return w

        def disponi(self, larghezza=None):
            larg = larghezza or self.winfo_width()
            if larg <= 1:
                larg = 600                      # prima che la finestra sia visibile: poi si ridispone
            posti, x, y, alt_riga = [], 0, 0, 0
            for w in self.elementi:
                lw, lh = w.winfo_reqwidth(), w.winfo_reqheight()
                if x > 0 and x + lw > larg:
                    y += alt_riga + 4
                    x, alt_riga = 0, 0
                posti.append((w, x, y))
                x += lw + self.spazio
                alt_riga = max(alt_riga, lh)
            altezza = y + alt_riga + 4
            chiave = (larg, tuple((px, py, w.winfo_reqwidth(), w.winfo_reqheight()) for w, px, py in posti))
            if chiave == self._ultima:
                return
            self._ultima = chiave
            for w, px, py in posti:
                w.place(in_=self, x=px, y=py + 2)
            if self._altezza != altezza:
                self._altezza = altezza
                self.configure(height=altezza)

    class EtichettaACapo(ttk.Label):
        """Etichetta il cui testo va a capo seguendo la larghezza."""
        def __init__(self, padre, **kw):
            super().__init__(padre, **kw)
            self.bind("<Configure>", lambda e: self.configure(wraplength=max(50, e.width - 8)))

    class App(tk.Tk):
        def __init__(self):
            super().__init__(className=CLASSE)
            self.cfg = leggi_config()
            self.title(f"{TITOLO} – {M.FIRMA}")
            self.coda = queue.Queue()
            self.ferma = threading.Event()
            self.lavoro = None
            self.risultati = {}
            self.ordine = []
            self.attuale = None
            self.suonando = None
            self.suonando_chiave = None
            self.finestra_trova = None
            self.cartella = Path(self.cfg.get("cartella") or M.cartella_risultati_predefinita())
            self._icona()
            self.stile = ttk.Style(self)
            try:
                self.stile.theme_use("clam")
            except tk.TclError:
                pass
            self.scala = tk.StringVar(value=self.cfg.get("scala", "150 %"))
            m = self.cfg.get("missione")
            c = self.cfg.get("cadenza")
            self.missione = tk.StringVar(value=m if m in MISSIONI else list(MISSIONI)[0])
            self.cadenza = tk.StringVar(value=c if c in CADENZE else list(CADENZE)[0])
            self.teff = tk.StringVar(value="")
            self.feh = tk.StringVar(value="")
            self.con_eta = tk.BooleanVar(value=self.cfg.get("eta", True))
            self.con_suono = tk.BooleanVar(value=self.cfg.get("suono", True))
            self.stato = tk.StringVar(value="Pronto. Scrivi una stella (o scegli un esempio) e premi «Analizza».")
            self.scelta = tk.StringVar(value="")
            self.quale = self.cfg.get("grafico", "ps") if self.cfg.get("grafico") in dict(GRAFICI) else "ps"
            self._costruisci()
            self.applica_scala()
            self.protocol("WM_DELETE_WINDOW", self.chiudi)
            self.after(120, self._leggi_coda)
            w, h = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry(f"{int(w * 0.85)}x{int(h * 0.82)}+{int(w * 0.04)}+{int(h * 0.04)}")
            self.minsize(400, 300)
            self.maxsize(w, h)
            # aperta ingrandita: non può uscire dallo schermo
            self.after(50, self._ingrandisci)

        # ------------------------------------------------------------ costruzione
        def _ingrandisci(self):
            try:
                if sys.platform.startswith("win"):
                    self.state("zoomed")
                else:
                    self.attributes("-zoomed", True)
            except tk.TclError:
                pass

        def _icona(self):
            for p in (QUI / "battito_stelle.png", Path(__file__).resolve().parent / "battito_stelle.png"):
                if p.exists():
                    try:
                        self._img_icona = tk.PhotoImage(file=str(p))
                        self.iconphoto(True, self._img_icona)
                    except tk.TclError:
                        pass
                    break

        def _costruisci(self):
            # in basso (sempre visibile): avanzamento, stato, pulsanti, cartella, firma
            basso = ttk.Frame(self, padding=(8, 0, 8, 6))
            basso.pack(side="bottom", fill="x")
            self.barra_av = ttk.Progressbar(basso, mode="determinate")
            self.barra_av.pack(fill="x")
            EtichettaACapo(basso, textvariable=self.stato).pack(fill="x", pady=(2, 4))
            pulsanti = RigaACapo(basso)
            pulsanti.pack(fill="x")
            self.riga_pulsanti = pulsanti
            self.b_ascolta = ttk.Button(pulsanti, text="Ascolta la stella", command=lambda: self.ascolta("wav"),
                                        state="disabled")
            self.b_modi = ttk.Button(pulsanti, text="Ascolta i modi", command=lambda: self.ascolta("wav_modi"),
                                     state="disabled")
            self.b_pdf = ttk.Button(pulsanti, text="Apri la scheda PDF", command=self.apri_pdf, state="disabled")
            self.b_cart = ttk.Button(pulsanti, text="Apri la cartella dei risultati", command=self.apri_cartella)
            self.b_cambia = ttk.Button(pulsanti, text="Cambia cartella…", command=self.cambia_cartella)
            self.b_guida = ttk.Button(pulsanti, text="Guida", command=self.guida)
            for b_ in (self.b_ascolta, self.b_modi, self.b_pdf, self.b_cart, self.b_cambia, self.b_guida):
                pulsanti.aggiungi(b_)
            self.l_cartella = EtichettaACapo(basso, text="")
            self.l_cartella.pack(fill="x")
            ttk.Label(basso, text=f"{TITOLO} – {M.FIRMA}", foreground="#666666").pack(anchor="e")

            # due schede: stelle, opzioni e registro / un grafico alla volta, grande
            self.schede = ttk.Notebook(self)
            self.schede.pack(fill="both", expand=True, padx=8, pady=(6, 4))
            pag1 = ttk.Frame(self.schede, padding=6)
            pag2 = ttk.Frame(self.schede, padding=6)
            self.schede.add(pag1, text="Stelle e risultati")
            self.schede.add(pag2, text="Grafici")
            self.pag_stelle, self.pag_grafici = pag1, pag2

            EtichettaACapo(pag1, text="Stelle da analizzare, una per riga: nome (es. «16 Cyg A»), «KIC 757137», "
                                      "«TIC 261136679», coordinate in gradi («291.05 36.56») oppure una cartella con "
                                      "file FITS di curve di luce.").pack(fill="x")
            self.testo_stelle = tk.Text(pag1, height=2, width=20, wrap="word", undo=True)
            self.testo_stelle.pack(fill="x", pady=(4, 0))
            self.testo_stelle.insert("1.0", self.cfg.get("ultime_stelle", "KIC 757137"))
            comandi = RigaACapo(pag1, spazio=6)
            comandi.pack(fill="x", pady=(4, 0))
            self.riga_comandi = comandi
            esempi = ttk.Menubutton(comandi, text="Esempi")
            menu = tk.Menu(esempi, tearoff=False)
            for etichetta, valore in ESEMPI:
                menu.add_command(label=etichetta, command=lambda v=valore: self._aggiungi_esempio(v))
            esempi["menu"] = menu
            self.menu_esempi = menu
            self.b_analizza = ttk.Button(comandi, text="Analizza", command=self.avvia, style="Attivo.TButton")
            self.b_ferma = ttk.Button(comandi, text="Ferma", command=self.interrompi, state="disabled")
            self.b_trova = ttk.Button(comandi, text="Trova stelle…", command=self.trova_stelle)
            for w_ in (self.b_analizza, self.b_ferma, self.b_trova, esempi):
                comandi.aggiungi(w_)

            opz = ttk.LabelFrame(pag1, text="Opzioni", padding=6)
            opz.pack(fill="x", pady=(6, 0))
            self.opz = opz
            # ogni opzione (etichetta + comando) sta in un suo riquadro; i riquadri vanno a capo
            riga_opz = RigaACapo(opz, spazio=14)
            riga_opz.pack(fill="x")
            self.riga_opz = riga_opz

            def opzione(etichetta, crea):
                f = ttk.Frame(riga_opz)
                if etichetta:
                    ttk.Label(f, text=etichetta).pack(side="left", padx=(0, 4))
                w = crea(f)
                w.pack(side="left")
                if isinstance(w, ttk.Combobox):
                    w.bind("<<ComboboxSelected>>", self._combo_cambiata)
                riga_opz.aggiungi(f)
                return w

            opzione("Missione:", lambda f: ttk.Combobox(f, textvariable=self.missione, values=list(MISSIONI),
                                                        state="readonly", width=largo(MISSIONI)))
            opzione("Cadenza:", lambda f: ttk.Combobox(f, textvariable=self.cadenza, values=list(CADENZE),
                                                       state="readonly", width=largo(CADENZE)))
            opzione("Teff a mano (K):", lambda f: ttk.Entry(f, textvariable=self.teff, width=7))
            opzione("[Fe/H] a mano:", lambda f: ttk.Entry(f, textvariable=self.feh, width=6))
            opzione(None, lambda f: ttk.Checkbutton(f, text="Età con MIST", variable=self.con_eta))
            opzione(None, lambda f: ttk.Checkbutton(f, text="Suono della stella", variable=self.con_suono))
            opzione("Dimensione testo:", lambda f: ttk.Combobox(f, textvariable=self.scala, values=SCALE,
                                                                state="readonly", width=largo(SCALE)))

            ttk.Label(pag1, text="Avanzamento e risultati").pack(anchor="w", pady=(6, 0))
            fr = ttk.Frame(pag1)
            fr.pack(fill="both", expand=True)
            self.registro = tk.Text(fr, wrap="word", state="disabled", width=20, height=4)
            sb = ttk.Scrollbar(fr, command=self.registro.yview)
            self.registro.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self.registro.pack(side="left", fill="both", expand=True)

            # scheda dei grafici
            testa = ttk.Frame(pag2)
            testa.pack(fill="x")
            ttk.Label(testa, text="Stella:").pack(side="left")
            self.c_scelta = ttk.Combobox(testa, textvariable=self.scelta, state="readonly", values=[])
            self.c_scelta.pack(side="left", fill="x", expand=True, padx=4)
            self.c_scelta.bind("<<ComboboxSelected>>", lambda e: self.mostra(self.scelta.get()))
            scelta_g = RigaACapo(pag2, spazio=4)
            scelta_g.pack(fill="x", pady=(4, 0))
            self.riga_grafici = scelta_g
            self.pulsanti_quale = {}
            for chiave, testo in GRAFICI:
                p = ttk.Button(scelta_g, text=testo, command=lambda k=chiave: self._scegli_grafico(k))
                scelta_g.aggiungi(p)
                self.pulsanti_quale[chiave] = p
            self.figura = Figure(figsize=(7, 5), dpi=100)
            self.tela = FigureCanvasTkAgg(self.figura, master=pag2)
            # la barra di matplotlib (icone minuscole) non si mostra: la comandano pulsanti italiani
            self.barra = NavigationToolbar2Tk(self.tela, pag2, pack_toolbar=False)
            self.barra.update()
            strumenti = RigaACapo(pag2, spazio=4)
            strumenti.pack(side="bottom", fill="x", pady=(4, 0))
            self.pulsanti_grafico = {}
            for chiave, testo in (("home", "Vista iniziale"), ("back", "Indietro"), ("forward", "Avanti"),
                                  ("pan", "Sposta"), ("zoom", "Ingrandisci un riquadro"), ("save", "Salva immagine…")):
                p = ttk.Button(strumenti, text=testo, command=lambda k=chiave: self._strumento(k))
                strumenti.aggiungi(p)
                self.pulsanti_grafico[chiave] = p
            self.riga_strumenti = strumenti
            self.l_strumento = EtichettaACapo(pag2, text="")
            self.l_strumento.pack(side="bottom", fill="x")
            # richiesta piccola: il grafico si allarga solo con lo spazio che c'è (non spinge fuori la finestra)
            self.tela.get_tk_widget().configure(width=300, height=200)
            self.tela.get_tk_widget().pack(fill="both", expand=True, pady=(4, 0))
            self._evidenzia_grafico()
            self._figura_vuota()
            self._aggiorna_cartella()

        def _scegli_grafico(self, k):
            self.quale = k
            self._evidenzia_grafico()
            if self.attuale:
                self.mostra(self.attuale)

        def _evidenzia_grafico(self):
            for k, p in self.pulsanti_quale.items():
                p.configure(style="Attivo.TButton" if k == self.quale else "TButton")

        def _disponi_opzioni(self):
            """Rifà la disposizione delle opzioni (dopo un cambio della dimensione del testo)."""
            self.riga_opz._ultima = None
            self.riga_opz.disponi()

        def _strumento(self, k):
            tb = self.barra
            {"home": tb.home, "back": tb.back, "forward": tb.forward, "pan": tb.pan, "zoom": tb.zoom,
             "save": tb.save_figure}[k]()
            modo = str(getattr(tb.mode, "name", tb.mode) or "").upper()
            for chiave, nome in (("pan", "PAN"), ("zoom", "ZOOM")):
                self.pulsanti_grafico[chiave].configure(style="Attivo.TButton" if modo == nome else "TButton")
            self.l_strumento.configure(text={"PAN": "Sposta attivo: trascina il grafico; ripremi «Sposta» per smettere.",
                                             "ZOOM": "Ingrandisci attivo: disegna un riquadro sul grafico; ripremi per smettere."}
                                       .get(modo, ""))

        def _combo_cambiata(self, _e=None):
            self.applica_scala()

        def trova_stelle(self):
            if self.finestra_trova is not None and self.finestra_trova.winfo_exists():
                self.finestra_trova.lift()
                return
            self.finestra_trova = FinestraTrova(self)

        def _aggiungi_esempio(self, valore):
            attuale = self.testo_stelle.get("1.0", "end").strip()
            righe = [r for r in attuale.splitlines() if r.strip()]
            if valore not in righe:
                righe.append(valore)
            self.testo_stelle.delete("1.0", "end")
            self.testo_stelle.insert("1.0", "\n".join(righe))

        def _aggiorna_cartella(self):
            self.l_cartella.configure(text=f"Risultati in: {self.cartella}")

        # ------------------------------------------------------------ dimensione del testo
        def applica_scala(self):
            try:
                s = int(self.scala.get().split()[0]) / 100.0
            except (ValueError, IndexError):
                s = 1.5
            base = max(8, round(10 * s))
            for nome in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                         "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont", "TkFixedFont"):
                try:
                    tkfont.nametofont(nome).configure(size=base)
                except tk.TclError:
                    pass
            self.testo_stelle.configure(font="TkTextFont")
            self.registro.configure(font="TkTextFont")
            try:
                riga = int(tkfont.nametofont("TkDefaultFont").metrics("linespace") or round(16 * s))
            except (tk.TclError, TypeError, ValueError):
                riga = round(16 * s)
            p = lambda v: max(1, round(v * s))
            self.stile.configure("TButton", padding=(p(8), p(4)))
            self.stile.configure("Attivo.TButton", padding=(p(8), p(4)), background="#f0b050")
            self.stile.configure("TCombobox", padding=p(3), arrowsize=max(12, int(riga * 0.9)))
            self.stile.configure("TMenubutton", padding=(p(8), p(4)), arrowsize=max(8, int(riga * 0.5)))
            self.stile.configure("TCheckbutton", padding=p(2), indicatorsize=max(12, int(riga * 0.8)))
            self.stile.configure("TEntry", padding=p(3))
            for orient in ("Vertical", "Horizontal"):
                self.stile.configure(f"{orient}.TScrollbar", arrowsize=max(14, int(riga * 1.1)), width=max(14, int(riga * 1.1)))
            self.stile.configure("TProgressbar", thickness=max(10, int(riga * 0.8)))
            self.stile.configure("TNotebook.Tab", padding=(p(12), p(5)))
            self.stile.configure("Treeview", rowheight=int(riga * 1.35), font="TkDefaultFont")
            self.stile.configure("Treeview.Heading", font="TkDefaultFont", padding=p(3))
            self.option_add("*TCombobox*Listbox.font", "TkTextFont")
            try:
                self.menu_esempi.configure(font="TkMenuFont")
            except tk.TclError:
                pass
            # grafico: dpi proporzionale (testi, linee e margini crescono insieme)
            # grafico: i dpi li adatta matplotlib alla densità dello schermo (su Tk lo fa da solo e
            # li reimposta), quindi si fanno crescere i caratteri con la dimensione del testo
            matplotlib.rcParams["font.size"] = 7.5 * s
            matplotlib.rcParams["lines.linewidth"] = 1.0 * s ** 0.5
            self.after(50, self._disponi_opzioni)
            for r in (getattr(self, "riga_strumenti", None), getattr(self, "riga_pulsanti", None),
                      getattr(self, "riga_grafici", None), getattr(self, "riga_comandi", None)):
                if r is not None:
                    r._ultima = None
                    self.after(60, r.disponi)
            if self.attuale:
                self.mostra(self.attuale)
            else:
                self._figura_vuota()

        # ------------------------------------------------------------ registro e stato
        def scrivi(self, testo):
            self.registro.configure(state="normal")
            self.registro.insert("end", testo + "\n")
            self.registro.see("end")
            self.registro.configure(state="disabled")

        def _leggi_coda(self):
            try:
                while True:
                    tipo, dato = self.coda.get_nowait()
                    if tipo == "log":
                        self.scrivi(dato)
                    elif tipo == "stato":
                        self.stato.set(dato)
                    elif tipo == "avanz":
                        a, b, testo = dato
                        self.barra_av.configure(maximum=max(b, 1), value=min(a, b))
                        if testo:
                            self.stato.set(testo)
                    elif tipo == "risultato":
                        self._nuovo_risultato(dato)
                    elif tipo == "fine":
                        self._fine(dato)
            except queue.Empty:
                pass
            self.after(120, self._leggi_coda)

        # ------------------------------------------------------------ analisi
        def _opzioni(self):
            teff = numero_utente(self.teff.get())
            feh = numero_utente(self.feh.get())
            if teff is not None and not (2500 <= teff <= 12000):
                raise ValueError("la temperatura deve stare tra 2500 e 12000 K")
            if feh is not None and not (-3 <= feh <= 1):
                raise ValueError("[Fe/H] deve stare tra −3 e +1")
            return {"missione": MISSIONI.get(self.missione.get(), "auto"),
                    "cadenza": CADENZE.get(self.cadenza.get(), "auto"),
                    "teff": teff, "feh": feh, "eta": self.con_eta.get(), "suono": self.con_suono.get(),
                    "cartella": self.cartella}

        def avvia(self):
            if self.lavoro and self.lavoro.is_alive():
                return
            righe = [r.strip() for r in self.testo_stelle.get("1.0", "end").splitlines()
                     if r.strip() and not r.strip().startswith("#")]
            if not righe:
                messaggio(self, "Nessuna stella", "Scrivi almeno una stella (o scegli un esempio dal menu «Esempi»).")
                return
            try:
                opz = self._opzioni()
            except ValueError as e:
                messaggio(self, "Opzioni", f"Valore non valido: {e}.")
                return
            self._salva()
            self.ferma.clear()
            self.b_analizza.configure(state="disabled")
            self.b_ferma.configure(state="normal")
            self.barra_av.configure(value=0)
            self.lavoro = threading.Thread(target=self._lavora, args=(righe, opz), daemon=True)
            self.lavoro.start()

        def _lavora(self, righe, opz):
            fatti, errori = 0, 0
            for i, r in enumerate(righe):
                if self.ferma.is_set():
                    break
                self.coda.put(("log", f"\n══════ {i + 1}/{len(righe)}: {r} ══════"))
                self.coda.put(("stato", f"Analizzo {r} ({i + 1} di {len(righe)})…"))
                try:
                    ris = M.analizza_stella(
                        r, opz, log=lambda s: self.coda.put(("log", s)),
                        avanzamento=lambda a, b, t="": self.coda.put(("avanz", (a, b, t))), ferma=self.ferma)
                    self.coda.put(("risultato", ris))
                    fatti += 1
                except M.Interrotto:
                    self.coda.put(("log", "Interrotto."))
                    break
                except Exception as e:
                    errori += 1
                    self.coda.put(("log", f"ERRORE con «{r}»: {type(e).__name__}: {e}"))
                    self.coda.put(("log", "".join(traceback.format_exception(e)[-3:]).rstrip()))
            self.coda.put(("fine", (fatti, errori, len(righe))))

        def _nuovo_risultato(self, ris):
            nome = f"{ris['nome']}  ({ris['chiave']})"
            self.risultati[nome] = ris
            if nome in self.ordine:
                self.ordine.remove(nome)
            self.ordine.append(nome)
            # tiene in memoria al massimo 15 stelle con i dati completi
            while len(self.ordine) > 15:
                vecchio = self.ordine.pop(0)
                self.risultati.pop(vecchio, None)
            self.c_scelta.configure(values=self.ordine)
            self.scelta.set(nome)
            self.schede.select(self.pag_grafici)
            self.mostra(nome)
            self.scrivi("— Riassunto (come nella scheda) —")
            for r in M.testo_risultati_inglese(ris):
                self.scrivi("   " + r)

        def _fine(self, dato):
            fatti, errori, tot = dato
            self.b_analizza.configure(state="normal")
            self.b_ferma.configure(state="disabled")
            self.barra_av.configure(maximum=1, value=1 if fatti else 0)
            self.stato.set(f"Finito: {fatti} stelle analizzate su {tot}" + (f", {errori} con errori" if errori else "")
                           + f". Risultati in {self.cartella}")

        def interrompi(self):
            self.ferma.set()
            self.stato.set("Interruzione richiesta: mi fermo appena possibile…")

        # ------------------------------------------------------------ grafici
        def _figura_vuota(self):
            self.figura.clear()
            a = self.figura.add_subplot(111)
            a.text(0.5, 0.5, "Qui compariranno curva di luce, spettro,\nmodi di oscillazione e diagramma échelle",
                   ha="center", va="center", transform=a.transAxes, color="#555555")
            a.set_axis_off()
            self.tela.draw_idle()

        def mostra(self, nome):
            ris = self.risultati.get(nome)
            if not ris:
                return
            self.attuale = nome
            try:
                if self.quale == "tutti":
                    M.disegna_pannelli(self.figura, ris, compatto=True)
                else:
                    M.disegna_pannelli(self.figura, ris, solo=self.quale)
                self.figura.suptitle(ris["nome"], fontweight="bold")
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)     # finestra piccola: niente avvisi
                    self.figura.tight_layout(h_pad=1.6, w_pad=1.2)
            except Exception as e:
                self.scrivi(f"(grafico non disegnato: {e})")
            self.tela.draw_idle()
            self.b_ascolta.configure(state="normal" if ris.get("wav") else "disabled")
            self.b_modi.configure(state="normal" if ris.get("wav_modi") else "disabled")
            self.b_pdf.configure(state="normal" if ris.get("pdf") else "disabled")

        # ------------------------------------------------------------ pulsanti
        def ascolta(self, chiave="wav"):
            """Suona il WAV della stella («wav» = curva di luce accelerata, «wav_modi» = accordo dei modi);
            premendo di nuovo lo stesso pulsante il suono si ferma."""
            ris = self.risultati.get(self.attuale or "")
            pulsante = self.b_modi if chiave == "wav_modi" else self.b_ascolta
            testo = "Ascolta i modi" if chiave == "wav_modi" else "Ascolta la stella"
            if self.suonando and self.suonando.poll() is None:
                self.suonando.terminate()
                fermato = self.suonando_chiave
                self.suonando = None
                self._rimetti_testi_suono()
                if fermato == chiave:
                    return
            if not ris or not ris.get(chiave):
                return
            for prog in ("paplay", "pw-play", "aplay"):
                if shutil.which(prog):
                    self.suonando = subprocess.Popen([prog, str(ris[chiave])], env=ambiente_esterno(),
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.suonando_chiave = chiave
                    pulsante.configure(text="Ferma il suono")
                    self.after(500, self._controlla_suono)
                    if chiave == "wav":
                        so = ris.get("suono") or {}
                        if so:
                            self.stato.set(f"Suono della stella: la curva di luce vera accelerata {so['k']:.3g} volte; "
                                           f"νmax diventa una nota di {so['nota_hz']:.0f} Hz ({so['durata_s']:.0f} s). "
                                           f"Le oscillazioni sono eccitate a caso dalla convezione: si sente un fruscio "
                                           f"che pulsa, non note pulite.")
                    else:
                        sm = ris.get("suono_modi") or {}
                        if sm:
                            self.stato.set(f"Accordo dei modi: {sm['n_modi']} modi trovati nello spettro, suonati come "
                                           f"note pulite da {sm['da_hz']:.0f} a {sm['a_hz']:.0f} Hz, con gli stessi "
                                           f"rapporti di frequenza della stella ({sm['durata_s']:.0f} s).")
                    return
            apri_esterno(ris[chiave])
            del testo

        def _rimetti_testi_suono(self):
            self.b_ascolta.configure(text="Ascolta la stella")
            self.b_modi.configure(text="Ascolta i modi")

        def _controlla_suono(self):
            if self.suonando and self.suonando.poll() is None:
                self.after(500, self._controlla_suono)
            else:
                self._rimetti_testi_suono()

        def apri_pdf(self):
            ris = self.risultati.get(self.attuale or "")
            if ris and ris.get("pdf"):
                apri_esterno(ris["pdf"])

        def apri_cartella(self):
            self.cartella.mkdir(parents=True, exist_ok=True)
            ris = self.risultati.get(self.attuale or "")
            apri_esterno(ris["cartella"] if ris and ris.get("cartella") else self.cartella)

        def cambia_cartella(self):
            nuova = None
            if shutil.which("zenity") and not sys.platform.startswith("win"):
                try:
                    r = subprocess.run(["zenity", "--file-selection", "--directory",
                                        "--title=Cartella per i risultati", f"--filename={self.cartella}/"],
                                       capture_output=True, text=True, env=ambiente_esterno(), timeout=600)
                    if r.returncode == 0 and r.stdout.strip():
                        nuova = r.stdout.strip()
                    elif r.returncode == 1:
                        return
                except Exception:
                    nuova = None
            if nuova is None:
                from tkinter import filedialog
                nuova = filedialog.askdirectory(parent=self, initialdir=str(self.cartella), title="Cartella per i risultati")
            if nuova:
                self.cartella = Path(nuova)
                self._aggiorna_cartella()
                self._salva()

        def guida(self):
            testo = None
            for p in (QUI / "LEGGIMI.txt", Path(__file__).resolve().parent / "LEGGIMI.txt"):
                if p.exists():
                    testo = p.read_text(encoding="utf-8")
                    break
            messaggio(self, "Guida", testo or __doc__, larga=True)

        def _salva(self):
            self.cfg.update({"scala": self.scala.get(), "missione": self.missione.get(), "cadenza": self.cadenza.get(),
                             "eta": self.con_eta.get(), "suono": self.con_suono.get(), "cartella": str(self.cartella),
                             "grafico": self.quale,
                             "ultime_stelle": self.testo_stelle.get("1.0", "end").strip()})
            salva_config(self.cfg)

        def chiudi(self):
            if self.lavoro and self.lavoro.is_alive():
                self.ferma.set()
            if self.suonando and self.suonando.poll() is None:
                self.suonando.terminate()
            self._salva()
            self.destroy()

    FONTI = [("Giganti rosse di Kepler (Yu et al. 2018, circa 16 000)", "yu"),
             ("Giganti di Kepler con l'età (APOKASC-3)", "apokasc3"),
             ("Stelle famose (tipo Sole, con pianeti, TESS)", "famose")]
    FASI = [("Tutte", None), ("Ramo delle giganti (RGB)", "RGB"), ("Red clump (bruciano elio)", "RC")]
    ORDINI = [("A caso", "caso"), ("Prima le più brillanti", "brillanti"), ("νmax crescente", "numax"),
              ("Prima le più vecchie", "vecchie"), ("Prima le più giovani", "giovani")]

    class FinestraTrova(tk.Toplevel):
        """Sceglie stelle da analizzare dai cataloghi (Yu+2018, APOKASC-3) o da un elenco di stelle famose."""
        ELENCO_FONTI = FONTI

        def __init__(self, app):
            super().__init__(app)
            self.app = app
            self.title("Trova stelle")
            self.transient(app)
            self.coda = queue.Queue()
            self.trovate = []
            self.fonte = tk.StringVar(value=FONTI[0][0])
            self.fase = tk.StringVar(value=FASI[0][0])
            self.nm_min = tk.StringVar(value="")
            self.nm_max = tk.StringVar(value="")
            self.mag_max = tk.StringVar(value="")
            self.eta_min = tk.StringVar(value="")
            self.eta_max = tk.StringVar(value="")
            self.quante = tk.StringVar(value="10")
            self.ordine = tk.StringVar(value=ORDINI[0][0])
            self.stato = tk.StringVar(value="Scegli da dove pescare e i filtri (tutti facoltativi), poi premi «Cerca».")
            fr = ttk.Frame(self, padding=10)
            fr.pack(fill="both", expand=True)
            basso = ttk.Frame(fr)
            basso.pack(side="bottom", fill="x", pady=(6, 0))
            EtichettaACapo(basso, textvariable=self.stato).pack(fill="x")
            azioni = RigaACapo(basso)
            azioni.pack(fill="x", pady=(4, 0))
            self.riga_azioni = azioni
            for testo, cmd in (("Aggiungi le stelle selezionate", self.aggiungi_scelte),
                               ("Aggiungi tutte", self.aggiungi_tutte), ("Chiudi", self.destroy)):
                azioni.aggiungi(ttk.Button(azioni, text=testo, command=cmd))

            riga1 = ttk.Frame(fr)
            riga1.pack(fill="x")
            ttk.Label(riga1, text="Da dove:").pack(side="left", padx=(0, 4))
            c = ttk.Combobox(riga1, textvariable=self.fonte, values=[f for f, _ in FONTI], state="readonly", width=largo(f for f, _ in FONTI))
            c.pack(side="left", fill="x", expand=True)
            c.bind("<<ComboboxSelected>>", lambda e: self._aggiorna_filtri())
            filtri = RigaACapo(fr, spazio=14)
            filtri.pack(fill="x", pady=(6, 0))
            self.riga_filtri = filtri
            self.blocchi = {}

            def blocco(chiave, etichetta, crea):
                f = ttk.Frame(filtri)
                ttk.Label(f, text=etichetta).pack(side="left", padx=(0, 4))
                for w in crea(f):
                    w.pack(side="left", padx=(0, 4))
                filtri.aggiungi(f)
                self.blocchi[chiave] = f

            blocco("fase", "Fase:", lambda f: [ttk.Combobox(f, textvariable=self.fase, values=[x for x, _ in FASI],
                                                            state="readonly", width=largo(x for x, _ in FASI))])
            blocco("numax", "νmax (µHz) da", lambda f: [ttk.Entry(f, textvariable=self.nm_min, width=6),
                                                        ttk.Label(f, text="a"),
                                                        ttk.Entry(f, textvariable=self.nm_max, width=6)])
            blocco("mag", "Più brillanti di magnitudine", lambda f: [ttk.Entry(f, textvariable=self.mag_max, width=5)])
            blocco("eta", "Età (miliardi di anni) da", lambda f: [ttk.Entry(f, textvariable=self.eta_min, width=5),
                                                                  ttk.Label(f, text="a"),
                                                                  ttk.Entry(f, textvariable=self.eta_max, width=5)])
            blocco("quante", "Quante:", lambda f: [ttk.Combobox(f, textvariable=self.quante,
                                                                values=["5", "10", "20", "50", "100"], width=5)])
            blocco("ordine", "Ordine:", lambda f: [ttk.Combobox(f, textvariable=self.ordine,
                                                                values=[x for x, _ in ORDINI], state="readonly",
                                                                width=largo(x for x, _ in ORDINI))])
            self.b_cerca = ttk.Button(fr, text="Cerca", command=self.cerca, style="Attivo.TButton")
            self.b_cerca.pack(anchor="w", pady=(6, 4))

            elenco = ttk.Frame(fr)
            elenco.pack(fill="both", expand=True)
            colonne = ("stella", "numax", "dnu", "fase", "mag", "massa", "eta", "nota")
            self.albero = ttk.Treeview(elenco, columns=colonne, show="headings", selectmode="extended", height=6)
            self.titoli = (("stella", "Stella"), ("numax", "νmax (µHz)"), ("dnu", "Δν (µHz)"), ("fase", "Fase"),
                           ("mag", "Magnitudine"), ("massa", "Massa (M☉)"), ("eta", "Età (Gyr)"), ("nota", "Note"))
            for col, testo in self.titoli:
                self.albero.heading(col, text=testo)
                self.albero.column(col, stretch=(col == "nota"), anchor="w")
            self._larghezze_colonne()
            sb = ttk.Scrollbar(elenco, command=self.albero.yview)
            self.albero.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self.albero.pack(side="left", fill="both", expand=True)
            self.albero.bind("<Double-1>", lambda e: self.aggiungi_scelte())
            w, h = app.winfo_width(), app.winfo_height()
            self.geometry(f"{max(600, int(w * 0.8))}x{max(400, int(h * 0.8))}+{app.winfo_rootx() + int(w * 0.1)}"
                          f"+{app.winfo_rooty() + int(h * 0.08)}")
            self._aggiorna_filtri()
            self.after(100, self._leggi)

        def _larghezze_colonne(self):
            """Ogni colonna larga quanto il titolo o il valore più lungo, misurati con il carattere vero
            (che cresce con «Dimensione testo»); le note prendono lo spazio che avanza."""
            try:
                car = tkfont.nametofont("TkDefaultFont")
                misura = car.measure
                spazio = car.measure("0") * 2
            except (tk.TclError, AttributeError, TypeError):
                misura, spazio = (lambda t: 10 * len(t)), 20
            for i, (col, testo) in enumerate(self.titoli):
                valori = [testo] + [str(self.albero.item(r, "values")[i]) for r in self.albero.get_children()
                                    if len(self.albero.item(r, "values") or ()) > i]
                try:
                    if col == "nota":
                        minimo = int(misura(testo)) + spazio
                        larg = max(minimo, int(misura("0" * 24)) + spazio)
                    else:
                        larg = minimo = max(int(misura(v)) for v in valori) + spazio
                except (TypeError, ValueError):
                    larg = minimo = 120
                self.albero.column(col, width=larg, minwidth=minimo)

        def _aggiorna_filtri(self):
            fonte = dict(FONTI).get(self.fonte.get())
            visibili = {"yu": ("fase", "numax", "mag", "quante", "ordine"),
                        "apokasc3": ("fase", "numax", "eta", "quante", "ordine"),
                        "famose": ()}[fonte]
            self.riga_filtri.elementi = [self.blocchi[k] for k in ("fase", "numax", "mag", "eta", "quante", "ordine")
                                         if k in visibili]
            for k, f in self.blocchi.items():
                if k not in visibili:
                    f.place_forget()
            self.riga_filtri._ultima = None
            self.riga_filtri.disponi()
            if fonte == "famose":
                self.cerca()

        def _numero(self, var, nome):
            t = var.get().strip().replace(",", ".")
            if not t:
                return None
            try:
                return float(t)
            except ValueError:
                raise ValueError(f"«{t}» non è un numero ({nome})")

        def cerca(self):
            fonte = dict(FONTI).get(self.fonte.get())
            try:
                filtri = {"fase": dict(FASI).get(self.fase.get()),
                          "numax_min": self._numero(self.nm_min, "νmax da"),
                          "numax_max": self._numero(self.nm_max, "νmax a"),
                          "mag_max": self._numero(self.mag_max, "magnitudine") if fonte == "yu" else None,
                          "eta_min": self._numero(self.eta_min, "età da") if fonte == "apokasc3" else None,
                          "eta_max": self._numero(self.eta_max, "età a") if fonte == "apokasc3" else None,
                          "quante": int(self._numero(self.quante, "quante") or 10),
                          "ordine": dict(ORDINI).get(self.ordine.get(), "caso")}
            except ValueError as e:
                self.stato.set(str(e))
                return
            self.b_cerca.configure(state="disabled")
            self.stato.set("Cerco…")

            def lavoro():
                try:
                    stelle, tot = M.trova_stelle(fonte, log=lambda s: self.coda.put(("stato", s)), **filtri)
                    self.coda.put(("fatto", (stelle, tot, fonte)))
                except Exception as e:
                    self.coda.put(("errore", f"{type(e).__name__}: {e}"))
            threading.Thread(target=lavoro, daemon=True).start()

        def _leggi(self):
            if not self.winfo_exists():
                return
            try:
                while True:
                    tipo, dato = self.coda.get_nowait()
                    if tipo == "stato":
                        self.stato.set(dato)
                    elif tipo == "errore":
                        self.b_cerca.configure(state="normal")
                        self.stato.set("Non riesco a leggere il catalogo: " + dato)
                    elif tipo == "fatto":
                        self.b_cerca.configure(state="normal")
                        self._mostra(*dato)
            except queue.Empty:
                pass
            self.after(150, self._leggi)

        def _mostra(self, stelle, tot, fonte):
            self.trovate = stelle
            self.albero.delete(*self.albero.get_children())

            def f(v, c=2):
                return "" if v is None else f"{v:.{c}f}"
            nomi_fase = {"RGB": "RGB", "RC": "clump"}
            for i, s_ in enumerate(stelle):
                self.albero.insert("", "end", iid=str(i), values=(
                    s_["nome"], f(s_.get("numax")), f(s_.get("dnu"), 3), nomi_fase.get(s_.get("fase"), ""),
                    f(s_.get("mag"), 1), f(s_.get("massa")), f(s_.get("eta")), s_.get("nota", "")))
            self._larghezze_colonne()
            if fonte == "famose":
                self.stato.set("Stelle famose: selezionane una o più (anche tenendo premuto Ctrl) e premi "
                               "«Aggiungi le stelle selezionate», oppure fai doppio clic su una stella.")
            else:
                self.stato.set(f"{tot} stelle del catalogo rispettano i filtri; qui ne vedi {len(stelle)}. "
                               f"Selezionale (Ctrl o Maiuscolo per più di una) e premi «Aggiungi», oppure "
                               f"doppio clic su una stella.")

        def _aggiungi(self, indici):
            n = 0
            for i in indici:
                self.app._aggiungi_esempio(self.trovate[int(i)]["testo"])
                n += 1
            if n:
                self.stato.set(f"Aggiunte {n} stelle alla casella della finestra principale: premi «Analizza».")
                self.app.schede.select(self.app.pag_stelle)

        def aggiungi_scelte(self):
            self._aggiungi(self.albero.selection())

        def aggiungi_tutte(self):
            self._aggiungi(self.albero.get_children())

    def messaggio(padre, titolo, testo, larga=False):
        """Finestra di messaggio ridimensionabile con il testo che va a capo."""
        w = tk.Toplevel(padre)
        w.title(titolo)
        w.transient(padre)
        fr = ttk.Frame(w, padding=10)
        fr.pack(fill="both", expand=True)
        t = tk.Text(fr, wrap="word", height=22 if larga else 6, width=90 if larga else 60, font="TkTextFont",
                    relief="flat", background=padre.cget("background") if not larga else "white")
        sb = ttk.Scrollbar(fr, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.insert("1.0", testo)
        t.configure(state="disabled")
        ttk.Button(fr, text="OK", command=w.destroy).pack(side="bottom", pady=(8, 0))
        if larga:
            sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        w.bind("<Escape>", lambda e: w.destroy())
        return w

    return App, messaggio


def avvia_finestra():
    import tkinter as tk
    from tkinter import ttk, font as tkfont
    App, _ = crea_app(tk, ttk, tkfont)
    app = App()
    app.mainloop()


# =============================================================================
# RIGA DI COMANDO
# =============================================================================
def analizza_da_terminale(argomenti):
    opz = {"missione": "auto", "cadenza": "auto", "teff": None, "feh": None, "eta": True, "suono": True}
    stelle = []
    i = 0
    while i < len(argomenti):
        a = argomenti[i]
        if a == "--missione":
            opz["missione"] = argomenti[i + 1]
            i += 1
        elif a == "--cadenza":
            opz["cadenza"] = argomenti[i + 1]
            i += 1
        elif a == "--teff":
            opz["teff"] = numero_utente(argomenti[i + 1])
            i += 1
        elif a == "--feh":
            opz["feh"] = numero_utente(argomenti[i + 1])
            i += 1
        elif a == "--cartella":
            opz["cartella"] = Path(argomenti[i + 1]).expanduser()
            i += 1
        elif a == "--senza-eta":
            opz["eta"] = False
        elif a == "--senza-suono":
            opz["suono"] = False
        else:
            stelle.append(a)
        i += 1
    errori = 0
    for s in stelle:
        print(f"\n══════ {s} ══════", flush=True)
        try:
            ris = M.analizza_stella(s, opz, log=lambda t: print(t, flush=True))
            print("— Summary —")
            for r in M.testo_risultati_inglese(ris):
                print("   " + r)
        except Exception as e:
            errori += 1
            print(f"ERRORE: {type(e).__name__}: {e}")
            traceback.print_exc()
    return 1 if errori else 0


def diagnostica():
    """Prova gli archivi in rete con KIC 757137 (gigante di Yu+2018: νmax 29,99, Δν 3,399)."""
    righe = []

    def scrivi(t):
        print(t, flush=True)
        righe.append(t)

    scrivi(f"{TITOLO} – diagnostica di rete – {time.strftime('%d/%m/%Y %H:%M:%S')}")
    prove = []

    def prova(nome, f):
        t0 = time.time()
        try:
            nota = f()
            prove.append(True)
            scrivi(f"[ok] {nome} ({time.time() - t0:.1f} s) {nota or ''}")
        except Exception as e:
            prove.append(False)
            scrivi(f"[ERRORE] {nome} ({time.time() - t0:.1f} s) {type(e).__name__}: {e}")

    info = {}

    def tic():
        info.update(M.risolvi_stella("KIC 757137", log=scrivi))
        return f"TIC {info.get('tic')}, Teff {info['riga_tic'].get('Teff')}"
    prova("MAST: catalogo TIC (KIC 757137)", tic)
    prova("MAST: risolutore dei nomi (16 Cyg A)", lambda: "RA %.4f Dec %.4f" % M.risolvi_nome("16 Cyg A"))

    elenco = []

    def kep():
        elenco.extend(M.elenca_curve(info, "kepler", "lunga", log=scrivi))
        if not elenco:
            raise RuntimeError("nessun file")
        return f"{len(elenco)} file"
    prova("MAST: elenco curve Kepler", kep)

    def scar():
        p = M.scarica(elenco[0])
        return f"{p.name} {p.stat().st_size} byte"
    prova("MAST: scaricamento di un file", scar)
    def cataloghi():
        c = M.cerca_cataloghi(757137, log=scrivi)
        if not c:
            raise RuntimeError("nessun catalogo")
        return ", ".join(f"{k}: νmax {v.get('numax')} Δν {v.get('dnu')}" for k, v in c.items())
    prova("VizieR Harvard: APOKASC-3/APOKASC-2/Yu+2018", cataloghi)

    def catalogo():
        righe = M.scarica_catalogo("yu", log=scrivi)
        return f"{len(righe)} giganti rosse"
    prova("VizieR Harvard: catalogo intero Yu+2018 (per «Trova stelle»)", catalogo)

    def gaia():
        g = M.gaia_dr3(info, log=scrivi)
        if not g:
            raise RuntimeError("nessuna riga")
        return f"source_id {g.get('source_id')}, Teff GSP-Phot {g.get('teff_gspphot')}"
    prova("Gaia DR3: archivio ESA", gaia)

    def mist():
        b = M.richiesta(M.MIST_URL.format(feh="p0.00"), intestazioni={"Range": "bytes=0-1023"}, tempo=60)
        return f"{len(b)} byte ricevuti"
    prova("MIST: tracce evolutive", mist)
    ok = sum(prove)
    scrivi(f"\n{ok}/{len(prove)} prove riuscite." + ("  TUTTO OK" if ok == len(prove) else ""))
    try:
        Path("diagnostica.txt").write_text("\n".join(righe) + "\n", encoding="utf-8")
        print(f"Scritto {Path('diagnostica.txt').resolve()}")
    except OSError:
        pass
    return 0 if ok == len(prove) else 1


# =============================================================================
# AUTOTEST (senza rete)
# =============================================================================
def autotest(con_finestra=True):
    import numpy as np
    import prova_sintetica as P
    esiti = []

    def controlla(nome, cond, nota=""):
        esiti.append(bool(cond))
        print(f"[{'ok' if cond else 'ERRORE'}] {nome} {nota}", flush=True)

    def prova(nome, f):
        try:
            f()
        except Exception as e:
            esiti.append(False)
            print(f"[ERRORE] {nome}: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()

    print(f"{TITOLO} – autotest – Python {sys.version.split()[0]}", flush=True)
    tmp = Path(tempfile.mkdtemp(prefix="battito_autotest_"))
    M.CACHE = tmp / "cache"

    # niente rete durante l'autotest
    def senza_rete(*a, **k):
        raise OSError("rete disattivata nell'autotest")
    vecchia_richiesta, vecchio_urlopen = M.richiesta, M.urllib.request.urlopen
    M.richiesta = senza_rete
    M.urllib.request.urlopen = senza_rete

    def t_nomi():
        c = [("KIC 757137", "kic", 757137), ("kplr000757137", "kic", 757137), ("TIC 261136679", "tic", 261136679),
             ("291.05 36.56", "coord", (291.05, 36.56)), ("16 Cyg A", "nome", "16 Cyg A")]
        for testo, tipo, val in c:
            x = M.interpreta_testo(testo)
            controlla(f"nome «{testo}»", x["tipo"] == tipo and x["valore"] == val, str(x))
    prova("nomi", t_nomi)

    def t_scala():
        s = M.relazioni_scala(M.NUMAX_SOLE, 30, M.DNU_SOLE, 0.1, M.TEFF_SOLE, 50)
        controlla("relazioni di scala sul Sole", abs(s["M"] - 1) < 1e-9 and abs(s["R"] - 1) < 1e-9
                  and abs(s["logg"] - M.LOGG_SOLE) < 1e-9)
    prova("scala", t_scala)

    def t_spettro():
        rng = np.random.default_rng(0)
        dt = 1765.46 / 86400
        t = np.arange(6000) * dt + 0.4 * dt * np.sin(np.arange(6000) / 300.0)
        t = t[rng.random(6000) > 0.1]
        x = 100 * np.sin(2 * np.pi * (250e-6 * 86400) * t) + rng.normal(0, 30, t.size)
        f, p, nyq, ris = M.spettro_potenza(t, x, dt)
        err = 0
        for nu in (100.0, 250.0, 270.0):
            i = np.argmin(abs(f - nu))
            fd = f[i] * 0.0864
            X = np.sum(x * np.exp(-2j * np.pi * fd * t))
            pd = 2 * abs(X) ** 2 * dt * 86400 / x.size * 1e-6
            err = max(err, abs(p[i] / pd - 1))
        controlla("spettro = DFT esatta sui tempi veri", err < 1e-6, f"(errore relativo {err:.1e})")
    prova("spettro", t_spettro)

    def t_pesi():
        rng = np.random.default_rng(2)
        t = np.arange(20000) * 60 / 86400
        x = rng.normal(0, 100, t.size)
        x[t < 3] *= 8                      # tre giorni molto rumorosi
        w = M.pesi_rumore(t, x, giorni=1.0)
        controlla("pesi del rumore (tratto rumoroso pesato meno)", np.mean(w[t < 3]) < 0.05 * np.mean(w[t > 4]))
    prova("pesi", t_pesi)

    def t_stelle():
        for nm, dn, dts, giorni, W in ((30, 3.4, 1765.46, 1460, 20), (8, 1.3, 1765.46, 1460, 20),
                                       (2200, 103, 58.85, 120, 2)):
            t, x, dt = P.curva_simulata(nm, dn, dt_s=dts, giorni=giorni, W=W)
            f, p, nyq, ris = M.spettro_potenza(t, x, dt)
            a = M.analizza_spettro(f, p, nyq, ris, sovr=2, n_mc=15)
            ok = a.get("rilevato") and abs(a["numax"] / nm - 1) < 0.10 and abs(a["dnu"] / dn - 1) < 0.02
            controlla(f"stella simulata νmax {nm}, Δν {dn}", ok,
                      f"→ νmax {a.get('numax', 0):.2f} ± {a.get('e_numax', 0):.2f}, Δν {a.get('dnu', 0):.3f} ± {a.get('e_dnu', 0):.3f}")
        rng = np.random.default_rng(5)
        t = np.arange(70000) * 1765.46 / 86400
        f, p, nyq, ris = M.spettro_potenza(t, rng.normal(0, 50, t.size), 1765.46 / 86400)
        a = M.analizza_spettro(f, p, nyq, ris, sovr=2, n_mc=5)
        controlla("solo rumore: nessuna oscillazione inventata", not a.get("rilevato"))
    prova("stelle simulate", t_stelle)

    def t_vizier():
        campione = ("#RESOURCE=yCat_2361\n#Name: J/ApJS/236/42\n#Title: Asteroseismology\n#Table\tJ_ApJS_236_42_giants:\n"
                    "#Name: J/ApJS/236/42/giants\n#Title: Red giants\n"
                    "KIC\tnumax\tDelnu\tTeff\n \tuHz\tuHz\tK\n--------\t------\t-----\t----\n"
                    "757137\t29.99\t3.399\t4751\n\n")
        tab = M.leggi_tsv_vizier(campione)
        r = tab[0][2][0] if tab and tab[0][2] else {}
        controlla("lettura delle tabelle VizieR", tab[0][0] == "J/ApJS/236/42/giants" and r.get("numax") == "29.99", str(r))
    prova("vizier", t_vizier)

    def t_trova():
        righe = [{"KIC": str(1000 + i), "numax": str(5 + i), "Delnu": "1", "Phase": str(i % 3),
                  "Kpmag": str(9 + i % 5), "NoCorM": "1.2", "Teff": "4800"} for i in range(200)]
        st = [M._stella_da_riga("yu", r) for r in righe]
        a, _ = M.filtra_stelle(st, fase="RC", numax_min=20, numax_max=80, mag_max=11, ordine="brillanti", quante=5)
        ok = len(a) == 5 and all(s_["fase"] == "RC" and 20 <= s_["numax"] <= 80 and s_["mag"] <= 11 for s_ in a)
        controlla("Trova stelle: filtri (fase, νmax, magnitudine)", ok, str([s_["testo"] for s_ in a]))
        f, n = M.trova_stelle("famose")
        controlla("Trova stelle: elenco delle stelle famose", n >= 10 and all(s_["testo"] for s_ in f))
    prova("trova", t_trova)

    def crea_mist_finta():
        import io as _io
        import tarfile as _tar
        intest = ("# MIST version number  = 1.2\n# EEPs: 1 202\n#    1     2    3    4    5    6\n"
                  "#  star_age  star_mass  log_Teff  log_g  log_R  phase\n")
        buf = _io.BytesIO()
        with _tar.open(fileobj=buf, mode="w:xz") as tf:
            for mi in np.arange(0.6, 4.01, 0.1):
                tms = 10e9 * mi ** -2.5
                age = np.concatenate([np.linspace(1e7, tms, 200), tms + np.linspace(1, 0.15 * tms, 300)])
                R = np.concatenate([np.linspace(0.9, 1.5, 200) * mi ** 0.8, np.geomspace(1.5 * mi ** 0.8, 150, 300)])
                T = np.minimum(np.concatenate([np.full(200, 5777 * mi ** 0.5), np.linspace(5777 * mi ** 0.5, 4000, 300)]), 9000)
                g = 4.438 + np.log10(mi / R ** 2)
                ph = np.concatenate([np.zeros(200), np.full(300, 2)])
                txt = intest + "\n".join(f"{a:.6e} {mi:.4f} {np.log10(tt):.5f} {gg:.5f} {np.log10(r):.5f} {pp:.0f}"
                                         for a, tt, gg, r, pp in zip(age, T, g, R, ph))
                b = txt.encode()
                ti = _tar.TarInfo(f"MIST_v1.2_feh_p0.00_afe_p0.0_vvcrit0.4_EEPS/{int(round(mi * 100)):05d}M.track.eep")
                ti.size = len(b)
                tf.addfile(ti, _io.BytesIO(b))
        d = M.CACHE / "mist"
        d.mkdir(parents=True, exist_ok=True)
        (d / "MIST_v1.2_feh_p0.00_afe_p0.0_vvcrit0.4_EEPS.txz").write_bytes(buf.getvalue())

    def t_mist():
        crea_mist_finta()
        g = M.griglia_mist(0.02, log=lambda s: None)
        controlla("tracce MIST: lettura del pacchetto .txz", g["m_ini"].size > 5000 and abs(g["feh_griglia"]) < 1e-9,
                  f"({g['m_ini'].size} punti)")
        # stella sulla griglia: 1,2 M☉ sul ramo delle giganti
        sel = (np.abs(g["m_ini"] - 1.2) < 1e-3) & (g["phase"] == 2)
        j = np.nonzero(sel)[0][150]
        T = 10 ** float(g["logT"][j])
        nm = M.NUMAX_SOLE * 10 ** (float(g["logg"][j]) - M.LOGG_SOLE) * (T / M.TEFF_SOLE) ** -0.5
        dn = M.DNU_SOLE * (float(g["mass"][j]) / (10 ** float(g["logR"][j])) ** 3) ** 0.5
        e = M.eta_da_griglia(g, nm, nm * 0.01, dn, dn * 0.005, T, 60)
        controlla("età dalla griglia (stella simulata di 1,2 M☉)", e and abs(e["massa"] - 1.2) < 0.15
                  and abs(e["eta"] - float(g["age"][j]) / 1e9) < 0.35 * float(g["age"][j]) / 1e9,
                  f"→ M {e['massa']:.2f}, età {e['eta']:.2f} Gyr (vera {float(g['age'][j]) / 1e9:.2f})" if e else "")
    prova("mist", t_mist)

    risultato = {}

    def t_completo():
        try:
            from astropy.io import fits
        except ImportError:
            controlla("analisi completa da file FITS (astropy assente: saltata)", True)
            return
        t, x, dt = P.curva_simulata(30, 3.4, giorni=1460)
        fl = P.flusso_kepler(t, x)
        d = tmp / "fits"
        d.mkdir()
        for i, j in enumerate(np.array_split(np.arange(t.size), 16)):
            h0 = fits.PrimaryHDU()
            h0.header.update({"TELESCOP": "Kepler", "OBJECT": "KIC 999", "KEPLERID": 999, "TEFF": 4750,
                              "LOGG": 2.5, "FEH": 0.0, "RADIUS": 12.0, "QUARTER": i})
            q = np.zeros(j.size, np.int32)
            q[::400] = 32
            cols = [fits.Column("TIME", "D", array=t[j]), fits.Column("PDCSAP_FLUX", "E", array=fl[j]),
                    fits.Column("SAP_QUALITY", "J", array=q), fits.Column("QUALITY", "J", array=q)]
            fits.HDUList([h0, fits.BinTableHDU.from_columns(cols, name="LIGHTCURVE")]).writeto(d / f"kplr000000999-q{i:02d}_llc.fits")
        c = M.leggi_curva(d / "kplr000000999-q03_llc.fits")
        controlla("lettura di una curva di luce FITS", c["t"].size > 1000 and c["info"]["teff"] == 4750)
        ris = M.analizza_stella(str(d), {"cartella": tmp / "risultati", "eta": True, "suono": True},
                                log=lambda s: None)
        an = ris["analisi"]
        controlla("analisi completa (curve FITS simulate)", an.get("rilevato") and abs(an["numax"] / 30 - 1) < 0.1
                  and abs(an["dnu"] / 3.4 - 1) < 0.02, f"→ νmax {an.get('numax', 0):.2f}, Δν {an.get('dnu', 0):.4f}")
        controlla("massa e raggio (Teff dall'intestazione FITS)", ris.get("scala") and 1.0 < ris["scala"]["M"] < 2.5)
        controlla("età MIST", ris.get("eta") is not None)
        pdf, png, wav = ris["pdf"], ris["png"], ris.get("wav")
        controlla("scheda PDF e PNG", pdf.stat().st_size > 30000 and png.stat().st_size > 30000,
                  f"({pdf.stat().st_size} e {png.stat().st_size} byte)")
        import wave as _w
        with _w.open(str(wav)) as w:
            durata = w.getnframes() / w.getframerate()
        controlla("suono WAV", 3 < durata <= 31, f"({durata:.1f} s)")
        controlla("accordo dei modi WAV", ris.get("wav_modi") is not None and ris["wav_modi"].stat().st_size > 100000,
                  str(ris.get("suono_modi")))
        controlla("CSV", (ris["cartella"] / f"{ris['chiave']}_results.csv").exists()
                  and (tmp / "risultati" / "all_results.csv").exists())
        risultato["ris"] = ris
    prova("analisi completa", t_completo)

    M.richiesta, M.urllib.request.urlopen = vecchia_richiesta, vecchio_urlopen

    def t_finestra():
        if not con_finestra:
            return
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or sys.platform.startswith("win")):
            controlla("finestra (nessuno schermo: saltata)", True)
            return
        import tkinter as tk
        from tkinter import ttk, font as tkfont
        global CONFIG
        vecchio = CONFIG
        CONFIG = tmp / "config.json"
        try:
            App, messaggio = crea_app(tk, ttk, tkfont)
            app = App()
            app.update()
            controlla("finestra aperta", app.winfo_exists(), f"(classe {app.winfo_class()})")
            controlla("classe della finestra per l'icona", app.winfo_class() == "Battitostelle", app.winfo_class())
            if risultato.get("ris"):
                app._nuovo_risultato(risultato["ris"])
                app.update()
            for s in ("100 %", "200 %", "150 %"):
                app.scala.set(s)
                app.applica_scala()
                app.update()
            controlla("grafico con i pulsanti italiani al posto della barra", len(app.pulsanti_grafico) == 6
                      and not app.barra.winfo_ismapped() and len(app.figura.axes) >= 1,
                      f"({len(app.figura.axes)} grafici)")
            try:
                if sys.platform.startswith("win"):
                    app.state("normal")
                else:
                    app.attributes("-zoomed", False)
            except tk.TclError:
                pass
            app.update()
            sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
            # finestra di prova realistica: circa metà schermo (come affiancata a un'altra finestra)
            gw, gh = max(760, int(sw * 0.55)), max(600, int(sh * 0.75))
            app.geometry(f"{gw}x{gh}")
            def nome_di(p):
                return p.winfo_children()[0].cget("text") if p.winfo_children() else p.cget("text")

            def misura(elementi):
                for _ in range(4):                      # lascia assestare la disposizione
                    app.update()
                    app._disponi_opzioni()
                    for r in (app.riga_pulsanti, app.riga_strumenti, app.riga_grafici, app.riga_comandi):
                        r._ultima = None
                        r.disponi()
                    time.sleep(0.1)
                app.update()
                f_ = [f"{nome_di(p)}: x {p.winfo_rootx() - app.winfo_rootx()} + largo {p.winfo_width()} > finestra "
                      f"{app.winfo_width()}" for p in elementi
                      if p.winfo_rootx() + p.winfo_width() > app.winfo_rootx() + app.winfo_width() + 2]
                s_ = [nome_di(p) for p in elementi if not p.winfo_ismapped()
                      or p.winfo_rooty() + p.winfo_height() > app.winfo_rooty() + app.winfo_height() + 2]
                return f_, s_

            app.schede.select(app.pag_stelle)
            fuori, sotto = misura(app.riga_pulsanti.elementi + app.riga_opz.elementi + app.riga_comandi.elementi)
            app.schede.select(app.pag_grafici)
            f2, s2 = misura(list(app.pulsanti_grafico.values()) + list(app.pulsanti_quale.values())
                            + app.riga_pulsanti.elementi)
            fuori += f2
            sotto += s2
            print(f"   (schermo visto da Tk {sw}×{sh}, finestra di prova {app.winfo_width()}×{app.winfo_height()}, testo 150 %)")
            if sotto:
                print(f"   AVVISO: in questa finestra di prova non stanno in altezza: {', '.join(sotto)}")
            if risultato.get("ris"):
                for k, _t in GRAFICI:
                    app._scegli_grafico(k)
                    app.update()
                alto_grafico = app.tela.get_tk_widget().winfo_height()
                controlla("tutti i grafici disegnati, uno alla volta", len(app.figura.axes) >= 1,
                          f"(area del grafico alta {alto_grafico} pixel su {app.winfo_height()})")
            controlla("pulsanti e opzioni dentro la finestra anche se è stretta (vanno a capo)", not fuori, str(fuori))
            for k in ("zoom", "zoom", "pan", "pan", "home"):
                app._strumento(k)
                app.update()
            app.trova_stelle()
            ft = app.finestra_trova
            ft.fonte.set(ft.ELENCO_FONTI[2][0])
            ft._aggiorna_filtri()
            for _ in range(30):
                app.update()
                time.sleep(0.05)
                if ft.albero.get_children():
                    break
            prima = app.testo_stelle.get("1.0", "end")
            ft.albero.selection_set(ft.albero.get_children()[:2])
            ft.aggiungi_scelte()
            app.update()
            dopo = app.testo_stelle.get("1.0", "end")
            controlla("finestra «Trova stelle»: elenco e aggiunta alla casella",
                      len(ft.albero.get_children()) >= 10 and len(dopo) > len(prima), ft.stato.get()[:60])
            car = tkfont.nametofont("TkDefaultFont")
            tagliate = []
            for i, (col, testo) in enumerate(ft.titoli):
                larg = int(ft.albero.column(col, "width"))
                valori = [testo] if col == "nota" else [testo] + [str(ft.albero.item(r, "values")[i])
                                                                  for r in ft.albero.get_children()]
                tagliate += [v for v in valori if car.measure(v) > larg]
            da_vedere = list(ft.winfo_children())
            while da_vedere:
                x = da_vedere.pop()
                da_vedere += list(x.winfo_children())
                if isinstance(x, ttk.Combobox):
                    cf = tkfont.Font(font=x.cget("font")) if str(x.cget("font")) else car
                    spazio = int(x.cget("width")) * cf.measure("0")
                    tagliate += [v for v in x.cget("values") if cf.measure(str(v)) > spazio]
            controlla("finestra «Trova stelle»: titoli, valori e tendine non tagliati", not tagliate, str(tagliate[:6]))
            ft.destroy()
            w = messaggio(app, "Prova", "Messaggio di prova con un testo abbastanza lungo da andare a capo. " * 4)
            app.update()
            w.destroy()
            app.after(2500, app.destroy)
            app.mainloop()
            controlla("finestra chiusa senza errori", True)
        finally:
            CONFIG = vecchio
    prova("finestra", t_finestra)

    ok = sum(esiti)
    print(f"\n{ok}/{len(esiti)} prove superate." + ("  TUTTO OK" if ok == len(esiti) else "  CI SONO ERRORI"), flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok == len(esiti) else 1


def main():
    a = sys.argv[1:]
    if "--versione" in a:
        print(TITOLO)
        return 0
    if "--autotest" in a:
        return autotest(con_finestra="--senza-finestra" not in a)
    if "--diagnostica" in a:
        return diagnostica()
    if "--analizza" in a:
        return analizza_da_terminale([x for x in a if x != "--analizza"])
    avvia_finestra()
    return 0


if __name__ == "__main__":
    sys.exit(main())
