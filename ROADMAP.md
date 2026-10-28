# Roadmap

## Vision

Paper trading crypto **intraday** (argent fictif, prix réels Binance) piloté par **une équipe d'agents IA qui débat**. 100 % gratuit, partageable : les agents ne dépendent d'aucun compte Claude ni d'aucune API payante.

Principe : les agents **n'inventent pas** de stratégies et **ne passent pas** les ordres eux-mêmes. Ils lisent le marché, débattent du **régime** du moment et **choisissent une stratégie dans un catalogue déjà backtesté**, avec des paramètres dans des bornes fixées. Le code l'exécute, le garde-fou a le dernier mot.

Architecture :

1. **Catalogue de stratégies (hors ligne)** : stratégies intraday simples, backtestées sur des mois de klines, frais et spread inclus. Seules celles qui battent les références restent, avec les régimes où elles fonctionnent.
2. **Équipe d'agents (secondes)** : débat en début de session, puis toutes les 1 à 2 h ou quand le régime change (volatilité, cassure). Sortie : le régime, la stratégie active (ou « ne rien faire ») et ses paramètres.
3. **Exécution (code pur, millisecondes)** : la stratégie active surveille les bougies 5m/15m et passe les ordres.
4. **Garde-fou de risque (code pur)** : dernier mot avant tout ordre. Aucun agent ne peut le contourner.

### Règles intraday

- **Session** = journée UTC (00:00 → 24:00), alignée sur le VWAP de session.
- **Aucune position ne passe la nuit** : clôture forcée de toutes les positions automatiques à 23:55 UTC (horaire paramétrable), plus d'entrée après 23:00 UTC.
- Positions tenues de quelques minutes à quelques heures, sur bougies 5m/15m. Pas de trading à la seconde : avec 0,1 % de frais par ordre (≈ 0,2 % par aller-retour + spread), il serait perdant par construction.
- Peu de trades par session : chaque aller-retour doit d'abord rembourser ses frais.

### Références (ce que les agents doivent battre)

- **Ne rien faire** : 0 %, sans frais.
- **Achat à l'ouverture de session, vente à la clôture** : l'équivalent intraday de « acheter et garder ».
- **Entrées au hasard** avec les mêmes stops, objectifs et tailles : pour vérifier que le gain vient du choix des entrées et pas des sorties.
- **Meilleure stratégie du catalogue, toujours active** : pour vérifier que le choix par les agents apporte quelque chose.

## Étapes

| # | Étape | Statut |
|---|-------|--------|
| 1 | Moteur (prix réels, ordres, portefeuille, persistance SQLite) | ✅ |
| 2 | API (FastAPI, PriceHub, cache de prix) | ✅ |
| 3 | Calculs de marché (bougies, SMA/EMA/RSI/VWAP, historique Binance) | ✅ |
| 4 | Ordres avancés, sessions intraday et comptes multiples | ⬜ |
| 5 | Garde-fou de risque | ⬜ |
| 6 | Briques et stratégies intraday (exécution rapide) | ⬜ |
| 7 | Journal, statistiques et backtest → catalogue | ⬜ |
| 8 | Équipe d'agents IA (choix du régime et de la stratégie) | ⬜ |
| 9 | Interface | ⬜ |
| 10 | Comparaison : agents vs références | ⬜ |
| 11 | Logiciel installable | ⬜ |

### 4. Ordres avancés, sessions intraday et comptes multiples

