"""Le commentaire SQL qui porte l'identifiant de run, écrit en tête de la requête relayée.

Sur option et par outil (``--sql-run-comment``), le proxy écrit au début de la
requête SQL d'un appel d'outil le commentaire ``/* answerlineage_run_id=<run> */``,
suivi d'une espace : l'entrepôt garde le texte des requêtes dans ses journaux
d'audit, où les lectures se rapprochent ainsi de leur run.

Seule la chaîne de la requête change. Le corps n'est jamais lu puis réécrit : le
proxy y trouve la position du premier caractère de la requête, et recopie tels
quels les octets qui la précèdent et ceux qui la suivent, l'ordre des clés, les
blancs et les échappements compris. Le commentaire n'est fait que de caractères
ASCII qu'une chaîne JSON porte sans échappement.

Le commentaire vient d'un gabarit fixe, où n'entre que l'identifiant de run, sous
sa forme canonique vérifiée : jamais une valeur reçue. Il ne contient donc ni
``*/`` qui le fermerait plus tôt, ni ``/*`` qu'un dialecte à commentaires
imbriqués ouvrirait.

Le corps est relu comme l'inspection le lit, sans ambiguïté (pas de clé répétée,
pas de lot), et en UTF-8 strict : ce qui ne se lit pas ainsi avec certitude n'est
pas modifié.
"""

from __future__ import annotations

import json
import re
from typing import Final, Literal
from uuid import UUID

from answerlineage_proxy.inspection import parse_message
from answerlineage_proxy.warehouse import WarehouseTags

# Pourquoi une requête n'a pas reçu son commentaire : le corps n'est pas de l'UTF-8 lisible
# tel quel, l'argument SQL manque ou n'est pas une chaîne, la requête est vide, ou elle
# commence par une directive que le commentaire priverait d'effet.
Untagged = Literal["unreadable", "missing_argument", "empty", "leading_directive"]

# Le chemin de la requête dans un appel d'outil : params.arguments.<argument>.
_PARAMS: Final = "params"
_ARGUMENTS: Final = "arguments"
# Blancs de JSON (RFC 8259, section 2).
_WHITESPACE: Final = re.compile(r"[ \t\n\r]*")
# Débuts qu'un commentaire placé devant changerait : la directive de dialecte de BigQuery
# (#standardSQL, #legacySQL) n'est lue qu'en première ligne, et un indice d'optimiseur
# (/*+ … */) n'est lu, par pg_hint_plan, que dans le premier commentaire de la requête.
_DIRECTIVES: Final = ("#", "/*+")
_DECODER: Final = json.JSONDecoder()


def comment_query(body: bytes, argument: str, run_id: UUID) -> bytes | Untagged:
    """Le corps d'un appel d'outil dont la requête SQL commence par le commentaire de son run.

    ``argument`` est l'argument de l'outil qui porte la requête. Rend le motif du
    refus quand le corps ne peut pas être étiqueté : il est alors relayé tel quel.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return "unreadable"
    message = parse_message(body)
    if not isinstance(message, dict):
        return "unreadable"
    params = message.get(_PARAMS)
    arguments = params.get(_ARGUMENTS) if isinstance(params, dict) else None
    query = arguments.get(argument) if isinstance(arguments, dict) else None
    if not isinstance(query, str):
        return "missing_argument"
    try:
        start = _string_at(text, (_PARAMS, _ARGUMENTS, argument))
        if _DECODER.raw_decode(text, start)[0] != query:  # pragma: no cover — garde-fou.
            # Le chemin suivi et le document lu désignent la même chaîne ; s'ils divergeaient,
            # rien ne serait écrit, plutôt qu'ailleurs que dans la requête.
            raise ValueError("requête attendue")
    except (ValueError, RecursionError):
        return "unreadable"
    if not query.strip():
        return "empty"
    if query.lstrip().startswith(_DIRECTIVES):
        return "leading_directive"
    comment = WarehouseTags(str(run_id)).sql_comment.encode("ascii")
    # Le premier caractère de la requête, compté en octets : tout ce qui le précède et tout
    # ce qui le suit est recopié à l'octet près.
    offset = len(text[: start + 1].encode("utf-8"))
    return body[:offset] + comment + body[offset:]


def _string_at(text: str, path: tuple[str, ...]) -> int:
    """Position du guillemet ouvrant de la chaîne au bout de ``path``, d'objet en objet.

    Le chemin ne se suit que sur la structure exacte du document : tout écart (un
    document qui n'est pas un objet, une marque d'ordre des octets en tête, un membre
    absent, une valeur qui n'est pas une chaîne) lève ``ValueError``.
    """
    index = _skip(text, 0)
    for key in path:
        index = _member(text, index, key)
    if not text.startswith('"', index):
        raise ValueError("chaîne attendue")
    return index


def _member(text: str, index: int, key: str) -> int:
    """Position de la valeur du membre ``key`` de l'objet qui commence à ``index``.

    Les clés sont comparées une fois décodées : ``"p\\u0061rams"`` est ``params``, comme
    pour le serveur. Les valeurs des autres membres sont sautées par le lecteur JSON de
    la bibliothèque standard.
    """
    if not text.startswith("{", index):
        raise ValueError("objet attendu")
    separator = "{"
    while text.startswith(separator, index):
        index = _skip(text, index + 1)
        if not text.startswith('"', index):
            raise ValueError("clé attendue")
        name, index = _DECODER.raw_decode(text, index)
        index = _skip(text, index)
        if not text.startswith(":", index):
            raise ValueError("deux-points attendus")
        index = _skip(text, index + 1)
        if name == key:
            return index
        _, index = _DECODER.raw_decode(text, index)
        index = _skip(text, index)
        separator = ","
    raise ValueError("membre absent")


def _skip(text: str, index: int) -> int:
    """Position du premier caractère qui n'est pas un blanc de JSON, à partir de ``index``."""
    blank = _WHITESPACE.match(text, index)
    return blank.end() if blank is not None else index
