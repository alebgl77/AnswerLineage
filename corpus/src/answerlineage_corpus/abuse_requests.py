"""Requêtes du corpus d'abus : une requête saine, une seule altération, ce qui part sur le fil.

Une base décrit une requête saine comme un document : méthode, chemin,
en-têtes, paramètres, corps, et ce qui est envoyé avant une lecture : des
événements (``seed``), des relevés de couverture (``reports``), des
déclarations d'incidents (``incidents``), des tests de qualité en échec qui font
naître une proposition d'incident (``proposals``). Une altération change la valeur
d'un champ de ce document, désigné par un pointeur JSON (RFC 6901), celle d'un
en-tête, ou applique une recette prise dans une liste fermée. ``build`` rend
la requête à jouer, octet pour octet.

Rien ici ne connaît le produit : ni ses validateurs, ni ses bornes. Une
recette fabrique une requête, elle ne dit pas ce qu'il faut en attendre.
"""

from __future__ import annotations

import base64
import copy
import gzip
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlencode

# Nom d'hôte toujours attendu par un service qui écoute sur la boucle locale.
LOCAL_HOST: Final = "localhost"
# En-têtes par lesquels un relais dit l'hôte que le client visait.
RELAYED_HOST_HEADERS: Final = ("x-forwarded-host", "forwarded")
PADDING: Final = b" "
# Dans le chemin d'une lecture, l'identifiant du premier incident déclaré avant
# elle : le service le tire à la déclaration, celui qui joue la requête le pose.
INCIDENT: Final = "{incident}"
# Dans le chemin, l'identifiant de la première proposition d'incident née des tests en
# échec envoyés avant la requête : la base le tire, celui qui joue la requête le pose.
PROPOSAL: Final = "{proposal}"


@dataclass(frozen=True)
class Play:
    """La requête à jouer : celle de la base, altérée ou non."""

    method: str
    # Chemin et paramètres, prêts à l'envoi ; pour l'explorateur, le fragment de l'adresse.
    path: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""
    # Faux : le corps part en flux, sans longueur annoncée.
    announced: bool = True
    # Événements envoyés à l'ingestion avant une lecture, en un lot.
    seed: tuple[Mapping[str, Any], ...] = ()
    # Relevés de couverture publiés avant une lecture, un par un.
    reports: tuple[Mapping[str, Any], ...] = ()
    # Incidents déclarés avant une lecture, un par un, après les relevés.
    incidents: tuple[Mapping[str, Any], ...] = ()
    # Événements de tests de qualité en échec, envoyés en un lot après les incidents, datés
    # par qui joue la requête : une proposition ne naît que d'un échec récent.
    proposals: tuple[Mapping[str, Any], ...] = ()
    # Réglages du service : ceux de la base, qu'une recette peut abaisser.
    settings: Mapping[str, Any] = field(default_factory=dict)
    # Vrai : la lecture est retenue par un verrou, pour dépasser son délai sans charge énorme.
    slow: bool = False
    # Le jeton à présenter à la place de celui d'un appelant légitime : sa contrefaçon, d'une
    # liste fermée (``FORGES``), et la marque qu'il porte. Celui qui joue la requête le forge.
    token: Mapping[str, str] | None = None


class AlterationError(ValueError):
    """L'altération ne s'applique pas à la requête de la base."""


def _tokens(pointer: object) -> list[str]:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise AlterationError("un pointeur JSON commence par « / »")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer.split("/")[1:]]


def _child(target: Any, token: str) -> Any:
    if isinstance(target, list):
        if not token.isascii() or not token.isdigit() or int(token) >= len(target):
            raise AlterationError("le pointeur sort d'un tableau")
        return target[int(token)]
    if isinstance(target, dict) and token in target:
        return target[token]
    raise AlterationError("le pointeur ne désigne rien dans la requête de la base")


def _resolve(document: Any, pointer: object) -> Any:
    target = document
    for token in _tokens(pointer):
        target = _child(target, token)
    return target


