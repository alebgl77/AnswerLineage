"""Règles de lecture déclarées, pour les outils dont aucun argument ne porte de SQL.

Un outil d'API, de fichiers ou de recherche documentaire ne dit pas ce qu'il lit.
Une règle le déclare : quel outil, sur quel serveur ; quels jeux de données il
lit, et éventuellement quelles colonnes ; quel argument porte la clé lue.

```yaml
version: 1
rules:
  - tool: get_price
    server: catalogue
    datasets:
      - namespace: postgres://referentiel:5432
        name: ref.tarifs
        columns: [produit, prix]
    key: produit
```

Le fichier est une entrée hostile comme une autre :

- sa taille est bornée avant toute lecture, puis le nombre de règles, de jeux de
  données, de colonnes, la longueur du chemin de la clé et la profondeur ;
- il est lu par un chargeur sûr, qui refuse ancres, alias et étiquettes
  explicites avant de rien construire, et ne construit d'ailleurs rien : le
  document est parcouru nœud par nœud, et une valeur qui n'a pas le type attendu
  (une date ou un nombre là où un nom est attendu) est refusée, pas convertie ;
- un nom d'outil, de serveur ou d'argument suit la forme du proxy, un nom de jeu
  de données ou de colonne celle de l'ingestion ;
- une erreur nomme la règle et le champ fautifs, et leur position, sans jamais
  recopier une valeur du fichier ; d'une erreur de syntaxe YAML, dont le message
  citerait le texte fautif, seule la position est gardée.

Une règle invalide empêche le démarrage du proxy : on ne capte pas à moitié en
silence.

La clé lue est une valeur d'argument : elle n'est jamais conservée telle quelle.
Elle devient une empreinte à clé, la même fonction que les autres empreintes du
proxy (``fingerprint.py``), sur l'étiquette ``answerlineage/fingerprint/key/v1``
et la forme canonique JSON de la valeur. Sans clé de locataire, ou sans valeur
lisible, la lecture est déclarée au niveau du jeu de données, et l'événement dit
pourquoi.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import yaml

from answerlineage_proxy.fingerprint import FingerprintKey, canonical_json
from answerlineage_proxy.names import (
    COLUMN_MAX,
    DATASET_NAME_MAX,
    NAMESPACE_MAX,
    name_fault,
    normalize_name,
)

FORMAT_VERSION: Final = 1
KEY_LABEL: Final = b"answerlineage/fingerprint/key/v1"

# Bornes du fichier et de ce qu'il déclare.
MAX_FILE_BYTES: Final = 256 * 1024
MAX_DEPTH: Final = 8
MAX_RULES: Final = 1_000
MAX_DATASETS: Final = 32
MAX_COLUMNS: Final = 1_000
MAX_KEY_PATH: Final = 8
MAX_KEY_INDEX: Final = 9_999
# Clés lues dans un appel : au-delà, aucune n'est gardée, la lecture vaut pour tout
# le jeu de données. En garder une partie ferait écarter à tort un appel touché.
MAX_KEYS: Final = 100

# Ce que l'événement dit de la clé lue (``readKeys``).
ReadKeys = Literal[
    "fingerprinted",
    "no_fingerprint_key",
    "request_too_large",
    "missing_argument",
    "unsupported_argument",
    "too_many_keys",
]

_TOOL = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SERVER = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_ARGUMENT = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_INDEX = re.compile(r"0|[1-9][0-9]{0,3}")
_STR = "tag:yaml.org,2002:str"
_INT = "tag:yaml.org,2002:int"
_MISSING: Final = object()

KeyPath = tuple[str | int, ...]


class ReadRulesError(ValueError):
    """Le fichier des règles de lecture est illisible ou invalide. Le message ne cite
    aucune valeur du fichier."""


@dataclass(frozen=True)
class DeclaredDataset:
    """Un jeu de données qu'un outil lit, et les colonnes lues quand la règle les nomme."""

    namespace: str
    name: str
    # Absentes : la lecture porte sur tout le jeu de données.
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadRule:
    """Ce qu'un outil lit, d'après une règle déclarée."""

    tool: str
    datasets: tuple[DeclaredDataset, ...]
    server: str | None = None
    # Chemin, dans les arguments de l'appel, de la valeur qui porte la clé lue.
    key: KeyPath | None = None


