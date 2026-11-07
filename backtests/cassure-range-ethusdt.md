# cassure-range sur ETHUSDT (5m)

Statut : **rejetee_en_echantillon** (aucune combinaison avec ≥ 30 trades et un P&L > 0 en échantillon)

Échantillon : 09/05/2026 → 07/09/2026 ; hors échantillon : 07/09/2026 → 07/11/2026 (fin exclue). Spread 0.02 %, frais 0.100 % par ordre, capital 10000 USDT.

## Grille en échantillon

P&L en USDT (trades) ; lignes : `cassure_range.minutes`, colonnes : `objectif.multiple`. * : candidate (≥ 30 trades et P&L > 0) ; [ ] : combinaison choisie.

| | 1 | 1.5 | 2.5 |
|---|---|---|---|
| **15** | -124.10 (57) | -139.47 (68) | -137.86 (68) |
| **30** | -198.91 (85) | -203.69 (89) | -189.19 (89) |
| **60** | -226.19 (84) | -218.73 (84) | -208.85 (84) |

Score de voisinage (moyenne du P&L de la combinaison et de ses voisines directes) :

| | 1 | 1.5 | 2.5 |
|---|---|---|---|
| **15** | -154.16 | -151.28 | -155.51 |
| **30** | -188.22 | -190.00 | -184.90 |
| **60** | -214.61 | -214.37 | -205.59 |

Aucune candidate : hors échantillon non lancé.
