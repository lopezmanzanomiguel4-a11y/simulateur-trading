# retour-vwap sur BTCUSDT (5m)

Statut : **rejetee_en_echantillon** (aucune combinaison avec ≥ 30 trades et un P&L > 0 en échantillon)

Échantillon : 09/05/2026 → 07/09/2026 ; hors échantillon : 07/09/2026 → 07/11/2026 (fin exclue). Spread 0.02 %, frais 0.100 % par ordre, capital 10000 USDT.

## Grille en échantillon

P&L en USDT (trades) ; lignes : `ecart_vwap.seuil_pct`, colonnes : `rsi_extreme.seuil`. * : candidate (≥ 30 trades et P&L > 0) ; [ ] : combinaison choisie.

| | 25 | 30 | 35 |
|---|---|---|---|
| **0.4** | -193.65 (98) | -440.53 (171) | -574.82 (242) |
| **0.6** | -183.30 (95) | -406.54 (163) | -545.73 (213) |
| **0.9** | -159.43 (76) | -323.68 (128) | -385.15 (159) |

Score de voisinage (moyenne du P&L de la combinaison et de ses voisines directes) :

| | 25 | 30 | 35 |
|---|---|---|---|
| **0.4** | -272.50 | -403.89 | -520.36 |
| **0.6** | -235.73 | -379.96 | -478.06 |
| **0.9** | -222.14 | -318.70 | -418.19 |

Aucune candidate : hors échantillon non lancé.