def _put(document: Any, pointer: object, value: Any) -> None:
    """Pose une valeur : un champ existant est remplacé, une clé nouvelle est ajoutée."""
    tokens = _tokens(pointer)
    if not tokens:
        raise AlterationError("le pointeur désigne toute la requête")
    parent = document
    for token in tokens[:-1]:
        parent = _child(parent, token)
    last = tokens[-1]
    if isinstance(parent, list):
        _child(parent, last)
        parent[int(last)] = value
    elif isinstance(parent, dict):
        parent[last] = value
    else:
        raise AlterationError("le pointeur traverse une valeur simple")


def _number(params: Mapping[str, Any], name: str, *, minimum: int = 1) -> int:
    value = params[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AlterationError(f"« {name} » est un nombre entier d'au moins {minimum}")
    return value


def _text(params: Mapping[str, Any], name: str) -> str:
    value = params[name]
    if not isinstance(value, str) or not value:
        raise AlterationError(f"« {name} » est un texte non vide")
    return value


@dataclass
class _Draft:
    """La requête en cours d'altération, puis ce qui se fait sur ses octets."""

    document: dict[str, Any]
    settings: dict[str, Any]
    announced: bool = True
    slow: bool = False
    token: dict[str, str] | None = None
    # Rend le corps à partir du document, quand le JSON ordinaire ne le peut pas.
    render: Callable[[Any], bytes] | None = None
    # Transformations des octets du corps, dans l'ordre.
    wire: list[Callable[[bytes], bytes]] = field(default_factory=list)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _repeat(draft: _Draft, params: Mapping[str, Any]) -> None:
    count = _number(params, "count")
    target = _resolve(draft.document, params["field"])
    if "motif" in params:
        if not isinstance(target, str):
            raise AlterationError("un motif se répète à la suite d'un texte")
        _put(draft.document, params["field"], target + _text(params, "motif") * count)
    elif isinstance(target, list) and target:
        _put(
            draft.document, params["field"], [target[index % len(target)] for index in range(count)]
        )
    elif isinstance(target, dict) and target:
        key, value = next(iter(target.items()))
        _put(
            draft.document,
            params["field"],
            {**target, **{f"{key}_{rank}": value for rank in range(len(target), count)}},
        )
    else:
        raise AlterationError("seul un tableau ou un objet non vide se répète")


def _nest(draft: _Draft, params: Mapping[str, Any]) -> None:
    depth = _number(params, "depth")
    opening, closing = _text(params, "open") * depth, _text(params, "close") * depth
    if params["field"] == "/body":
        draft.wire.append(lambda body: opening.encode() + body + closing.encode())
        return
    target = _resolve(draft.document, params["field"])
    if not isinstance(target, str):
        raise AlterationError("seul un texte, ou le corps entier, s'imbrique")
    _put(draft.document, params["field"], opening + target + closing)


def _padded(body: bytes, size: int, *, at_start: bool = False) -> bytes:
    if len(body) > size:
        raise AlterationError("le corps de la base dépasse déjà cette taille")
    padding = PADDING * (size - len(body))
    return padding + body if at_start else body + padding


def _inflate(draft: _Draft, params: Mapping[str, Any]) -> None:
    size = _number(params, "bytes")
    where = params.get("at", "fin")
    if where not in ("debut", "fin"):
        raise AlterationError("« at » vaut « debut » ou « fin »")
    announced = params.get("announce", True)
    if not isinstance(announced, bool):
        raise AlterationError("« announce » vaut true ou false")
    draft.announced = announced
    # Des blancs autour du document : il reste du JSON, seule sa taille change.
    draft.wire.append(lambda body: _padded(body, size, at_start=where == "debut"))


def _gzip(body: bytes) -> bytes:
    return gzip.compress(body, mtime=0)


def _compress(draft: _Draft, params: Mapping[str, Any]) -> None:
    draft.document["headers"]["content-encoding"] = "gzip"
    draft.wire.append(_gzip)


def _gzip_bomb(draft: _Draft, params: Mapping[str, Any]) -> None:
    size = _number(params, "bytes")
    draft.document["headers"]["content-encoding"] = "gzip"
    draft.wire.append(lambda body: _gzip(_padded(body, size)))


def _with_duplicate(value: Any, tokens: list[str], duplicate: Any) -> str:
    """Le JSON de ``value``, où la clé que désignent ``tokens`` figure une seconde fois."""
    if not tokens:
        raise AlterationError("le pointeur ne désigne pas une clé")
    if isinstance(value, list):
        _child(value, tokens[0])
        return (
            "["
            + ",".join(
                _with_duplicate(item, tokens[1:], duplicate)
                if str(rank) == tokens[0]
                else _dumps(item)
                for rank, item in enumerate(value)
            )
            + "]"
        )
    if not isinstance(value, dict) or tokens[0] not in value:
        raise AlterationError("le pointeur ne désigne aucune clé de la requête de la base")
    members = []
    for key, item in value.items():
        if key != tokens[0]:
            members.append(f"{_dumps(key)}:{_dumps(item)}")
        elif len(tokens) > 1:
            members.append(f"{_dumps(key)}:{_with_duplicate(item, tokens[1:], duplicate)}")
        else:
            # La valeur saine d'abord : un analyseur qui garde la dernière lirait l'autre.
            members.extend(f"{_dumps(key)}:{_dumps(each)}" for each in (item, duplicate))
    return "{" + ",".join(members) + "}"


def _duplicate_key(draft: _Draft, params: Mapping[str, Any]) -> None:
    tokens = _tokens(params["field"])
    if tokens[:1] != ["body"]:
        raise AlterationError("une clé se répète dans le corps")
    value = params["value"]
    _with_duplicate(draft.document["body"], tokens[1:], value)
    draft.render = lambda body: _with_duplicate(body, tokens[1:], value).encode()


def _move_host(draft: _Draft, params: Mapping[str, Any]) -> None:
    where = params["to"]
    if where not in RELAYED_HOST_HEADERS:
        raise AlterationError(
            "« to » nomme un en-tête de relais : " + ", ".join(RELAYED_HOST_HEADERS)
        )
    headers = draft.document["headers"]
    # L'hôte attendu passe dans l'en-tête de relais, un hôte étranger prend sa place.
    headers["host"] = _text(params, "host")
    headers[where] = f"host={LOCAL_HOST}" if where == "forwarded" else LOCAL_HOST


def encoded_header(value: str) -> str:
    """Valeur d'en-tête MCP sous sa forme encodée, ``=?base64?…?=``."""
    return "=?base64?" + base64.b64encode(value.encode()).decode("ascii") + "?="


def _encode_header(draft: _Draft, params: Mapping[str, Any]) -> None:
    draft.document["headers"][_text(params, "header").lower()] = encoded_header(
        _text(params, "value")
    )


def _slow_down(draft: _Draft, params: Mapping[str, Any]) -> None:
    draft.settings["read_timeout_ms"] = _number(params, "timeout_ms")
    draft.slow = True


# Les jetons qu'un appelant peut présenter à l'API à la place du sien, en liste fermée.
# Rien ici ne les fabrique : le corpus ne connaît ni l'émetteur ni ses clés.
FORGES: Final = (
    # Aucun en-tête Authorization.
    "absent",
    # Un jeton sans signature (« alg: none »).
    "alg-none",
    # Un HMAC signé avec la clé publique de l'émetteur : la confusion d'algorithme.
    "hs256-cle-publique",
    # Un jeton juste, signé par l'émetteur, pour un autre émetteur (iss) ou une autre audience.
    "emetteur-etranger",
    "audience-etrangere",
    # Un jeton juste, mais expiré, ou pas encore valide (nbf).
    "expire",
    "pas-encore-valide",
    # Un jeton signé d'une clé que l'émetteur ne publie pas (kid inconnu).
    "kid-inconnu",
    # Un jeton juste dont la signature est altérée.
    "signature-alteree",
    # Un jeton juste de plus de 16 Kio.
    "demesure",
    # Un jeton juste, d'une personne qui n'a que le rôle de lecteur.
    "lecteur-seul",
)


def _forge_token(draft: _Draft, params: Mapping[str, Any]) -> None:
    forge = params["forge"]
    if forge not in FORGES:
        raise AlterationError("contrefaçon inconnue : " + ", ".join(FORGES) + " sont admises")
    token = {"forge": str(forge)}
    if "subject" in params:
        token["subject"] = _text(params, "subject")
    draft.token = token


def _replay(draft: _Draft, params: Mapping[str, Any]) -> None:
    tokens = _tokens(params["event"])
    if len(tokens) != 2 or tokens[0] not in ("seed", "reports"):
        raise AlterationError(
            "« event » désigne un événement (« /seed/… ») ou un relevé (« /reports/… ») "
            "envoyé avant la lecture"
        )
    sent = copy.deepcopy(_resolve(draft.document, params["event"]))
    if "field" in params:
        _put(sent, params["field"], params.get("value"))
    draft.document[tokens[0]].append(sent)


@dataclass(frozen=True)
class Recipe:
    apply: Callable[[_Draft, Mapping[str, Any]], None]
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    # Paramètre qui porte une valeur hostile, où le canari doit figurer.
    hostile: str | None = None


# Liste fermée : une altération qui n'est ni un champ ni un en-tête est l'une d'elles.
RECIPES: Final[Mapping[str, Recipe]] = {
    # Répète le contenu d'un tableau ou le premier membre d'un objet jusqu'à en compter
    # ``count`` ; avec ``motif``, ajoute ce texte ``count`` fois à la suite d'un texte.
    "repeter": Recipe(_repeat, ("field", "count"), ("motif",)),
    # Entoure un texte, ou le corps entier (« /body »), de ``depth`` ouvrants et fermants.
    "imbriquer": Recipe(_nest, ("field", "open", "close", "depth")),
    # Porte le corps à ``bytes`` octets par des blancs, à la fin ou au début.
    "gonfler": Recipe(_inflate, ("bytes",), ("at", "announce")),
    # Compresse le corps en gzip et l'annonce.
    "compresser": Recipe(_compress),
    # Corps gzip de quelques Ko qui se déploie en ``bytes`` octets.
    "bombe-gzip": Recipe(_gzip_bomb, ("bytes",)),
    # Écrit deux fois une clé du corps : sa valeur saine, puis ``value``.
    "dupliquer-cle": Recipe(_duplicate_key, ("field", "value"), hostile="value"),
    # Présente un hôte étranger dans Host, et l'hôte attendu dans un en-tête de relais.
    "deplacer-hote": Recipe(_move_host, ("host", "to"), hostile="host"),
    # Donne à un en-tête MCP une valeur encodée en base64.
    "encoder-base64": Recipe(_encode_header, ("header", "value"), hostile="value"),
    # Retient la lecture par un verrou et abaisse son délai maximal.
    "ralentir": Recipe(_slow_down, ("timeout_ms",)),
    # Renvoie un événement ou un relevé déjà envoyé, tel quel ou avec un champ changé.
    "rejouer": Recipe(_replay, ("event",), ("field", "value")),
    # Présente un jeton contrefait à la place de celui de l'appelant ; ``subject`` en est le
    # sujet, porteur du canari.
    "forger-jeton": Recipe(_forge_token, ("forge",), ("subject",), hostile="subject"),
}


def kind_of(alter: Mapping[str, Any]) -> str:
    """« recipe », « field » ou « header » : une altération est exactement l'un des trois."""
    if "recipe" in alter:
        # Une recette nomme elle-même le champ ou l'en-tête qu'elle travaille.
        return "recipe"
    kinds = [kind for kind in ("field", "header") if kind in alter]
    if len(kinds) != 1:
        raise AlterationError(
            "une altération change un champ (« field »), un en-tête (« header ») "
            "ou applique une recette (« recipe »), et une seule chose"
        )
    return kinds[0]


def hostile_text(alter: Mapping[str, Any]) -> str | None:
    """Le texte où le canari doit figurer, ou ``None`` quand l'altération ne porte pas de valeur."""
    kind = kind_of(alter)
    if kind == "recipe":
        recipe = RECIPES.get(alter["recipe"]) if isinstance(alter["recipe"], str) else None
        if recipe is None or recipe.hostile is None or recipe.hostile not in alter:
            return None
        return _dumps(alter.get(recipe.hostile))
    value = alter.get("value")
    if value is None or isinstance(value, bool | int | float):
        # Un nombre ne porte pas de marque : il n'y a rien à chercher ensuite.
        return None
    return _dumps([alter[kind], value])


def _alter(draft: _Draft, alter: Mapping[str, Any]) -> None:
    kind = kind_of(alter)
    if kind == "recipe":
        name = alter["recipe"]
        recipe = RECIPES.get(name) if isinstance(name, str) else None
        if recipe is None:
            raise AlterationError("recette inconnue : " + ", ".join(RECIPES) + " sont admises")
        params = {key: value for key, value in alter.items() if key != "recipe"}
        missing = [name for name in recipe.required if name not in params]
        unknown = [name for name in params if name not in (*recipe.required, *recipe.optional)]
        if missing or unknown:
            raise AlterationError(
                "paramètres de la recette : "
                + ", ".join(
                    [
                        *(f"« {name} » manque" for name in missing),
                        *(f"« {name} » inconnu" for name in unknown),
                    ]
                )
            )
        recipe.apply(draft, params)
        return
    if set(alter) != {kind, "value"}:
        raise AlterationError(
            f"une altération de « {kind} » porte ce nom et sa valeur, rien d'autre"
        )
    if kind == "header":
        if not isinstance(alter["header"], str) or not isinstance(alter["value"], str):
            raise AlterationError("un en-tête et sa valeur sont des textes")
        draft.document["headers"][alter["header"].lower()] = alter["value"]
    else:
        _put(draft.document, alter["field"], alter["value"])


def _draft(
    request: Mapping[str, Any],
    seed: tuple[Mapping[str, Any], ...],
    settings: Mapping[str, Any],
    alter: Mapping[str, Any] | None,
    reports: tuple[Mapping[str, Any], ...],
    incidents: tuple[Mapping[str, Any], ...],
    proposals: tuple[Mapping[str, Any], ...] = (),
) -> _Draft:
    document = copy.deepcopy(dict(request))
    document.setdefault("headers", {})
    document.setdefault("query", {})
    document["seed"] = [copy.deepcopy(dict(event)) for event in seed]
    document["reports"] = [copy.deepcopy(dict(report)) for report in reports]
    document["incidents"] = [copy.deepcopy(dict(incident)) for incident in incidents]
    document["proposals"] = [copy.deepcopy(dict(event)) for event in proposals]
    draft = _Draft(document=document, settings=copy.deepcopy(dict(settings)))
    if alter is not None:
        _alter(draft, alter)
    return draft


def check_alteration(
    request: Mapping[str, Any],
    seed: tuple[Mapping[str, Any], ...],
    settings: Mapping[str, Any],
    alter: Mapping[str, Any],
    *,
    reports: tuple[Mapping[str, Any], ...] = (),
    incidents: tuple[Mapping[str, Any], ...] = (),
    proposals: tuple[Mapping[str, Any], ...] = (),
) -> None:
    """Lève ``AlterationError`` si l'altération ne s'applique pas ; ne fabrique aucun corps."""
    _draft(request, seed, settings, alter, reports, incidents, proposals)


def build(
    request: Mapping[str, Any],
    seed: tuple[Mapping[str, Any], ...] = (),
    settings: Mapping[str, Any] | None = None,
    alter: Mapping[str, Any] | None = None,
    *,
    reports: tuple[Mapping[str, Any], ...] = (),
    incidents: tuple[Mapping[str, Any], ...] = (),
    proposals: tuple[Mapping[str, Any], ...] = (),
    fragment: bool = False,
) -> Play:
    """La requête à jouer. ``fragment`` : le chemin est celui d'une adresse de l'explorateur."""
    draft = _draft(request, seed, settings or {}, alter, reports, incidents, proposals)
    document = draft.document
    body = b""
    if "body" in document:
        body = (
            draft.render(document["body"])
            if draft.render is not None
            else _dumps(document["body"]).encode()
        )
    for transform in draft.wire:
        body = transform(body)
    path = str(document["path"])
    query = document.get("query")
    if query:
        path += "?" + urlencode(query)
    return Play(
        method=str(document.get("method", "GET")),
        path="#" + path if fragment else path,
        headers=tuple((str(name), str(value)) for name, value in document["headers"].items()),
        body=body,
        announced=draft.announced,
        seed=tuple(document["seed"]),
        reports=tuple(document["reports"]),
        incidents=tuple(document["incidents"]),
        proposals=tuple(document["proposals"]),
        settings=draft.settings,
        slow=draft.slow,
        token=draft.token,
    )
