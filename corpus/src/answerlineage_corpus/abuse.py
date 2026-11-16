"""Corpus d'abus : des cas d'attaque, et ce que « bloqué » veut dire pour chacun.

Un cas part d'une requête saine nommée (sa base), lui applique une seule
altération et déclare son attente : un verdict et son code. L'attente est
écrite à la main, dans le fichier de la famille du cas. Elle sert à mesurer le
produit, et ne se calcule donc jamais avec lui : rien ici n'importe un
validateur d'AnswerLineage.

« Bloqué » dépend de la surface visée :

- ``proxy`` : la capture ne bloque jamais un agent. ``neutralise`` : l'appel
  passe, l'événement marque l'anomalie sans porter la valeur ; ``compte`` : la
  requête passe sans événement, comptée comme non inspectée ; ``refuse`` : pour
  Host, Origin et les requêtes d'une autre page seulement ;
- ``ingestion`` : ``refuse`` pour le corps entier ; ``rejete`` pour un
  événement, avec son chemin et son code ; ``sans_effet`` quand la requête
  altérée vaut la requête saine ;
- ``lecture`` : ``refuse`` ; ``borne`` pour une réponse complète ou dite
  tronquée, ou pour le refus du délai ; ``sans_effet`` ;
- ``explorateur`` : ``inerte``, quand aucun nœud ne porte la valeur, que la
  console reste sans erreur et qu'un message fixe s'affiche.

Un cas que le produit ne bloque pas encore reste au corpus : il est déclaré
ouvert, avec son motif, dans le seul fichier d'exceptions.

Le contrôle du corpus refuse un cas sans attente ou hors des listes fermées,
une famille exigée qui compte moins de trois cas bloqués, une exception sans
motif, un caractère invisible écrit tel quel, et un contenu qui change sans
nouvelle version.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Final

from answerlineage_corpus.abuse_requests import (
    AlterationError,
    Play,
    build,
    check_alteration,
    encoded_header,
    hostile_text,
)
from answerlineage_corpus.scenarios import CorpusError

# Change dès qu'un cas, une base ou une exception change : affaiblir le corpus
# sans le dire échoue au contrôle.
ABUSE_VERSION = "2"
_DIGESTS = {
    "1": "a6e3bdbb6724874d86cde209f8549640b68bce9d76e097da185ea54284a05619",
    # La lecture antidatée est bloquée : son exception est retirée.
    "2": "36e95e6e85984cee01b5119f779dab4892af6f1e69f9b6423da8efb46ecc1a95",
}

# Marque que porte toute valeur hostile, cherchée ensuite partout : réponses,
# base, événements, journaux. Ni mot réel, ni suite hexadécimale.
CANARY: Final = "canari7c1d"

SURFACES: Final = ("proxy", "ingestion", "lecture", "explorateur")
VERDICTS: Final[Mapping[str, tuple[str, ...]]] = {
    "proxy": ("neutralise", "compte", "refuse"),
    "ingestion": ("refuse", "rejete", "sans_effet"),
    "lecture": ("refuse", "borne", "sans_effet"),
    "explorateur": ("inerte",),
}
# Familles exigées, et le nombre de cas bloqués que chacune doit compter.
FAMILIES: Final = ("identifiants", "sql", "lots", "invisibles", "emetteur", "entetes", "capture")
MIN_BLOCKED: Final = 3

# Ce à quoi se reconnaît une requête neutralisée par le proxy, dans son événement.
NEUTRALISED: Final = ("invalidToolName", "headerMismatch", "unresolved")
UNINSPECTED: Final = "uninspected"
# La capture ne bloque jamais un agent : le proxy ne refuse que ces trois cas.
PROXY_REFUSALS: Final = ("host_not_allowed", "origin_not_allowed", "cross_site_not_allowed")
_REFUSAL_STATUSES: Final[Mapping[str, tuple[int, ...]]] = {
    "proxy": (403,),
    "ingestion": (400, 403, 413, 415),
    "lecture": (403, 404, 422),
}

BASES_FILE: Final = "abuse_bases.json"
OPEN_FILE: Final = "abuse_open.json"
CASES_DIRECTORY: Final = "abuse_data"

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_CASE_FIELDS: Final = ("name", "title", "surface", "base", "alter", "expect", "canary")
# Lettres sans rendu, que leur catégorie Unicode ne distingue pas des autres.
_BLANK_LETTERS: Final = frozenset("\u115f\u1160\u2800\u3164\uffa0")


@dataclass(frozen=True)
class Base:
    """Une requête saine : elle doit passer, et ``healthy`` dit à quoi cela se voit."""

    name: str
    title: str
    surface: str
    request: Mapping[str, Any] = field(hash=False)
    healthy: Mapping[str, Any] = field(hash=False)
    # Événements envoyés à l'ingestion avant une lecture, puis relevés de couverture.
    seed: tuple[Mapping[str, Any], ...] = field(default=(), hash=False)
    reports: tuple[Mapping[str, Any], ...] = field(default=(), hash=False)
    settings: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class Case:
    family: str
    name: str
    title: str
    surface: str
    base: Base
    alter: Mapping[str, Any] = field(hash=False)
    expect: Mapping[str, Any] = field(hash=False)
    canary: str = CANARY
    # Motif de l'exception, quand le cas est ouvert : le produit ne le bloque pas encore.
    open_reason: str | None = None

    @property
    def id(self) -> str:
        return f"{self.family}/{self.name}"

    @property
    def verdict(self) -> str:
        return str(self.expect["verdict"])

    @property
    def is_open(self) -> bool:
        return self.open_reason is not None


@dataclass(frozen=True)
class AbuseCorpus:
    version: str
    bases: tuple[Base, ...]
    cases: tuple[Case, ...]

    def __iter__(self) -> Iterator[Case]:
        return iter(self.cases)

    @property
    def open_cases(self) -> tuple[Case, ...]:
        return tuple(case for case in self.cases if case.is_open)


def _play(base: Base, alter: Mapping[str, Any] | None) -> Play:
    return build(
        base.request,
        base.seed,
        base.settings,
        alter,
        reports=base.reports,
        fragment=base.surface == "explorateur",
    )


def healthy_play(base: Base) -> Play:
    """La requête saine de la base, telle qu'elle part."""
    return _play(base, None)