@dataclass(frozen=True)
class DeclaredReads:
    """Les lectures d'un appel décrites par sa règle : jeux de données et clés lues."""

    datasets: tuple[DeclaredDataset, ...]
    # Empreintes à clé des clés lues, sans doublon.
    keys: tuple[str, ...] = ()
    # Absent quand la règle ne nomme pas d'argument de clé.
    key_status: ReadKeys | None = None


def _position(mark: Any) -> str:
    return f"ligne {mark.line + 1}, colonne {mark.column + 1}"


class _SafeComposer(yaml.SafeLoader):
    """Le chargeur sûr de PyYAML, qui refuse en plus ancres, alias et étiquettes
    explicites, et borne la profondeur, avant de composer le moindre nœud."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self._depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()  # type: ignore[no-untyped-call]
        where = _position(event.start_mark)
        if isinstance(event, yaml.AliasEvent):
            raise ReadRulesError(f"{where} : un alias (*) n'est pas admis")
        if getattr(event, "anchor", None) is not None:
            raise ReadRulesError(f"{where} : une ancre (&) n'est pas admise")
        if getattr(event, "tag", None) is not None:
            raise ReadRulesError(f"{where} : une étiquette explicite (!) n'est pas admise")
        self._depth += 1
        try:
            if self._depth > MAX_DEPTH:
                raise ReadRulesError(f"{where} : plus de {MAX_DEPTH} niveaux d'imbrication")
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _compose(text: str) -> yaml.Node | None:
    composer: _SafeComposer | None = None
    try:
        # La lecture refuse dès la construction un caractère non imprimable.
        composer = _SafeComposer(text)
        return composer.get_single_node()
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        where = _position(mark) if mark is not None else "position inconnue"
        # Le message de PyYAML citerait le texte fautif : seuls la position et le genre
        # de l'erreur sont gardés.
        raise ReadRulesError(f"{where} : syntaxe YAML invalide ({type(error).__name__})") from None
    except yaml.reader.ReaderError as error:
        raise ReadRulesError(
            f"caractère {error.position + 1} : caractère non admis dans un fichier YAML"
        ) from None
    finally:
        if composer is not None:
            composer.dispose()


class _Field:
    """Un champ du fichier : où il est, pour une erreur qui le désigne sans le citer."""

    def __init__(self, rule: int | None, path: str) -> None:
        self.rule = rule
        self.path = path

    def at(self, segment: str | int) -> _Field:
        suffix = f"[{segment}]" if isinstance(segment, int) else f".{segment}"
        return _Field(self.rule, f"{self.path}{suffix}" if self.path else str(segment))

    def error(self, node: yaml.Node, reason: str) -> ReadRulesError:
        names = [f"règle {self.rule + 1}"] if self.rule is not None else []
        if self.path:
            names.append(f"champ {self.path}")
        where = _position(node.start_mark)
        located = f"{', '.join(names)} ({where})" if names else where
        return ReadRulesError(f"{located} : {reason}")


def _mapping(
    node: yaml.Node, field: _Field, allowed: frozenset[str], required: frozenset[str]
) -> dict[str, yaml.Node]:
    if not isinstance(node, yaml.MappingNode):
        raise field.error(node, "un objet est attendu")
    members: dict[str, yaml.Node] = {}
    for key, value in node.value:
        if not (isinstance(key, yaml.ScalarNode) and key.tag == _STR and key.value in allowed):
            # Le nom d'un champ inconnu n'est pas recopié : sa position le désigne.
            raise field.error(key, "champ inconnu")
        if key.value in members:
            raise field.at(key.value).error(key, "champ répété")
        members[key.value] = value
    missing = sorted(required - members.keys())
    if missing:
        raise field.at(missing[0]).error(node, "champ obligatoire absent")
    return members


def _sequence(node: yaml.Node, field: _Field, bound: int) -> list[yaml.Node]:
    if not isinstance(node, yaml.SequenceNode):
        raise field.error(node, "une liste est attendue")
    if not 1 <= len(node.value) <= bound:
        raise field.error(node, f"une liste de 1 à {bound} éléments est attendue")
    items: list[yaml.Node] = node.value
    return items


def _text(node: yaml.Node, field: _Field) -> str:
    """Un texte : une date, un nombre ou un booléen n'en sont pas, et ne sont pas convertis."""
    if not (isinstance(node, yaml.ScalarNode) and node.tag == _STR):
        raise field.error(node, "un texte est attendu")
    text: str = node.value
    return text


