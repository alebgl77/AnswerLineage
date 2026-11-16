"""Validation d'une réponse de l'API, champ par champ, avant tout affichage.

Chaque fonction rend la valeur validée ou lève ``InvalidResponseError`` avec
l'endroit fautif, écrit avec les clés du contrat et les rangs
(``/graph/answers/0/agent``), jamais avec ce qui s'y trouvait. Ce qui est
rendu ne contient que les champs du contrat : un champ inconnu n'est ni
affiché ni recopié dans la sortie JSON.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any, Final

from answerlineage_cli.client import InvalidResponseError
from answerlineage_cli.names import is_identifier

# Un décompte de l'API tient dans un entier de PostgreSQL.
MAX_COUNT: Final = 2**63 - 1
_INSTANT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")


def fault(where: str) -> InvalidResponseError:
    return InvalidResponseError("un champ manque ou n'a pas la forme attendue", where)


def obj(
    value: Any, where: str, required: Sequence[str], optional: Sequence[str] = ()
) -> dict[str, Any]:
    """Un objet qui porte au moins ses champs obligatoires ; les autres champs sont ignorés."""
    if not isinstance(value, dict):
        raise fault(where)
    for key in required:
        if key not in value:
            raise fault(f"{where}/{key}")
    return {key: value[key] for key in (*required, *optional) if key in value}


def items(value: Any, where: str, max_items: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > max_items:
        raise fault(where)
    return value


def flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise fault(where)
    return value


def count(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
        raise fault(where)
    return value


def optional_count(value: Any, where: str) -> int | None:
    return None if value is None else count(value, where)


def rate(value: Any, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise fault(where)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise fault(where)
    return float(value)


def choice(value: Any, where: str, admitted: Collection[str]) -> str:
    if not isinstance(value, str) or value not in admitted:
        raise fault(where)
    return value


def identifier(value: Any, where: str, max_length: int) -> str:
    if not isinstance(value, str) or not is_identifier(value, max_length=max_length):
        raise fault(where)
    return value


def pattern(value: Any, where: str, form: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not form.fullmatch(value):
        raise fault(where)
    return value


def instant(value: Any, where: str) -> str:
    """Une date et une heure RFC 3339, avec leur fuseau, telles que l'API les écrit."""
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        raise fault(where)
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise fault(where) from None
    return value


def version(value: Any, where: str) -> str:
    return pattern(value, where, _VERSION)
