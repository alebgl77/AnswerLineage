# 0013. Des artefacts signés sans clé et attestés

Statut : en vigueur.

## Contexte

Le produit est installé par des équipes qui l'exposent à ce que leurs agents lisent. Elles doivent pouvoir vérifier que l'image et les paquets qu'elles installent sortent bien du dépôt, par le workflow prévu, et pas d'un poste ou d'un compte compromis. Cette vérification ne doit pas reposer sur un secret qu'il faudrait garder, faire tourner et protéger : un secret de plus est un secret qui peut fuir, et une signature produite par une clé volée est valable.

## Décision

Une version sort quand le mainteneur pose une étiquette `v1.2.3` : le workflow `release` construit l'image et les paquets, les publie, les signe et les atteste. Aucune clé de signature ni aucun jeton de publication n'existe, ni dans le dépôt ni dans ses secrets.

- Le workflow obtient de GitHub un jeton OIDC ; Fulcio, l'autorité de Sigstore, lui délivre en échange un certificat de quelques minutes au nom de l'identité du workflow lancé par l'étiquette ; la signature est inscrite au journal de transparence public (Rekor).
- L'image est signée (cosign) par son empreinte `sha256:…`, jamais par son étiquette, qui peut être déplacée ; sa provenance (SLSA) et son SBOM CycloneDX sont attestés. Les paquets reçoivent une attestation de provenance et l'attestation de publication de PyPI (PEP 740), publiés par publication de confiance. La version GitHub joint `SHA256SUMS`.
- Tout ce qui peut échouer sans rien publier passe d'abord (étiquette, métadonnées, notes, analyse de l'image) ; l'image, qui se republie, précède les paquets, que PyPI ne laisse jamais remplacer ; la version GitHub vient en dernier.
- La signature s'éprouve à chaque merge request sans rien publier : un registre éphémère, des clés jetables, aucun journal public ; l'image signée doit se vérifier, une image modifiée ne doit pas passer (`scripts/image.sh signtest`).

Comment vérifier une image ou un paquet : [Publication](../publication.md).

## Alternative écartée

**Une clé de signature stockée, dans les secrets du dépôt ou dans un coffre.**

Elle serait plus simple à comprendre et fonctionnerait hors de GitHub. Mais elle demande de la garder, de la renouveler et d'en limiter l'usage ; qui la détient signe n'importe quoi, et sa fuite oblige à révoquer et à re-signer. Surtout, une signature par clé dit « quelqu'un qui détenait la clé a signé », pas « ce workflow, lancé par cette étiquette, sur un runner de GitHub, a produit cet artefact ». Sans clé, la preuve porte sur l'identité du workflow, son étiquette et le commit des sources.

Le pipeline l'impose : `make policy` refuse un job qui publie (permission `packages` ou `id-token` en écriture, envoi sur PyPI) et qui reçoit un secret ou un mot de passe.

## Conséquences

- Tout se vérifie sans compte du projet, avec `cosign` et `gh attestation verify`, et la signature est publique : chacun retrouve ce que l'identité a signé.
- Une vérification ne vaut que par l'identité exigée : une expression régulière trop large accepterait la signature de n'importe quel workflow de GitHub.
- **Ce que la vérification ne prouve pas.** Que le code est sain (elle dit d'où vient l'artefact, pas ce qu'il fait) ; que l'étiquette a été posée par le mainteneur (quiconque peut pousser une étiquette `v*` déclenche une publication valablement signée, d'où la protection des étiquettes) ; que le workflow de publication est celui que l'on croit (une modification fusionnée produirait elle aussi des signatures valables : les workflows sont des chemins protégés) ; qu'une autre construction des mêmes sources donnerait les mêmes octets ; qu'une version signée est toujours sûre : une signature ne se révoque pas, une version vulnérable est signalée par un avis de sécurité.
- La publication dépend de GitHub (OIDC) et de Sigstore (Fulcio, Rekor) : une panne de l'un retarde une version.
- Les noms de distribution doivent être réservés avant la première version : un éditeur de confiance en attente de PyPI ne réserve rien, et le nom de l'image ne peut être pris que par le propriétaire du dépôt.
- Le workflow `release` ne tourne jamais sur une merge request et ne lit aucun secret.