- Ordres limite, stop-loss, take-profit, et une paire stop + objectif liée (l'un annule l'autre).
- Déclenchement sur le flux de prix live (bid pour les sorties d'une position longue).
- Sessions : clôture forcée des positions automatiques à l'heure de fin de session, plus d'entrée après l'heure limite.
- Plusieurs comptes fictifs actifs en parallèle, chacun avec son capital (nécessaire pour comparer agents et références).
- Fin d'étape : un stop posé est exécuté automatiquement quand le prix le touche, et une position ouverte est fermée à la fin de session, testé sans réseau.

### 5. Garde-fou de risque

Module pur (aucun I/O), appelé avant chaque ordre automatique.

- Taille maximale de position (en % du capital).
- Stop-loss obligatoire pour tout ordre automatique.
- Perte maximale par session : au-delà, plus aucun ordre automatique jusqu'à la session suivante.
- Nombre maximal de trades par session et de positions ouvertes, délai minimal entre deux ordres sur une même paire.
- Refus clairs et journalisés.
- Fin d'étape : un ordre « tout le capital, sans stop » est refusé, testé.

### 6. Briques et stratégies intraday (exécution rapide)

- **Briques** : fonctions pures paramétrables qui lisent bougies et indicateurs (étape 3) : RSI en zone extrême, croisement d'EMA, écart au VWAP de session, cassure du range d'ouverture, filtre de volatilité.
- **Stratégie** = combinaison de briques décrite en JSON : `{paire, intervalle, entree, stop, objectif, taille, heures_actives}`. Pas de code libre.
- Stratégies de départ : retour au VWAP, suivi de tendance EMA, cassure du range d'ouverture.
- Aucun signal si `qualite` signale un prix périmé, un flux muet ou un historique incomplet.
- Fin d'étape : une stratégie activée trade seule en live via le garde-fou, et sa liste de signaux est exposée par une route.

### 7. Journal, statistiques et backtest → catalogue

- Sauvegarde des bougies dans SQLite et téléchargement de plusieurs mois de klines.
- Journal : chaque signal, décision, ordre et résultat.
- Statistiques par compte et par stratégie : P&L, drawdown maximal, taux de réussite, gain moyen par trade, nombre de trades, frais payés, ratio de Sharpe.
- Backtest intraday (sessions, clôture forcée, frais et spread inclus) de chaque stratégie et des références.
- Validation hors échantillon : paramètres choisis sur une période, testés sur une autre, pour éviter le surajustement.
- Classement par régime de marché (range, tendance, forte volatilité).
- **Catalogue** : seules les stratégies qui battent les références hors échantillon y entrent, avec leurs bornes de paramètres et leurs régimes.
- Fin d'étape : un rapport de backtest par stratégie sur au moins 3 mois, et un premier catalogue.

### 8. Équipe d'agents IA (choix du régime et de la stratégie)

- **À trancher en premier** : le modèle gratuit. Soit un modèle local (Ollama), soit une clé gratuite fournie par chaque utilisateur (vérifier les niveaux gratuits du moment). Ce choix conditionne l'étape 11.
- Rôles : analyste haussier, analyste baissier, critique, juge (reprise du projet labo-finance).
- Entrées : bougies, indicateurs, bloc `qualite`, résultats de la session en cours, catalogue avec ses statistiques.
- Sortie stricte en JSON : `{regime, strategie | "aucune", parametres, valide_jusqu_a, confiance, raison}`. Paramètres hors bornes ou réponse invalide : « aucune ».
- Débat en début de session, puis toutes les 1 à 2 h ou sur changement de régime. Délai maximal par agent ; un agent trop lent est ignoré.
- Chaque débat est enregistré dans le journal (étape 7).
- Fin d'étape : l'équipe pilote un compte fictif en live pendant une session complète, débats consultables.

### 9. Interface

- Graphiques en bougies en direct avec indicateurs et VWAP de session.
- Portefeuille, ordres, P&L de la session.
- Fil des débats (arguments de chaque agent, régime, stratégie choisie, refus du garde-fou).
- Bouton pour lancer ou arrêter les agents, choix du capital de départ.

### 10. Comparaison : agents vs références

- Comptes séparés en parallèle : l'équipe, un agent seul, les références (voir Vision).
- Comparaison sur les statistiques de l'étape 7 après au moins 2 semaines de sessions.
- Objectif : savoir avec des chiffres si le débat apporte quelque chose.

### 11. Logiciel installable

- Installation en un clic sous Windows, sans Python préinstallé.
- Aucune dépendance au compte de l'auteur ; configuration du modèle IA guidée au premier lancement.

## Décisions déjà prises

- 100 % gratuit : pas d'API payante, pas de compte requis pour les données de marché.
- Intraday : session = journée UTC, aucune position automatique ne passe la nuit.
- Données : Binance (repli Coinbase), bougies sur le prix des transactions, ordres exécutés à l'ask et au bid.
- VWAP de session remis à zéro à 00:00 UTC.
- Les agents choisissent un régime et une stratégie du catalogue ; ils n'écrivent pas de code et ne passent pas d'ordres.

## Idées

- Glissement (slippage) simulé pour les gros ordres, utile surtout en backtest.
- Synchroniser l'horloge locale ou corriger l'écart mesuré avec Binance.
- Ventes à découvert (short) simulées, pour profiter aussi des baisses.
