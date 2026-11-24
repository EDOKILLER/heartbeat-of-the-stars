#!/usr/bin/env bash
# =============================================================================
# Il battito delle stelle 1.16 – crea l'eseguibile per Ubuntu, lo prova e lo installa (menu + Scrivania)
# © 2026 Edoardo Battistel
#
# Uso:  cd ~/Claude/Astropy/programma_battito_stelle/versione_1.16
#       bash crea_eseguibile_ubuntu.sh
# Usa il venv ~/Claude/Astropy/venv_compila (PyInstaller), che vede numpy, scipy, astropy,
# matplotlib e Pillow di sistema.
# =============================================================================
set -e
cd "$(dirname "$(readlink -f "$0")")"
PY="$HOME/Claude/Astropy/venv_compila/bin/python"

for f in battito_stelle.py motore_battito.py prova_sintetica.py crea_icona.py LEGGIMI.txt; do
    [ -f "$f" ] || { echo "ERRORE: manca $f in questa cartella ($(pwd))."; exit 1; }
done
[ -x "$PY" ] || { echo "ERRORE: non trovo $PY"; exit 1; }
[ -f battito_stelle.png ] || "$PY" crea_icona.py

echo "=== 1/5  Autotest dallo script (apre la finestra per qualche secondo) ==="
if ! "$PY" battito_stelle.py --autotest; then
    echo "L'autotest dello script è FALLITO: mandami il testo qui sopra."
    exit 1
fi

echo "=== 2/5  Creazione dell'eseguibile (qualche minuto) ==="
"$PY" -m PyInstaller --noconfirm --clean --onefile --windowed \
    --name BattitoStelle \
    --add-data "battito_stelle.png:." \
    --add-data "LEGGIMI.txt:." \
    --hidden-import motore_battito \
    --hidden-import prova_sintetica \
    --hidden-import PIL._tkinter_finder \
    --hidden-import matplotlib.backends.backend_tkagg \
    --hidden-import matplotlib.backends.backend_pdf \
    --collect-all astropy --collect-all erfa \
    --collect-data astropy_iers_data \
    --copy-metadata astropy --copy-metadata numpy --copy-metadata pyerfa \
    --exclude-module sklearn --exclude-module IPython --exclude-module pandas \
    battito_stelle.py

echo "=== 3/5  Verifica dell'eseguibile (autotest) ==="
if ! ./dist/BattitoStelle --autotest > autotest.txt 2>&1; then
    cat autotest.txt
    echo
    echo "L'autotest dell'eseguibile è FALLITO: i dettagli sono in $(pwd)/autotest.txt"
    echo "L'eseguibile non è stato installato. Mandami autotest.txt per la correzione."
    rm -rf build BattitoStelle.spec
    exit 1
fi
cat autotest.txt

echo "=== 4/5  Prova degli archivi in rete (diagnostica.txt) ==="
./dist/BattitoStelle --diagnostica || echo "(qualche archivio non risponde: vedi diagnostica.txt; il programma si installa lo stesso)"

echo "=== 5/5  Installazione nella home e icona nel menu ==="
DEST="$HOME/.local/bin"
ICONE="$HOME/.local/share/icons/hicolor/512x512/apps"
mkdir -p "$DEST" "$HOME/.local/share/applications" "$ICONE"
cp battito_stelle.png "$ICONE/battitostelle.png"
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
cp dist/BattitoStelle "$DEST/BattitoStelle"
chmod +x "$DEST/BattitoStelle"

# tk.Tk(className="BattitoStelle") -> classe della finestra «Battitostelle» (solo la prima maiuscola)
SCRIVANIA="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Scrivania")"
cat > "$HOME/.local/share/applications/battitostelle.desktop" <<FINE
[Desktop Entry]
Type=Application
Name=Il battito delle stelle
Comment=Astrosismologia con Kepler e TESS: νmax, Δν, massa, raggio, età e suono delle stelle – © 2026 Edoardo Battistel
Exec=$DEST/BattitoStelle
Icon=$ICONE/battitostelle.png
StartupWMClass=Battitostelle
Terminal=false
Categories=Science;Astronomy;Education;
FINE
chmod +x "$HOME/.local/share/applications/battitostelle.desktop"
update-desktop-database -q "$HOME/.local/share/applications" 2>/dev/null || true
if [ -d "$SCRIVANIA" ]; then
    cp "$HOME/.local/share/applications/battitostelle.desktop" "$SCRIVANIA/"
    chmod +x "$SCRIVANIA/battitostelle.desktop"
    gio set "$SCRIVANIA/battitostelle.desktop" metadata::trusted true 2>/dev/null || true
fi

rm -rf build BattitoStelle.spec
echo
echo "FATTO."
echo "  Eseguibile:  $DEST/BattitoStelle   (copia anche in $(pwd)/dist/)"
echo "  Avvio:       'Il battito delle stelle' nel menu o doppio clic sull'icona sulla Scrivania"
echo "               (la prima volta: tasto destro -> 'Consenti avvio')."
echo "  Risultati:   ~/Scrivania/Astronomia/Battito_stelle/<stella>/"
echo "  Prova vera:  $DEST/BattitoStelle --analizza \"KIC 757137\" 2>&1 | tee prova_757137.txt"
echo "               (atteso da Yu+2018: νmax 29,99 µHz, Δν 3,399 µHz)"
echo "  Controllo dell'icona nella barra: xwininfo -root -tree | grep -i battito"
