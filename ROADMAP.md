# Roadmap

## Vision

Paper trading crypto intraday (argent fictif, prix réels Binance) où **une équipe d'agents IA débat** pour décider des mouvements à faire. 100 % gratuit, partageable : les agents ne dépendent d'aucun compte Claude ni d'aucune API payante.

Architecture visée, à deux vitesses :

1. **Couche rapide (code pur, millisecondes)** : des experts à règles surveillent les bougies et indicateurs en continu et lèvent un signal.
2. **Couche de débat (agents IA, secondes)** : déclenchée **seulement sur signal**, au plus un débat toutes les 5 min par paire. Les analystes débattent, un juge tranche.
3. **Garde-fou de risque (code pur)** : dernier mot avant toute exécution. Aucun agent ne peut le contourner.

Horizon de trading : positions tenues de 15 min à quelques heures, sur bougies 5m/15m. Pas de trading à la seconde : avec 0,1 % de frais par ordre (≈ 0,2 % par aller-retour + spread), il serait perdant par construction.

## Étapes

| # | Étape | Statut |
|---|-------|--------|
| 1 | Moteur (prix réels, ordres, portefeuille, persistance SQLite) | ✅ |
| 2 | API (FastAPI, PriceHub, cache de prix) | ✅ |
| 3 | Calculs de marché (bougies, SMA/EMA/RSI/VWAP, historique Binance) | ✅ |
| 4 | Ordres avancés et comptes multiples | ⬜ |
| 5 | Garde-fou de risque | ⬜ |
| 6 | Experts à règles (couche rapide) | ⬜ |
| 7 | Journal, statistiques et backtest des règles | ⬜ |
| 8 | Équipe d'agents IA (couche de débat) | ⬜ |
| 9 | Interface | ⬜ |
| 10 | Comparaison : agent seul vs équipe | ⬜ |
| 11 | Logiciel installable | ⬜ |

### 4. Ordres avancés et comptes multiples

Les agents doivent pouvoir poser une sortie, pas seulement acheter ou vendre au marché.

- Ordres limite, stop-loss, take-profit, et une paire stop + objectif liée (l'un annule l'autre).
- Déclenchement sur le flux de prix live (bid pour les sorties d'une position longue).
- Plusieurs comptes fictifs actifs en parallèle, chacun avec son capital (nécessaire pour l'étape 10).
- Fin d'étape : un stop posé est exécuté automatiquement quand le prix le touche, testé sans réseau.

### 5. Garde-fou de risque

Module pur (aucun I/O), appelé avant chaque ordre automatique.

- Taille maximale de position (en % du capital).
- Stop-loss obligatoire pour tout ordre venant d'un expert ou d'un agent.
- Perte maximale par jour : au-delà, plus aucun ordre automatique jusqu'au lendemain (00:00 UTC).
- Nombre maximal de positions ouvertes, délai minimal entre deux ordres sur une même paire.
- Refus clairs et journalisés.
- Fin d'étape : un ordre « tout le capital, sans stop » est refusé, testé.

### 6. Experts à règles (couche rapide)

Fonctions pures qui lisent bougies et indicateurs (étape 3) et renvoient un signal, pas un ordre.

- Exemples : RSI en zone extrême, croisement d'EMA, écart au VWAP de session, cassure d'un range.
- Format commun : `{paire, intervalle, sens, force, raison, heure}`.
- Ignorer tout signal si `qualite` signale un prix périmé, un flux muet ou un historique incomplet.
- Mode « expert seul » : un expert peut trader directement via le garde-fou. Ça sert de référence pour juger les agents.
- Fin d'étape : les experts tournent en live dans l'API et leurs signaux sont exposés par une route.

### 7. Journal, statistiques et backtest des règles

- Sauvegarde des bougies dans SQLite (plus besoin de tout recharger, historique long possible).
- Journal : chaque signal, décision, ordre et résultat, avec le prix ensuite (à +15 min, +1 h).
- Statistiques par compte et par stratégie : P&L, drawdown maximal, taux de réussite, gain moyen par trade, ratio de Sharpe.
- Backtest des experts à règles sur l'historique (rapide et gratuit, puisque sans IA), frais et spread inclus.
- Fin d'étape : un rapport de backtest pour chaque expert sur au moins un mois de klines.

### 8. Équipe d'agents IA (couche de débat)

- **À trancher en premier** : le modèle gratuit. Soit un modèle local (Ollama), soit une clé gratuite fournie par chaque utilisateur (vérifier les niveaux gratuits du moment). Ce choix conditionne l'étape 11.
- Rôles : analyste haussier, analyste baissier, critique, juge (reprise du projet labo-finance).
- Entrées : bougies, indicateurs, bloc `qualite`, signal déclencheur, positions ouvertes.
- Sortie stricte en JSON : `{action, confiance, stop, objectif, raison}`. Une réponse invalide vaut « ne rien faire ».
- Délai maximal par agent ; un agent trop lent est ignoré.
- Déclenchement sur signal uniquement, au plus un débat toutes les 5 min par paire.
- Toute décision passe par le garde-fou (étape 5).
- Chaque débat est enregistré dans le journal (étape 7) : qui a dit quoi, décision, résultat.
- Fin d'étape : l'équipe trade en live sur un compte fictif, et les débats sont consultables.

### 9. Interface

- Graphiques en bougies en direct avec indicateurs.
- Portefeuille, ordres, P&L.
- Fil des débats en direct (arguments de chaque agent, décision du juge, refus du garde-fou).
- Bouton pour lancer ou arrêter les agents, choix du capital de départ.

### 10. Comparaison : agent seul vs équipe

- Même marché, même période, comptes séparés : expert à règles seul, un agent IA seul, l'équipe.
- Comparaison sur les statistiques de l'étape 7 après au moins une semaine.
- Objectif : savoir avec des chiffres si le débat apporte quelque chose.

### 11. Logiciel installable

- Installation en un clic sous Windows, sans Python préinstallé.
- Aucune dépendance au compte de l'auteur ; configuration du modèle IA guidée au premier lancement.

## Décisions déjà prises

- 100 % gratuit : pas d'API payante, pas de compte requis pour les données de marché.
- Données : Binance (repli Coinbase), bougies sur le prix des transactions, ordres exécutés à l'ask et au bid.
- VWAP de session remis à zéro à 00:00 UTC.
- Les agents ne tradent jamais à la seconde : décisions sur bougies 5m/15m, déclenchées par signal.

## Idées

- Glissement (slippage) simulé pour les gros ordres, utile surtout en backtest.
- Synchroniser l'horloge locale ou corriger l'écart mesuré avec Binance.
