# Avis de sécurité

Ce document dit au mainteneur comment un signalement privé devient un correctif, ou une mesure d'atténuation, publié avec un avis de sécurité ; puis il donne le gabarit de chaque avis. Les délais sont ceux du [tableau de la politique de sécurité](../../SECURITY.md#délais), qui les tient à lui seul : ce document y renvoie sans les recopier, et un test vérifie qu'il n'en dit pas d'autres. La même politique dit ce qu'est une mesure d'atténuation.

## Le compte du délai

Le délai d'un correctif court depuis la **confirmation**, pas depuis le signalement. La confirmation est le moment où un mainteneur a reproduit la faille, sur la branche principale ou sur une version publiée, et fixé sa gravité. Il l'écrit aussitôt dans le brouillon de l'avis, en heure UTC, avec l'échéance qui en découle :

> Confirmée le 2026-03-02 à 14:00 UTC, gravité critique. Échéance : correctif ou mesure d'atténuation publié avec l'avis le 2026-03-05 à 14:00 UTC au plus tard (72 heures).

Le délai se compte en heures d'horloge, fins de semaine comprises. Une gravité revue en cours de route change l'échéance, comptée depuis la même confirmation, et s'écrit de même dans le brouillon. Un signalement que le mainteneur ne parvient pas à reproduire n'est pas confirmé : il le dit au découvreur, avec ce qui lui manque, dans le délai de la première évaluation.

## La marche à suivre

Tout se passe dans le brouillon de l'avis et dans sa fourche privée : rien dans une issue, une merge request ou un canal public avant l'étape 5.

1. **Accepter le signalement.** Un signalement privé arrive dans l'onglet Security du dépôt, sous Advisories, comme un brouillon d'avis de sécurité que ne voient que les mainteneurs et le découvreur. Le mainteneur l'accepte comme brouillon, ou le ferme en disant pourquoi, et accuse réception dans le brouillon.
2. **Évaluer et confirmer.** Reproduire la faille ; nommer le composant et les versions touchées ; fixer la gravité avec le calculateur CVSS du brouillon ; citer la menace du [modèle de menaces](modele-de-menaces.md) que la faille fait mentir, ou dire qu'elle en est une nouvelle. Écrire la confirmation et son échéance, comme plus haut.
3. **Demander un identifiant CVE.** Depuis le brouillon (Request CVE) : GitHub, autorité d'attribution des CVE, réserve l'identifiant, qui reste privé jusqu'à la publication de l'avis. La demande se fait dès la confirmation, l'attribution n'étant pas immédiate. Un avis dont l'identifiant n'est pas encore attribué à l'échéance se publie sans lui, et le reçoit ensuite.
4. **Corriger dans une fourche privée.** Depuis le brouillon, ouvrir la fourche privée temporaire (Start a temporary private fork) et y inviter le découvreur s'il le souhaite. Le correctif suit les règles du dépôt : d'abord le test qui reproduit la faille, puis un commit cohérent, au message sobre, qui dit ce qui change et non comment exploiter la faille, et peut citer l'identifiant CVE. Le pipeline ne tourne pas dans la fourche : `make check` et `make image` s'y jouent sur le poste. La même branche prépare la version corrigée, comme le dit [Publication](../publication.md#comment-sort-une-version) : la version des composants et la revue du modèle de menaces, où entre la menace si elle était nouvelle. Si le correctif ne peut pas être prêt à l'échéance, la branche prépare la mesure d'atténuation, et le correctif suit.
5. **Publier la version corrigée.** Au dernier moment, la branche revue dans la fourche devient une merge request sur la branche principale : `ci-ok` doit y passer, comme pour toute fusion. Le mainteneur la fusionne, pose l'étiquette, et le workflow `release` publie l'image, les paquets et la version GitHub. Son bilan, le job `report`, doit dire la publication complète ; le [relevé des vulnérabilités connues](../publication.md#le-relevé-des-vulnérabilités-connues) joint à la version n'a, par construction, rien de bloquant.
6. **Publier l'avis.** Remplir le brouillon selon le gabarit ci-dessous, versions corrigées et références comprises, puis le publier : l'avis devient public, et l'identifiant CVE avec lui. La base d'avis de GitHub le reprend, et les projets qui dépendent des paquets touchés en sont avertis.
7. **Créditer.** Le découvreur est crédité dans l'avis (Credits), sous le nom ou le pseudonyme qu'il a choisi, s'il le souhaite ; sinon le crédit dit « Signalé en privé ». Le mainteneur lui envoie le lien de l'avis publié.

**Avec une mesure d'atténuation.** À l'échéance, l'avis se publie avec la mesure et sans version corrigée : il dit les versions touchées, la mesure, ce qu'elle coûte et quand le correctif est attendu. Une mesure qui demande une nouvelle version, comme le retrait d'une fonction, se publie comme un correctif, par les étapes 4 et 5. Quand le correctif sort, l'avis se met à jour : versions corrigées, et la mesure dite inutile une fois la version corrigée installée.

**Si l'échéance ne peut pas être tenue**, le découvreur en est prévenu dans le brouillon avant elle, avec la raison et la nouvelle date, comme la politique s'y engage.

## Le gabarit d'un avis

Le formulaire de l'avis tient à part ses champs structurés : l'écosystème (`pip` pour un paquet Python), le paquet, les versions touchées et corrigées, la gravité (CVSS), la faiblesse (CWE) et les crédits. La description en reprend l'essentiel, pour qui lit l'avis hors de GitHub, et suit ce gabarit, ses titres dans cet ordre, sans en ajouter ni en retirer :

```markdown
### Composant

<!-- Le composant touché et ce qui le publie : son paquet PyPI, l'image, ou les deux. -->
answerlineage-proxy, sur PyPI ; l'image ghcr.io/alebgl77/answerlineage.

### Versions touchées

>= 1.2.0, < 1.2.3

### Versions corrigées

<!-- « Aucune pour l'instant : voir la mesure d'atténuation. » tant que le correctif n'est pas sorti. -->
1.2.3

### Gravité

<!-- Le niveau, le vecteur CVSS complet que donne le calculateur de l'avis, et son score. -->
Critique : CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N (9,1)

### Ce que permet la faille

<!-- Ce qu'un attaquant obtient, dans quelles conditions, et la garantie du produit qu'elle fait mentir. Ni exploit, ni donnée réelle. -->

### Mesure d'atténuation

<!-- Le réglage, le contournement documenté ou la fonction retirée, et comment vérifier qu'il est en place. « Aucune : installer une version corrigée. » s'il n'y en a pas. -->

### Crédit

<!-- Le nom ou le pseudonyme que le découvreur a choisi, ou « Signalé en privé. » -->

### Références

<!-- L'identifiant CVE, la version GitHub corrigée, la menace du modèle de menaces (composant/nom-court). -->
```

Les commentaires du gabarit se retirent avant publication. Un avis ne contient ni jeton, ni contenu de prompt, de résultat d'outil ou de réponse, ni le nom d'une personne qui n'a pas choisi d'être créditée.