def play_of(case: Case) -> Play:
    """La requête du cas : celle de sa base, après son altération."""
    return _play(case.base, case.alter)


def needles(case: Case) -> tuple[str, ...]:
    """Ce qu'il faut chercher après le passage du cas : le canari, sous chaque forme envoyée."""
    if case.canary not in json.dumps(case.alter, ensure_ascii=False):
        return ()
    if case.alter.get("recipe") == "encoder-base64":
        return (case.canary, encoded_header(str(case.alter["value"])))
    return (case.canary,)


def abuse_texts(directory: Path | None = None) -> dict[str, str]:
    """Les fichiers du corpus tels qu'ils sont écrits, par nom ; ceux du paquet par défaut."""
    if directory is not None:
        texts = {
            name: (directory / name).read_text(encoding="utf-8") for name in (BASES_FILE, OPEN_FILE)
        }
        families = {
            f"{CASES_DIRECTORY}/{path.name}": path.read_text(encoding="utf-8")
            for path in (directory / CASES_DIRECTORY).glob("*.json")
        }
    else:
        package = resources.files("answerlineage_corpus")
        texts = {
            name: (package / name).read_text(encoding="utf-8") for name in (BASES_FILE, OPEN_FILE)
        }
        families = {
            f"{CASES_DIRECTORY}/{entry.name}": entry.read_text(encoding="utf-8")
            for entry in (package / CASES_DIRECTORY).iterdir()
            if entry.name.endswith(".json")
        }
    return texts | {name: families[name] for name in sorted(families)}


