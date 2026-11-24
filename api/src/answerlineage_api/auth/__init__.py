"""Authentification et rôles : chaque route de l'API exige une identité OIDC et un rôle.

- ``roles.py`` : les six rôles des personnes et le rôle technique des émetteurs ;
- ``matrix.py`` : la matrice des droits, route par route, seule source de ce
  qu'ouvre chaque rôle, publiée dans l'OpenAPI et la documentation ;
- ``oidc.py`` : la vérification d'un jeton porteur JWT de l'émetteur configuré,
  ses clés lues de la découverte et du JWKS, en cache ;
- ``fetch.py`` : la lecture bornée des documents de l'émetteur ;
- ``access.py`` : la dépendance qui, avant toute route, lit le jeton et
  applique la matrice ; le réglage, et le mode désactivé de la démonstration ;
- ``routes.py`` : ce que l'explorateur lit sans jeton pour se connecter.
"""