def _form(node: yaml.Node, field: _Field, pattern: re.Pattern[str], what: str) -> str:
    text = _text(node, field)
    if not pattern.fullmatch(text):
        raise field.error(node, f"{what} compte 1 à 128 lettres, chiffres, « _ », « - » ou « . »")
    return text


def _name(node: yaml.Node, field: _Field, max_length: int) -> str:
    text = _text(node, field)
    fault = name_fault(text, max_length=max_length)
    if fault is not None:
        raise field.error(node, f"{fault.message} ({fault.code})")
    return normalize_name(text)


_DATASET_FIELDS: Final = frozenset({"namespace", "name", "columns"})


def _dataset(node: yaml.Node, field: _Field) -> DeclaredDataset:
    members = _mapping(node, field, _DATASET_FIELDS, frozenset({"namespace", "name"}))
    columns: list[str] = []
    if "columns" in members:
        listed = field.at("columns")
        for rank, item in enumerate(_sequence(members["columns"], listed, MAX_COLUMNS)):
            column = _name(item, listed.at(rank), COLUMN_MAX)
            if column in columns:
                raise listed.at(rank).error(item, "colonne répétée")
            columns.append(column)
    return DeclaredDataset(
        namespace=_name(members["namespace"], field.at("namespace"), NAMESPACE_MAX),
        name=_name(members["name"], field.at("name"), DATASET_NAME_MAX),
        columns=tuple(columns),
    )


def _segment(node: yaml.Node, field: _Field) -> str | int:
    if isinstance(node, yaml.ScalarNode) and node.tag == _INT:
        if not _INDEX.fullmatch(node.value):
            raise field.error(node, f"un rang de liste va de 0 à {MAX_KEY_INDEX}, en décimal")
        return int(node.value)
    return _form(node, field, _ARGUMENT, "un nom d'argument")


def _key(node: yaml.Node, field: _Field) -> KeyPath:
    if isinstance(node, yaml.SequenceNode):
        segments = _sequence(node, field, MAX_KEY_PATH)
        path = tuple(_segment(item, field.at(rank)) for rank, item in enumerate(segments))
        if isinstance(path[0], int):
            raise field.at(0).error(segments[0], "le chemin commence par un nom d'argument")
        return path
    return (_form(node, field, _ARGUMENT, "un nom d'argument"),)


_RULE_FIELDS: Final = frozenset({"tool", "server", "datasets", "key"})


def _rule(node: yaml.Node, index: int) -> ReadRule:
    field = _Field(index, "")
    members = _mapping(node, field, _RULE_FIELDS, frozenset({"tool", "datasets"}))
    listed = field.at("datasets")
    datasets = tuple(
        _dataset(item, listed.at(rank))
        for rank, item in enumerate(_sequence(members["datasets"], listed, MAX_DATASETS))
    )
    return ReadRule(
        tool=_form(members["tool"], field.at("tool"), _TOOL, "un nom d'outil"),
        server=(
            _form(members["server"], field.at("server"), _SERVER, "un nom de serveur")
            if "server" in members
            else None
        ),
        datasets=datasets,
        key=_key(members["key"], field.at("key")) if "key" in members else None,
    )


_FILE_FIELDS: Final = frozenset({"version", "rules"})


