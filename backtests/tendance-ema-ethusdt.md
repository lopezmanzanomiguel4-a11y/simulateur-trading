# tendance-ema sur ETHUSDT (5m)

Statut : **rejetee_en_echantillon** (aucune combinaison avec ≥ 30 trades et un P&L > 0 en échantillon)

Échantillon : 09/05/2026 → 07/09/2026 ; hors échantillon : 07/09/2026 → 07/11/2026 (fin exclue). Spread 0.02 %, frais 0.100 % par ordre, capital 10000 USDT.

## Grille en échantillon

P&L en USDT (trades) ; lignes : `croisement_ema.rapide`, colonnes : `croisement_ema.lente`. * : candidate (≥ 30 trades et P&L > 0) ; [ ] : combinaison choisie.

| | 21 | 30 | 42 |
|---|---|---|---|
| **6** | -574.50 (197) | -484.96 (172) | -389.21 (148) |
| **9** | -508.48 (168) | -384.89 (145) | -301.57 (127) |
| **12** | -388.89 (149) | -309.36 (128) | -296.04 (115) |

Score de voisinage (moyenne du P&L de la combinaison et de ses voisines directes) :

| | 21 | 30 | 42 |
|---|---|---|---|
| **6** | -522.65 | -458.39 | -391.91 |
| **9** | -464.19 | -397.85 | -342.93 |
| **12** | -402.25 | -344.80 | -302.33 |

Aucune candidate : hors échantillon non lancé.
