# Cahier de recette — acquisition TPE (extrait)

REQ-100 : Le système doit accepter un achat par carte à puce EMV Visa avec saisie du PIN online. L'achat doit pouvoir être annulé par le commerçant.

REQ-101 : En cas de PIN erroné, la transaction d'achat doit être refusée.

REQ-102 : Un paiement sans contact au-delà du plafond doit exiger le PIN du porteur.

REQ-103 : Un paiement sans contact Mastercard de 150 MAD en dessous du plafond est accepté sans PIN.

REQ-104 : Un retrait au GAB avec une carte expirée doit être refusé.

REQ-105 : Si le serveur d'autorisation ne répond pas (timeout), le terminal envoie une annulation automatique.

REQ-106 : Un achat avec une carte perdue (opposition) doit être refusé.

REQ-107 : Une pré-autorisation hôtel suivie d'une complétion doit être acceptée.

REQ-108 : Le switch doit rejeter un message dont le MAC est invalide.
