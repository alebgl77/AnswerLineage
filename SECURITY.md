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
| Correctif ou mesure d'atténuation d'une faille critique, publié avec un avis | 72 heures après sa confirmation |
| Correctif d'une faille haute | 7 jours après sa confirmation |
| Autres failles | Dans la version suivante |

Le délai d'un correctif court depuis la **confirmation** de la faille, pas depuis son signalement : le moment où un mainteneur l'a reproduite et a fixé sa gravité, qu'il note, avec l'échéance, dans le brouillon de l'avis. L'accusé de réception et la première évaluation se comptent, eux, depuis le signalement.

Une **mesure d'atténuation** protège en attendant le correctif. C'est l'une de ces trois choses :

- un réglage : une option à poser, une valeur à changer, un service ou une route à ne plus exposer ;
- un contournement documenté : ce que fait l'exploitant, et comment il vérifie que c'est en place ;
- le retrait d'une fonction : une version qui retire ou désactive la fonction touchée jusqu'à son correctif.

Elle est publiée avec l'avis, qui dit ce qu'elle protège, ce qu'elle coûte et quand le correctif est attendu ; l'avis est mis à jour à la sortie du correctif.

La divulgation est coordonnée : un avis de sécurité est publié sur GitHub avec le correctif ou la mesure d'atténuation, et son découvreur y est crédité s'il le souhaite. Si un délai ne peut pas être tenu, vous en êtes prévenu avant son échéance. [La marche à suivre des mainteneurs](docs/securite/avis.md) dit comment ces délais sont tenus, et donne le gabarit de chaque avis.

## Versions suivies

Le projet est en construction et n'a pas encore publié de version. Seule la branche principale reçoit des correctifs.

Une image publiée dans le registre de conteneurs de GitHub (`ghcr.io/alebgl77/answerlineage`) a été analysée par Grype le jour de sa publication, sans vulnérabilité haute ou critique connue qui n'ait une exception motivée et datée dans `vulnerability-exceptions.toml` ; son SBOM CycloneDX lui est attaché par une attestation, que `gh attestation verify` vérifie et lit, comme le montre [la documentation du pipeline](docs/ci.md#publication-dune-version).

Chaque version GitHub porte aussi, parmi ses fichiers, le relevé des vulnérabilités connues de ses dépendances le jour de sa publication : celles de l'image, des paquets Python publiés sur PyPI et de l'explorateur, quelle que soit leur gravité, chacune avec son état. La même règle s'y applique : une version ne sort pas avec une vulnérabilité haute ou critique sans exception. [La documentation de publication](docs/publication.md#le-relevé-des-vulnérabilités-connues) dit comment le lire et le rejouer.

L'image est signée sans clé par le workflow de publication, et l'image comme les paquets Python publiés sur PyPI portent une attestation de provenance signée par le même workflow : [vérifiez-les avant de les installer](docs/publication.md), avec l'identité exacte que donne ce document. Les paquets du projet ne se publient que sous les noms que ce document liste.

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

Le [modèle de menaces](docs/securite/modele-de-menaces.md) tient les menaces connues de chaque composant, leurs parades et les risques acceptés, dont ces deux limites ; il est revu à chaque version, et une version ne sort pas tant qu'une acceptation y reste à signer.
