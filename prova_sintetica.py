# -*- coding: utf-8 -*-
"""
IL BATTITO DELLE STELLE - dati simulati per l'autotest
© 2026 Edoardo Battistel

Crea curve di luce di stelle che oscillano con νmax e Δν noti (modi l = 0, 1, 2 con profilo
lorentziano, fondo granulare, rumore bianco, buchi tra i quarti, tempi non uniformi come quelli
baricentrici) per controllare che il motore ritrovi i valori giusti.
"""
import numpy as np

import motore_battito as M


def spettro_limite(freq, nyq, numax, dnu, W=20.0, gamma=0.12, epsilon=1.0, H=None):
    a = 3382 * numax ** -0.609
    b1, b2 = 0.317 * numax ** 0.970, 0.948 * numax ** 0.992
    e2 = M.eta2(freq, nyq)
    harvey = M.ZETA * a ** 2 / b1 / (1 + (freq / b1) ** 4) + M.ZETA * a ** 2 / b2 / (1 + (freq / b2) ** 4)
    if H is None:
        H = 8.0 * harvey[np.argmin(np.abs(freq - numax))] / e2[np.argmin(np.abs(freq - numax))] + 30 * W
    sig = M.fwhm_inviluppo(numax) / 2.355
    modi = np.zeros_like(freq)
    nmin, nmax = int((numax - 4 * sig) / dnu) - 1, int((numax + 4 * sig) / dnu) + 2
    for n in range(max(nmin, 1), nmax):
        nu0 = dnu * (n + epsilon)
        for nu, vis in ((nu0, 1.0), (nu0 + 0.5 * dnu - 0.03 * dnu, 1.5), (nu0 - 0.12 * dnu, 0.6)):
            h = H * vis * np.exp(-0.5 * ((nu - numax) / sig) ** 2)
            sel = np.abs(freq - nu) < 40 * gamma
            modi[sel] += h / (1 + (2 * (freq[sel] - nu) / gamma) ** 2)
    return W + e2 * (harvey + modi)


def curva_simulata(numax, dnu, dt_s=1765.46, giorni=365.0, seme=3, W=20.0, gamma=0.12):
    rng = np.random.default_rng(seme)
    n = int(giorni * 86400 / dt_s)
    nf = n // 2 + 1
    freq = np.arange(nf) / (n * dt_s) * 1e6
    nyq = 0.5 / dt_s * 1e6
    L = spettro_limite(np.maximum(freq, 1e-3), nyq, numax, dnu, W=W, gamma=gamma)
    L[0] = 0
    var = L * n / (2 * dt_s * 1e-6)
    C = np.sqrt(var / 2) * (rng.standard_normal(nf) + 1j * rng.standard_normal(nf))
    x = np.fft.irfft(C, n)
    t = np.arange(n) * dt_s / 86400.0
    # tempi non uniformi (come i tempi baricentrici) e buchi
    t = t + 0.35 * dt_s / 86400.0 * np.sin(2 * np.pi * t / 372.5)
    tieni = rng.random(n) > 0.06
    for inizio in np.arange(90, giorni, 93):
        tieni &= ~((t > inizio) & (t < inizio + 1.5))
    return t[tieni], x[tieni], dt_s / 86400.0


def flusso_kepler(t, x_ppm, livello=1.0e5):
    """Trasforma ppm in un flusso con una deriva lenta, come i dati veri."""
    return livello * (1 + x_ppm * 1e-6) * (1 + 0.002 * np.sin(2 * np.pi * t / 60.0))
