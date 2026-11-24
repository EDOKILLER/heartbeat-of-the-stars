#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Icona di «Il battito delle stelle»: una gigante rossa che pulsa (onde concentriche),
attraversata da un battito cardiaco dorato fatto con il pettine dei modi di oscillazione,
su fondo blu-viola stellato.
© 2026 Edoardo Battistel
"""
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

LATO = 512


def crea(cartella):
    S = LATO * 2                      # disegno in grande e poi riduco (bordi morbidi)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # fondo: quadrato arrotondato con sfumatura radiale blu notte -> viola
    fondo = Image.new("RGBA", (S, S))
    px = fondo.load()
    cx, cy = S * 0.5, S * 0.44
    for y in range(S):
        for x in range(S):
            r = math.hypot(x - cx, y - cy) / (S * 0.75)
            t = min(r, 1.0)
            px[x, y] = (int(46 * (1 - t) + 10 * t), int(22 * (1 - t) + 8 * t), int(88 * (1 - t) + 34 * t), 255)
    maschera = Image.new("L", (S, S), 0)
    ImageDraw.Draw(maschera).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.18), fill=255)
    img.paste(fondo, (0, 0), maschera)
    d = ImageDraw.Draw(img)
    # stelline
    rnd = random.Random(7)
    for _ in range(90):
        x, y = rnd.uniform(0.06, 0.94) * S, rnd.uniform(0.06, 0.94) * S
        if math.hypot(x - cx, y - cy) < S * 0.33:
            continue
        rr = rnd.choice([1, 1, 2, 2, 3])
        a = rnd.randint(120, 255)
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=(255, 255, 235, a))
    # onde di pulsazione (anelli che si affievoliscono)
    onde = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    do = ImageDraw.Draw(onde)
    for i, r in enumerate((0.27, 0.33, 0.395, 0.46)):
        a = int(170 * (1 - i / 4.5))
        w = int(S * (0.012 - i * 0.0018))
        R = S * r
        do.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(255, 140, 80, a), width=max(w, 3))
    onde = onde.filter(ImageFilter.GaussianBlur(S * 0.004))
    img.alpha_composite(onde)
    # alone e stella (gigante rossa)
    alone = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    da = ImageDraw.Draw(alone)
    R = S * 0.25
    da.ellipse([cx - R, cy - R, cx + R, cy + R], fill=(255, 110, 40, 150))
    alone = alone.filter(ImageFilter.GaussianBlur(S * 0.035))
    img.alpha_composite(alone)
    stella = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ps = stella.load()
    R = S * 0.2
    for y in range(int(cy - R) - 2, int(cy + R) + 3):
        for x in range(int(cx - R) - 2, int(cx + R) + 3):
            r = math.hypot(x - cx, y - cy) / R
            if r <= 1.0:
                # oscuramento al bordo e granulazione leggera
                mu = math.sqrt(max(1 - r * r, 0))
                g = 0.92 + 0.08 * math.sin(x * 0.09) * math.sin(y * 0.11)
                lum = (0.35 + 0.65 * mu) * g
                ps[x, y] = (min(255, int(255 * lum + 40)), int(150 * lum), int(60 * lum), 255)
    img.alpha_composite(stella)
    # battito dorato: linea piatta, poi il «pettine» di picchi regolari, poi di nuovo piatta
    y0 = cy + S * 0.02
    punti = []
    for i in range(0, 401):
        u = i / 400
        x = S * (0.06 + 0.88 * u)
        y = y0
        if 0.3 < u < 0.7:
            v = (u - 0.3) / 0.4
            inv = math.exp(-0.5 * ((v - 0.5) / 0.2) ** 2)          # inviluppo gaussiano
            fase = (v * 5.0) % 1.0                                   # 5 picchi equidistanti (Δν)
            picco = math.exp(-0.5 * ((fase - 0.5) / 0.07) ** 2) - 0.35 * math.exp(-0.5 * ((fase - 0.72) / 0.05) ** 2)
            y = y0 - S * 0.24 * inv * picco
        punti.append((x, y))
    ombra = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(ombra).line(punti, fill=(255, 215, 90, 200), width=int(S * 0.03), joint="curve")
    ombra = ombra.filter(ImageFilter.GaussianBlur(S * 0.012))
    img.alpha_composite(ombra)
    ImageDraw.Draw(img).line(punti, fill=(255, 226, 120, 255), width=int(S * 0.014), joint="curve")
    # piccola scritta «Δν» dorata in basso
    try:
        from PIL import ImageFont
        font = None
        for nome in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(nome, int(S * 0.11))
                break
            except OSError:
                continue
        if font:
            testo = "Δν"
            b = ImageDraw.Draw(img).textbbox((0, 0), testo, font=font)
            ImageDraw.Draw(img).text(((S - (b[2] - b[0])) / 2, S * 0.80), testo, font=font, fill=(255, 215, 90, 255))
    except Exception:
        pass
    out = img.resize((LATO, LATO), Image.LANCZOS)
    cartella = Path(cartella)
    out.save(cartella / "battito_stelle.png")
    out.save(cartella / "battito_stelle.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128),
                                                     (256, 256)])
    return cartella / "battito_stelle.png"


if __name__ == "__main__":
    print(crea(Path(__file__).resolve().parent))
