"""Erreurs métier. Leur message est destiné à être affiché tel quel à l'utilisateur."""


class SimTradeError(Exception):
    """Base de toutes les erreurs prévues du simulateur."""


class InvalidInput(SimTradeError):
    """Saisie illisible : nombre invalide, paire inconnue..."""


class InvalidCapital(SimTradeError):
    """Capital de départ refusé (nul, négatif, trop grand...)."""


class OrderRejected(SimTradeError):
    """Ordre impossible : solde insuffisant, vente sans position..."""


class FeedUnavailable(SimTradeError):
    """Aucune source de prix n'a pu fournir de cotation."""
