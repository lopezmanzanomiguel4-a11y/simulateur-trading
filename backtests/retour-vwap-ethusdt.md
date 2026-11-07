# retour-vwap sur ETHUSDT (5m)

Statut : **rejetee_en_echantillon** (aucune combinaison avec ≥ 30 trades et un P&L > 0 en échantillon)

Échantillon : 09/05/2026 → 07/09/2026 ; hors échantillon : 07/09/2026 → 07/11/2026 (fin exclue). Spread 0.02 %, frais 0.100 % par ordre, capital 10000 USDT.

## Grille en échantillon

P&L en USDT (trades) ; lignes : `ecart_vwap.seuil_pct`, colonnes : `rsi_extreme.seuil`. * : candidate (≥ 30 trades et P&L > 0) ; [ ] : combinaison choisie.

| | 25 | 30 | 35 |
|---|---|---|---|
| **0.4** | -346.13 (123) | -536.71 (235) | -689.09 (343) |
| **0.6** | -346.72 (120) | -491.37 (214) | -556.80 (300) |
| **0.9** | -286.30 (99) | -392.16 (170) | -462.95 (220) |

Score de voisinage (moyenne du P&L de la combinaison et de ses voisines directes) :

| | 25 | 30 | 35 |
|---|---|---|---|
| **0.4** | -409.85 | -515.83 | -594.20 |
| **0.6** | -367.63 | -464.75 | -550.05 |
| **0.9** | -341.72 | -408.19 | -470.64 |

Aucune candidate : hors échantillon non lancé.