def parse_read_rules(text: str) -> tuple[ReadRule, ...]:
    """Lit les règles d'un document YAML. Lève ``ReadRulesError`` au premier défaut."""
    root = _compose(text)
    if root is None:
        raise ReadRulesError("le fichier est vide")
    top = _Field(None, "")
    members = _mapping(root, top, _FILE_FIELDS, _FILE_FIELDS)
    version = members["version"]
    if not (
        isinstance(version, yaml.ScalarNode)
        and version.tag == _INT
        and version.value == str(FORMAT_VERSION)
    ):
        raise top.at("version").error(version, f"la version du format est {FORMAT_VERSION}")
    rules = tuple(
        _rule(item, index)
        for index, item in enumerate(_sequence(members["rules"], top.at("rules"), MAX_RULES))
    )
    seen: dict[tuple[str | None, str], int] = {}
    for index, rule in enumerate(rules):
        target = (rule.server, rule.tool)
        if target in seen:
            raise ReadRulesError(
                f"règle {index + 1} : même outil, sur le même serveur, que la règle "
                f"{seen[target] + 1}"
            )
        seen[target] = index
    return rules


def load_read_rules(path: str | Path) -> tuple[ReadRule, ...]:
    """Lit le fichier des règles. Sa taille est bornée avant lecture ; aucun message
    d'erreur ne cite son contenu."""
    try:
        with Path(path).open("rb") as source:
            content = source.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise ReadRulesError("le fichier est illisible") from None
    if len(content) > MAX_FILE_BYTES:
        raise ReadRulesError(f"le fichier dépasse {MAX_FILE_BYTES // 1024} Kio")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ReadRulesError("le fichier n'est pas écrit en UTF-8") from None
    return parse_read_rules(text)


def rules_for(rules: tuple[ReadRule, ...], server: str) -> dict[str, ReadRule]:
    """Les règles qui valent pour ce serveur, par outil : une règle qui nomme le serveur
    l'emporte sur une règle qui vaut pour tous."""
    chosen: dict[str, ReadRule] = {}
    for rule in rules:
        if rule.server is None:
            chosen.setdefault(rule.tool, rule)
        elif rule.server == server:
            chosen[rule.tool] = rule
    return chosen


def _lookup(arguments: object, path: KeyPath) -> object:
    value = arguments
    for segment in path:
        if isinstance(segment, int):
            value = value[segment] if isinstance(value, list) and segment < len(value) else _MISSING
        else:
            value = value.get(segment, _MISSING) if isinstance(value, dict) else _MISSING
        if value is _MISSING:
            break
    return value


def _scalar(value: object) -> bool:
    return isinstance(value, str | int | float | bool)


def declared_reads(
    rule: ReadRule,
    arguments: object,
    *,
    readable: bool,
    key: FingerprintKey | None,
) -> DeclaredReads:
    """Les lectures d'un appel d'après sa règle, et l'empreinte de la clé lue.

    ``readable`` dit si les arguments de l'appel ont été lus ; sans clé de
    locataire, ils ne sont pas gardés. Une valeur absente, nulle, vide, qui
    n'est ni un scalaire ni une liste de scalaires, ou trop de clés : aucune clé
    n'est gardée, la lecture vaut pour tout le jeu de données, et le motif le dit.
    """
    datasets = rule.datasets
    if rule.key is None:
        return DeclaredReads(datasets)
    if key is None:
        return DeclaredReads(datasets, key_status="no_fingerprint_key")
    if not readable:
        return DeclaredReads(datasets, key_status="request_too_large")
    value = _lookup(arguments, rule.key)
    values = value if isinstance(value, list) else [value]
    if value is _MISSING or value is None or not values:
        return DeclaredReads(datasets, key_status="missing_argument")
    if not all(_scalar(item) for item in values):
        return DeclaredReads(datasets, key_status="unsupported_argument")
    if len(values) > MAX_KEYS:
        return DeclaredReads(datasets, key_status="too_many_keys")
    encoded = [canonical_json(item) for item in values]
    if any(item is None for item in encoded):
        return DeclaredReads(datasets, key_status="unsupported_argument")
    fingerprints = dict.fromkeys(key.fingerprint(KEY_LABEL, item) for item in encoded if item)
    return DeclaredReads(datasets, tuple(fingerprints), "fingerprinted")
