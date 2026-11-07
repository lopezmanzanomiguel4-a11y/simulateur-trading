# retour-vwap sur SOLUSDT (5m)

Statut : **rejetee_en_echantillon** (aucune combinaison avec ≥ 30 trades et un P&L > 0 en échantillon)

Échantillon : 09/05/2026 → 07/09/2026 ; hors échantillon : 07/09/2026 → 07/11/2026 (fin exclue). Spread 0.02 %, frais 0.100 % par ordre, capital 10000 USDT.

## Grille en échantillon

P&L en USDT (trades) ; lignes : `ecart_vwap.seuil_pct`, colonnes : `rsi_extreme.seuil`. * : candidate (≥ 30 trades et P&L > 0) ; [ ] : combinaison choisie.

| | 25 | 30 | 35 |
|---|---|---|---|
| **0.4** | -146.39 (124) | -531.99 (255) | -771.76 (385) |
| **0.6** | -146.37 (120) | -491.14 (241) | -668.25 (346) |
| **0.9** | -122.28 (105) | -434.41 (206) | -511.94 (280) |

Score de voisinage (moyenne du P&L de la combinaison et de ses voisines directes) :

| | 25 | 30 | 35 |
|---|---|---|---|
| **0.4** | -274.91 | -485.32 | -657.34 |
| **0.6** | -226.54 | -454.43 | -610.77 |
| **0.9** | -234.35 | -389.94 | -538.20 |

Aucune candidate : hors échantillon non lancé.
