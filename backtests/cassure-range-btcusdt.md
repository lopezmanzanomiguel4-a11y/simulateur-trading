# cassure-range sur BTCUSDT (5m)

Statut : **rejetee_en_echantillon** (aucune combinaison avec ≥ 30 trades et un P&L > 0 en échantillon)

Échantillon : 09/05/2026 → 07/09/2026 ; hors échantillon : 07/09/2026 → 07/11/2026 (fin exclue). Spread 0.02 %, frais 0.100 % par ordre, capital 10000 USDT.

## Grille en échantillon

P&L en USDT (trades) ; lignes : `cassure_range.minutes`, colonnes : `objectif.multiple`. * : candidate (≥ 30 trades et P&L > 0) ; [ ] : combinaison choisie.

| | 1 | 1.5 | 2.5 |
|---|---|---|---|
| **15** | -127.00 (47) | -137.93 (53) | -129.37 (53) |
| **30** | -156.13 (64) | -145.04 (66) | -152.61 (66) |
| **60** | -152.02 (64) | -151.27 (64) | -167.11 (64) |

Score de voisinage (moyenne du P&L de la combinaison et de ses voisines directes) :

| | 1 | 1.5 | 2.5 |
|---|---|---|---|
| **15** | -140.35 | -134.83 | -139.97 |
| **30** | -145.05 | -148.59 | -148.53 |
| **60** | -153.14 | -153.86 | -157.00 |

Aucune candidate : hors échantillon non lancé.
