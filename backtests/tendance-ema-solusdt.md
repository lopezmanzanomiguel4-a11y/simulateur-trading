# tendance-ema sur SOLUSDT (5m)

Statut : **rejetee_en_echantillon** (aucune combinaison avec ≥ 30 trades et un P&L > 0 en échantillon)

Échantillon : 09/05/2026 → 07/09/2026 ; hors échantillon : 07/09/2026 → 07/11/2026 (fin exclue). Spread 0.02 %, frais 0.100 % par ordre, capital 10000 USDT.

## Grille en échantillon

P&L en USDT (trades) ; lignes : `croisement_ema.rapide`, colonnes : `croisement_ema.lente`. * : candidate (≥ 30 trades et P&L > 0) ; [ ] : combinaison choisie.

| | 21 | 30 | 42 |
|---|---|---|---|
| **6** | -731.73 (260) | -621.58 (240) | -553.90 (208) |
| **9** | -593.51 (225) | -507.75 (196) | -509.43 (178) |
| **12** | -469.06 (204) | -439.10 (177) | -441.23 (164) |

Score de voisinage (moyenne du P&L de la combinaison et de ses voisines directes) :

| | 21 | 30 | 42 |
|---|---|---|---|
| **6** | -648.94 | -603.74 | -561.64 |
| **9** | -575.51 | -534.27 | -503.08 |
| **12** | -500.56 | -464.28 | -463.25 |

Aucune candidate : hors échantillon non lancé.
