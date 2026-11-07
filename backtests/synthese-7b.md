# Catalogue : synthèse

Calcul terminé le 07/11/2026 à 21:47 UTC (commit 48a90f5) : échantillon 21,1 min, hors échantillon 1,8 min.

Échantillon 09/11/2025 → 07/09/2026, hors échantillon 07/09/2026 → 07/11/2026, intervalle 15m et 1h, spread 0,02 %, frais 0,1 % par ordre, capital 10 000,00 USDT. Critères figés avant le calcul (README, « Backtest et catalogue »).

| Modèle | Paire | Statut | P&L hors éch. | Trades | Percentile tirages | Achat-clôture |
|---|---|---|---|---|---|---|
| tendance-1h | BTCUSDT | rejetee_en_echantillon | – | – | – | – |
| tendance-1h | ETHUSDT | rejetee_en_echantillon | – | – | – | – |
| tendance-1h | SOLUSDT | rejetee_en_echantillon | – | – | – | – |
| cassure-matin | BTCUSDT | rejetee_en_echantillon | – | – | – | – |
| cassure-matin | ETHUSDT | rejetee_en_echantillon | – | – | – | – |
| cassure-matin | SOLUSDT | rejetee_en_echantillon | – | – | – | – |
| repli-tendance | BTCUSDT | donnees_insuffisantes | -8,93 | 5 | 59 | -177,40 |
| repli-tendance | ETHUSDT | rejetee_en_echantillon | – | – | – | – |
| repli-tendance | SOLUSDT | rejetee_en_echantillon | – | – | – | – |

Stratégies admises : 0. Le catalogue est vide : c'est un résultat valide.

**Prudence statistique** : 1 couple(s) testé(s) hors échantillon contre les tirages au hasard, au seuil de 10 %. Même sans aucun avantage réel, environ 0,1 couple(s) passeraient ce critère par hasard (9 tests à ce seuil : environ un faux positif). Le critère reste à 90 % ; le percentile exact de chaque couple est donné ci-dessus et dans son rapport. En échantillon, 81 combinaisons ont été explorées ici, après 81 à l'étape 7 (autre période, sans passage hors échantillon) : le risque de retenir un couple par chance en est accru.
