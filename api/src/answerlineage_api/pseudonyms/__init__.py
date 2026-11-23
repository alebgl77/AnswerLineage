"""Destinataires pseudonymisés, et leur révélation journalisée.

Un destinataire n'entre dans AnswerLineage que sous son pseudonyme : un HMAC-SHA-256
de son identité, sous une clé de locataire que l'émetteur et l'API tiennent d'un
coffre à clés, jamais de la base. Aucune table ne relie un pseudonyme à une identité.

- ``vault.py`` : la forme d'un pseudonyme, la forme canonique d'une identité, le
  calcul, et le coffre à clés (fichier ou variable aujourd'hui, une interface prête
  pour un KMS) ;
- ``models.py`` : la demande de révélation et ce que rendent les routes ;
- ``store.py`` : la révélation et son journal, en ajout seul ;
- ``routes.py`` : révéler un destinataire, lire le journal des révélations.

Révéler ne lit rien que la base ne sache : la personne qui révèle apporte des
identités candidates, tirées de son annuaire ou du canal de la diffusion, et l'API
dit laquelle a ce pseudonyme en recalculant le HMAC avec la clé du coffre. Rien n'est
gardé que le journal de l'action : qui, quand, pourquoi, quel pseudonyme, quel
résultat ; jamais l'identité.
"""
