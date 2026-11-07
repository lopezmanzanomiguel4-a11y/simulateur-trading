# Simulateur de trading : règles de travail

Paper trading crypto intraday (argent fictif, prix réels Binance). Vision et étapes : `ROADMAP.md`. Usage : `README.md`.

## Méthode
- Une étape de la ROADMAP par session. Plan d'abord (mode Plan), pas de code avant validation.
- Fin d'étape : `python -m pytest -q`, commit seulement si 0 échec, jamais de push sans accord explicite.
- `RAPPORT-ETAPEn.md` à la racine, jamais commité. Y signaler tout test existant modifié, avec la raison.
- `ROADMAP.md` : ne jamais la réécrire de mémoire. Avant chaque commit, `git diff ROADMAP.md` ne doit montrer que la ligne de statut de l'étape.
- Essais réels : toujours `SIMTRADE_DB` sur une base temporaire dans le scratchpad, jamais la vraie base (`%LOCALAPPDATA%\simtrade\simtrade.db`). Arrêter l'API à la fin (port 8000 libre).
- `_journal/` et `.claude/` sont locaux : ne jamais les commiter.

## Économie de contexte
- pytest : toujours `-q`, et n'afficher que la fin de la sortie (`| Select-Object -Last 15` ou `| tail -15`), sauf en cas d'échec.
- Ne pas relire un fichier entier pour une fonction : grep, ou un intervalle de lignes.
- Ne pas relire un fichier qu'on vient d'écrire ou de modifier.
- Messages courts entre deux actions ; le détail va dans le rapport.
- Pas de sous-agents en plus du `verificateur`, sauf demande explicite.
- Longues attentes (mesures, essais) : lancer en arrière-plan et attendre la notification, sans relire la sortie en boucle.
