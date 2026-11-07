# cassure-range sur SOLUSDT (5m)

Statut : **rejetee_en_echantillon** (aucune combinaison avec ≥ 30 trades et un P&L > 0 en échantillon)

Échantillon : 09/05/2026 → 07/09/2026 ; hors échantillon : 07/09/2026 → 07/11/2026 (fin exclue). Spread 0.02 %, frais 0.100 % par ordre, capital 10000 USDT.

## Grille en échantillon

P&L en USDT (trades) ; lignes : `cassure_range.minutes`, colonnes : `objectif.multiple`. * : candidate (≥ 30 trades et P&L > 0) ; [ ] : combinaison choisie.

| | 1 | 1.5 | 2.5 |
|---|---|---|---|
| **15** | -181.69 (65) | -193.07 (67) | -190.99 (67) |
| **30** | -222.63 (96) | -190.55 (96) | -217.36 (96) |
| **60** | -138.80 (93) | -153.33 (94) | -141.68 (94) |

Score de voisinage (moyenne du P&L de la combinaison et de ses voisines directes) :

| | 1 | 1.5 | 2.5 |
|---|---|---|---|
| **15** | -199.13 | -189.08 | -200.47 |
| **30** | -183.42 | -195.39 | -185.15 |
| **60** | -171.59 | -156.09 | -170.79 |

Aucune candidate : hors échantillon non lancé.