def _unambiguous(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    members = dict(pairs)
    if len(members) != len(pairs):
        # Une clé écrite deux fois cacherait la première valeur à la revue.
        raise ValueError("clé répétée")
    return members


def _parsed(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unambiguous)


def _plainly_written(character: str) -> bool:
    """Vrai pour ce qu'une revue voit : lettres, chiffres, ponctuation, symboles, espace."""
    if character in "\n ":
        return True
    return unicodedata.category(character)[0] in "LNPS" and character not in _BLANK_LETTERS


def _unseen(name: str, text: str) -> list[str]:
    """Caractères qu'une revue ne verrait pas : ils s'écrivent en échappement JSON."""
    return [
        f"{name} : caractère invisible écrit tel quel, ligne {number} (U+{ord(character):04X}) : "
        "l'écrire en échappement JSON"
        for number, line in enumerate(text.split("\n"), start=1)
        for character in sorted(set(line))
        if not _plainly_written(character)
    ]


def _keys(
    expect: Mapping[str, Any], required: Sequence[str], optional: Sequence[str] = ()
) -> list[str]:
    given = [key for key in expect if key != "verdict"]
    return [
        *(f"l'attente ne dit pas « {key} »" for key in required if key not in given),
        *(
            f"l'attente porte un champ inconnu « {key} »"
            for key in given
            if key not in (*required, *optional)
        ),
    ]


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _neutralised(expect: Mapping[str, Any]) -> list[str]:
    code = expect.get("code")
    if code == "headerMismatch":
        headers = expect.get("headers")
        if not (isinstance(headers, list) and headers and all(map(_is_text, headers))):
            return ["un écart d'en-têtes nomme les en-têtes contredits (« headers »)"]
        return _keys(expect, ("code", "headers"))
    if code == "unresolved":
        if not _is_text(expect.get("reason")):
            return ["une lecture non résolue dit son motif (« reason »)"]
        return _keys(expect, ("code", "reason"))
    if code != "invalidToolName":
        return ["une requête neutralisée se reconnaît à : " + ", ".join(NEUTRALISED)]
    return _keys(expect, ("code",))


def _counted(expect: Mapping[str, Any]) -> list[str]:
    if expect.get("code") != UNINSPECTED:
        return [f"une requête comptée l'est comme non inspectée (« {UNINSPECTED} »)"]
    return _keys(expect, ("code",))


def _refused(surface: str, expect: Mapping[str, Any]) -> list[str]:
    problems = _keys(
        expect, ("status", "code"), {"ingestion": ("read",), "lecture": ("path",)}.get(surface, ())
    )
    if problems:
        return problems
    if surface == "proxy" and expect["code"] not in PROXY_REFUSALS:
        return [
            "le proxy relaie tout appel : il ne refuse que pour Host, Origin et les requêtes "
            "d'une autre page (" + ", ".join(PROXY_REFUSALS) + ")"
        ]
    if expect["status"] not in _REFUSAL_STATUSES[surface]:
        admitted = ", ".join(map(str, _REFUSAL_STATUSES[surface]))
        return [f"un refus de la surface « {surface} » porte l'un des statuts {admitted}"]
    if not _is_text(expect["code"]):
        return ["un refus dit son code"]
    if expect.get("read", "partial") != "partial":
        return ["« read » ne prend que la valeur « partial » : le corps n'est pas lu en entier"]
    if surface == "lecture" and (expect["status"] == 422) != ("path" in expect):
        return ["un refus 422, et lui seul, dit le chemin du paramètre fautif (« path »)"]
    return []


def _rejected(expect: Mapping[str, Any]) -> list[str]:
    problems = _keys(expect, ("path", "code"), ("index",))
    if problems:
        return problems
    if not isinstance(expect["path"], str) or not _is_text(expect["code"]):
        return ["un rejet dit le chemin du champ fautif et son code"]
    if "index" in expect and not _is_count(expect["index"]):
        return ["le rang d'un événement rejeté est un nombre entier"]
    return []


def _bounded(expect: Mapping[str, Any]) -> list[str]:
    if expect.get("status") == 200:
        if not isinstance(expect.get("truncated"), bool):
            return ["une lecture bornée qui répond dit si elle est tronquée (« truncated »)"]
        return _keys(expect, ("status", "truncated"))
    if expect.get("status") == 503:
        if not _is_text(expect.get("code")):
            return ["une lecture interrompue dit son code"]
        return _keys(expect, ("status", "code"))
    return ["une lecture bornée répond 200, ou 503 quand son délai est dépassé"]


def _inert(expect: Mapping[str, Any]) -> list[str]:
    if not _is_text(expect.get("message")):
        return ["une adresse hostile ramène un message fixe (« message »)"]
    return _keys(expect, ("message",))


def _expect_problems(surface: str, expect: object) -> list[str]:
    if not isinstance(expect, dict):
        return ["l'attente est un objet JSON"]
    verdict = expect.get("verdict")
    if verdict not in VERDICTS[surface]:
        admitted = ", ".join(VERDICTS[surface])
        return [f"verdict inconnu pour la surface « {surface} » : {admitted} sont admis"]
    if verdict == "neutralise":
        return _neutralised(expect)
    if verdict == "compte":
        return _counted(expect)
    if verdict == "refuse":
        return _refused(surface, expect)
    if verdict == "rejete":
        return _rejected(expect)
    if verdict == "borne":
        return _bounded(expect)
    if verdict == "inerte":
        return _inert(expect)
    return _keys(expect, ())


def _base(name: str, raw: Any, seeds: Mapping[str, Any]) -> Base:
    """Construit la base. Une forme invalide lève KeyError, TypeError ou ValueError."""
    surface, request, healthy = raw["surface"], raw["request"], raw["healthy"]
    if surface not in SURFACES:
        raise ValueError("surface inconnue")
    if not name.startswith(f"{surface}/"):
        raise ValueError("le nom d'une base commence par sa surface")
    if not isinstance(request, dict) or not isinstance(healthy, dict) or "path" not in request:
        raise TypeError("la requête et son issue saine sont des objets JSON")
    if not isinstance(request.get("headers", {}), dict):
        raise TypeError("les en-têtes sont un objet JSON")
    seed = seeds[raw["seed"]] if "seed" in raw else []
    reports, settings = raw.get("reports", []), raw.get("settings", {})
    if not isinstance(seed, list) or not isinstance(reports, list):
        raise TypeError("les événements et les relevés préalables sont des tableaux")
    if not isinstance(settings, dict):
        raise TypeError("les réglages sont un objet JSON")
    unknown = set(raw) - {"title", "surface", "request", "healthy", "seed", "reports", "settings"}
    if unknown:
        raise ValueError("champ inconnu")
    return Base(
        name=name,
        title=str(raw["title"]),
        surface=surface,
        request=request,
        healthy=healthy,
        seed=tuple(seed),
        reports=tuple(reports),
        settings=settings,
    )


def check_bases(raw: object) -> tuple[dict[str, Base], list[str]]:
    """Les bases bien formées, et les problèmes des autres."""
    if not isinstance(raw, dict) or not isinstance(raw.get("bases"), dict):
        return {}, [f"{BASES_FILE} : un objet JSON qui porte « bases » est attendu"]
    seeds = raw.get("seeds", {})
    bases: dict[str, Base] = {}
    problems: list[str] = []
    for name, described in raw["bases"].items():
        try:
            bases[name] = _base(name, described, seeds)
        except (KeyError, TypeError, ValueError) as error:
            problems.append(
                f"{BASES_FILE} : base « {name} » invalide ({type(error).__name__} : {error})"
            )
    return bases, problems


def _case(family: str, raw: Mapping[str, Any], base: Base, open_reason: str | None = None) -> Case:
    return Case(
        family=family,
        name=raw["name"],
        title=raw["title"],
        surface=raw["surface"],
        base=base,
        alter=raw["alter"],
        expect=raw["expect"],
        canary=raw.get("canary", CANARY),
        open_reason=open_reason,
    )


def check_case(family: str, raw: object, bases: Mapping[str, Base]) -> list[str]:
    """Problèmes d'un cas ; la liste est vide quand il est bien formé."""
    if not isinstance(raw, dict):
        return [f"{family} : un cas est un objet JSON"]
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return [
            f"{family} : un cas porte un nom fait de minuscules, de chiffres et de traits d'union"
        ]
    problems = [f"champ inconnu « {key} »" for key in raw if key not in _CASE_FIELDS]
    if not _is_text(raw.get("title")):
        problems.append("aucun titre")
    surface, base = raw.get("surface"), bases.get(str(raw.get("base")))
    if surface not in SURFACES:
        problems.append("surface inconnue : " + ", ".join(SURFACES) + " sont admises")
    elif "expect" not in raw:
        problems.append("aucune attente")
    else:
        problems.extend(_expect_problems(surface, raw["expect"]))
    if base is None:
        problems.append(
            "base inconnue : la requête saine du cas doit être nommée dans " + BASES_FILE
        )
    elif base.surface != surface:
        problems.append(f"la base « {base.name} » n'est pas une requête de la surface du cas")
    canary, alter = raw.get("canary", CANARY), raw.get("alter")
    if not _is_text(canary):
        problems.append("le canari est un texte non vide")
    elif not isinstance(alter, dict):
        problems.append("aucune altération")
    else:
        try:
            hostile = hostile_text(alter)
            if base is not None:
                check_alteration(
                    base.request, base.seed, base.settings, alter, reports=base.reports
                )
        except AlterationError as error:
            problems.append(f"altération inapplicable ({error})")
        else:
            if hostile is not None and canary not in hostile:
                problems.append("la valeur hostile ne porte pas le canari du cas")
    return [f"{family}/{name} : {problem}" for problem in problems]


def _family_cases(family: str, raw: object) -> tuple[list[Any], list[str]]:
    if not _NAME.fullmatch(family):
        return [], [f"{family} : le nom d'une famille est fait de minuscules et de chiffres"]
    if (
        not isinstance(raw, dict)
        or not _is_text(raw.get("title"))
        or not isinstance(raw.get("cases"), list)
    ):
        return [], [f"{family} : une famille porte un titre (« title ») et ses cas (« cases »)"]
    return raw["cases"], []


def _exceptions(raw: object, known: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """Les cas ouverts et leur motif, et les problèmes du fichier d'exceptions."""
    if not isinstance(raw, list):
        return {}, [f"{OPEN_FILE} : un tableau d'exceptions est attendu"]
    reasons: dict[str, str] = {}
    problems: list[str] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("case"), str)
            or set(item) - {"case", "reason"}
        ):
            problems.append(
                f"{OPEN_FILE} : une exception nomme son cas (« case ») et son motif (« reason »)"
            )
            continue
        identifier = item["case"]
        if identifier not in known:
            problems.append(f"{OPEN_FILE} : exception pour un cas qui n'est pas au corpus")
        elif identifier in reasons:
            problems.append(f"{identifier} : deux exceptions pour le même cas")
        elif not _is_text(item.get("reason")):
            problems.append(f"{identifier} : cas ouvert sans motif")
        else:
            reasons[identifier] = item["reason"]
    return reasons, problems


