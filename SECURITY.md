# Politique de sécurité

AnswerLineage voit passer les appels d'outils des agents d'une entreprise. Une faille y compte double : merci de la signaler en privé.

## Signaler une vulnérabilité

N'ouvrez pas d'issue publique. Utilisez le signalement privé de GitHub : onglet **Security** du dépôt, puis **Report a vulnerability**, ou directement [ce formulaire](https://github.com/alebgl77/AnswerLineage/security/advisories/new).

Le signalement reste confidentiel, entre vous et les mainteneurs, jusqu'à la publication du correctif.

Indiquez autant que possible :

- le composant touché et le commit ou la version ;
- le scénario d'attaque et ce qu'il permet ;
- un exemple minimal qui le reproduit.

Ne joignez aucune donnée réelle : ni jeton, ni contenu de prompt, de résultat d'outil ou de réponse.

## Délais

| Étape | Délai |
| --- | --- |
| Accusé de réception | 72 heures |
| Première évaluation : gravité et périmètre | 7 jours |
| Correctif d'une faille critique | 72 heures après sa confirmation |
| Correctif d'une faille haute | 7 jours après sa confirmation |
| Autres failles | Dans la version suivante |

La divulgation est coordonnée : un avis de sécurité est publié sur GitHub avec le correctif, et son découvreur y est crédité s'il le souhaite. Si un délai ne peut pas être tenu, vous en êtes prévenu avant son échéance.

## Versions suivies

Le projet est en construction et n'a pas encore publié de version. Seule la branche principale reçoit des correctifs.

## Ce qui compte comme une vulnérabilité

Tout ce qui fait mentir une garantie du produit, en plus des failles habituelles :

- un contenu de prompt, de résultat d'outil ou de réponse stocké, journalisé ou mis en métrique ;
- un jeton conservé, journalisé ou rejoué par le proxy ;
- un appel d'agent modifié, retardé ou bloqué par la capture ;
- un texte libre venu d'un émetteur qui atteint un destinataire ou un modèle ;
- un identifiant reçu recopié sans contrôle de forme, ou une entrée non bornée qui épuise un service ;
- un contournement du contrôle des en-têtes `Host` et `Origin` ;
- une faille de la chaîne de construction : workflow, dépendance, image.

Deux limites sont connues et documentées, et ne sont donc pas des découvertes : l'API n'authentifie pas encore ses appelants, et les émetteurs d'événements ne sont pas authentifiés. Dans cet état, l'API est faite pour la boucle locale ou un réseau de confiance.