def _coverage(cases: Sequence[Case]) -> list[str]:
    problems = []
    for family in FAMILIES:
        members = [case for case in cases if case.family == family]
        blocked = sum(1 for case in members if not case.is_open)
        if not members:
            problems.append(f"famille exigée absente : « {family} »")
        elif blocked < MIN_BLOCKED:
            problems.append(
                f"famille « {family} » : {blocked} cas bloqué(s), {MIN_BLOCKED} exigés "
                "(un cas ouvert ne compte pas)"
            )
    return problems


def abuse_digest(directory: Path | None = None) -> str:
    """Empreinte du contenu du corpus, indépendante de la mise en forme des fichiers."""
    content = {name: _parsed(text) for name, text in abuse_texts(directory).items()}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def recorded_abuse_digest(version: str = ABUSE_VERSION) -> str:
    """Empreinte enregistrée pour cette version du corpus."""
    return _DIGESTS[version]


def _read(directory: Path | None) -> tuple[AbuseCorpus | None, list[str]]:
    try:
        texts = abuse_texts(directory)
    except OSError as error:
        return None, [f"corpus d'abus illisible ({type(error).__name__})"]
    problems = [problem for name, text in texts.items() for problem in _unseen(name, text)]
    content: dict[str, Any] = {}
    for name, text in texts.items():
        try:
            content[name] = _parsed(text)
        except ValueError as error:
            problems.append(f"{name} : JSON illisible ({type(error).__name__} : {error})")
    if problems:
        return None, problems

    bases, problems = check_bases(content[BASES_FILE])
    described: list[tuple[str, Mapping[str, Any]]] = []
    for name in content:
        if not name.startswith(f"{CASES_DIRECTORY}/"):
            continue
        family = name.removeprefix(f"{CASES_DIRECTORY}/").removesuffix(".json")
        cases, malformed = _family_cases(family, content[name])
        problems.extend(malformed)
        for raw in cases:
            found = check_case(family, raw, bases)
            problems.extend(found)
            if not found:
                described.append((family, raw))
    identifiers = [f"{family}/{raw['name']}" for family, raw in described]
    problems.extend(
        f"{identifier} : deux cas portent ce nom"
        for identifier in sorted(set(identifiers))
        if identifiers.count(identifier) > 1
    )
    reasons, unfounded = _exceptions(content[OPEN_FILE], identifiers)
    problems.extend(unfounded)
    if problems:
        return None, problems

    corpus = AbuseCorpus(
        version=ABUSE_VERSION,
        bases=tuple(bases.values()),
        cases=tuple(
            _case(family, raw, bases[raw["base"]], reasons.get(f"{family}/{raw['name']}"))
            for family, raw in described
        ),
    )
    problems = _coverage(corpus.cases)
    digest = abuse_digest(directory)
    if not problems and digest != recorded_abuse_digest():
        problems.append(
            f"le corpus d'abus a changé sans nouvelle version : son empreinte est {digest}, "
            f"celle de la version {ABUSE_VERSION} est {recorded_abuse_digest()}. "
            "Enregistrer une nouvelle version dans abuse.py."
        )
    return corpus, problems


def check_abuse_corpus(directory: Path | None = None) -> list[str]:
    """Problèmes du corpus : ceux de chaque fichier et de chaque cas, puis ce qu'il ne couvre
    pas, puis un contenu changé sans nouvelle version."""
    return _read(directory)[1]


def load_abuse_corpus(directory: Path | None = None) -> AbuseCorpus:
    """Charge le corpus, après l'avoir contrôlé."""
    corpus, problems = _read(directory)
    if corpus is None or problems:
        raise CorpusError(problems)
    return corpus
