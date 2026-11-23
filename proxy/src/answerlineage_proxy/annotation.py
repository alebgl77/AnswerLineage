"""Avertir : le résultat d'une lecture en quarantaine porte un avertissement à gabarit fixe.

Pendant une quarantaine active en mode ``warn``, dont la diffusion est vérifiée
(``quarantine.py``), le relais HTTP retient les appels d'un outil qui lit la
zone, et insère en tête du contenu de leur résultat un bloc de texte qui dit
au modèle de l'agent, et à l'humain qui le lit, que ce résultat lit des données
déclarées fausses. Ce module en porte tout ce qui ne dépend pas du transport :

- **la zone** (``Zone``) : un instantané immuable des jeux en quarantaine,
  indexés par espace de noms puis par nom, avec les incidents qui les y mettent
  et leur profondeur : 0 pour un jeu d'un périmètre, au-delà pour une vue ou une
  table dérivée (``derived``), que la diffusion ne porte pas encore ;
- **qui est retenu** (``retention``) : un outil SQL déclaré dont l'espace de
  noms porte au moins un jeu de la zone, ou un outil dont la règle de lecture
  déclare un jeu de la zone. Un outil non décrit ne l'est jamais : on ne lui
  connaît pas de lecture ;
- **la décision** (``Verdict``) : ``certain`` quand la lecture établie touche la
  zone, ``outside`` quand elle n'y touche pas, ``uncertain`` quand elle n'a pas
  pu être établie, ou pas à temps. Pour une règle de lecture, elle est
  statique ; pour un outil SQL, elle suit l'analyse de la requête ;
- **le texte** (``warning_text``), à gabarit fixe. Il ne contient que des
  identifiants d'incident (des UUID contrôlés par ``parse``), l'heure
  d'activation relue strictement puis réécrite ici, et des noms que l'agent ou
  l'exploitant ont eux-mêmes écrits : les tables que la requête de l'agent lit,
  les jeux d'une règle de lecture, l'espace de noms de ``--sql-tool``. Un nom de
  la zone n'y entre que s'il est égal à l'un d'eux. Aucun texte de l'API ni d'un
  émetteur n'a de place dans le gabarit ;
- **le bloc** (``content_block``) : un bloc de contenu texte MCP, sérialisé en
  ASCII, que les hôtes passent au modèle (``annotations.audience`` nomme aussi
  l'humain) ;
- **l'insertion, à l'octet près** (``JsonInsertion``, ``SseInsertion``) : un
  lecteur strict du début de la réponse (``prefix.read_result_prefix``) trouve la
  place qui suit le ``[`` de ``result.content`` ; les octets avant et après sont
  recopiés tels quels. Rien n'est relu puis réécrit. Quand la place ne se trouve
  pas avec certitude, rien n'est inséré, et le motif est dit
  (``NotAnnotatedReason``).

Le contenu lu pour trouver la place n'est gardé que le temps de la décision, et
n'est jamais journalisé ni conservé.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from answerlineage_proxy.config import SqlTool
from answerlineage_proxy.names import normalize_name
from answerlineage_proxy.prefix import PREFIX_BYTES, ResultPrefix, read_result_prefix
from answerlineage_proxy.quarantine import QuarantineScope
from answerlineage_proxy.rules import ReadRule
from answerlineage_proxy.sql import SqlAnalysis

Decision = Literal["certain", "uncertain", "outside"]
# Pourquoi une décision est incertaine : la lecture n'a pas été établie (son motif est déjà
# dans ``unresolvedReason``), ou pas avant l'échéance de la décision.
UncertainReason = Literal["unresolved_read", "decision_timeout"]
# Pourquoi rien n'a été inséré dans le résultat d'un appel retenu qu'il fallait annoter.
NotAnnotatedReason = Literal[
    "encoded", "body_bound", "no_content", "error_response", "client_gone", "not_whole"
]

# Budget de calcul de l'analyseur de quarantaine, par requête : fixe.
ANALYSIS_BUDGET_MS: Final = 50
# Ce que le texte nomme au plus : au-delà, « et N autre(s) ».
MAX_NAMED_INCIDENTS: Final = 3
MAX_NAMED_DATASETS: Final = 5

_CERTAIN: Final = (
    "[AnswerLineage] Avertissement sur la qualité des données : ce résultat lit des données"
    " qu'un incident déclare fausses."
)
_UNCERTAIN: Final = (
    "[AnswerLineage] Avertissement sur la qualité des données, incertain : ce résultat peut"
    " lire des données qu'un incident déclare fausses ; sa lecture n'a pas pu être établie"
    " à temps."
)
_CONSEQUENCE: Final = (
    "Une réponse fondée sur ce résultat peut être fausse tant que la quarantaine n'est pas levée."
)
# Les hôtes passent au modèle les blocs de contenu ; l'audience nomme aussi l'humain.
_ANNOTATIONS: Final = {"audience": ["assistant", "user"], "priority": 1}
_CR: Final = 13
_LF: Final = 10
_DATA: Final = b"data"


@dataclass(frozen=True)
class ZoneEntry:
    """Un jeu de données de la zone : sa profondeur, et les incidents qui l'y mettent."""

    # 0 pour un jeu d'un périmètre ; au-delà, un jeu dérivé. Un jeu que deux incidents
    # mettent dans la zone à deux profondeurs garde la plus faible.
    depth: int
    incidents: frozenset[str]


# Un jeu dérivé d'un périmètre : espace de noms, nom et profondeur, d'au moins 1.
Derived = tuple[str, str, int]


@dataclass(frozen=True)
class Zone:
    """La zone des quarantaines actives : un instantané immuable, calculé à chaque version de
    la diffusion, hors du chemin des appels.

    Les jeux de données sont indexés par espace de noms, puis par nom : savoir si un outil
    lit la zone coûte une recherche dans un dictionnaire.
    """

    # Espace de noms, puis nom, sous leur forme NFC : le jeu de la zone.
    datasets: Mapping[str, Mapping[str, ZoneEntry]]
    # Les incidents qui mettent au moins un jeu de chaque espace de noms dans la zone.
    namespaces: Mapping[str, frozenset[str]]
    # L'heure d'activation de la quarantaine de chaque incident.
    activations: Mapping[str, datetime]

    @classmethod
    def of(
        cls,
        scopes: Iterable[QuarantineScope],
        derived: Mapping[str, Iterable[Derived]] | None = None,
    ) -> Zone:
        """La zone des périmètres, à la profondeur 0, et des jeux dérivés de chaque incident,
        à leur profondeur."""
        found: dict[str, dict[str, tuple[int, frozenset[str]]]] = {}
        activations: dict[str, datetime] = {}
        for scope in scopes:
            activations[scope.incident_id] = scope.activated_at
            members = [(namespace, name, 0) for namespace, name in scope.datasets]
            for namespace, name, depth in [*members, *(derived or {}).get(scope.incident_id, ())]:
                names = found.setdefault(namespace, {})
                known_depth, incidents = names.get(name, (depth, frozenset()))
                names[name] = (min(depth, known_depth), incidents | {scope.incident_id})
        return cls(
            datasets={
                namespace: {name: ZoneEntry(*entry) for name, entry in names.items()}
                for namespace, names in found.items()
            },
            namespaces={
                namespace: frozenset().union(*(incidents for _, incidents in names.values()))
                for namespace, names in found.items()
            },
            activations=activations,
        )

    def __bool__(self) -> bool:
        return bool(self.datasets)

    def entry(self, namespace: str, name: str) -> ZoneEntry | None:
        """Le jeu de la zone de cet espace de noms et de ce nom, s'il y est."""
        names = self.datasets.get(namespace)
        return None if names is None else names.get(name)


@dataclass(frozen=True)
class ZoneRead:
    """Un jeu de la zone qu'un appel lit, sous le nom que l'agent ou l'exploitant a écrit."""

    namespace: str
    name: str
    # 0 pour un jeu d'un périmètre ; au-delà, un jeu dérivé.
    depth: int


@dataclass(frozen=True)
class Verdict:
    """La décision sur un appel retenu, et ce que son avertissement nomme."""

    decision: Decision
    # Identifiants d'incident, triés.
    incidents: tuple[str, ...]
    # Lecture certaine : les jeux de la zone que l'appel lit.
    reads: tuple[ZoneRead, ...] = ()
    # Lecture incertaine : l'espace de noms de l'outil SQL.
    namespace: str | None = None
    uncertain_reason: UncertainReason | None = None

    @property
    def warns(self) -> bool:
        """Vrai quand le résultat doit porter un avertissement."""
        return self.decision != "outside"


@dataclass(frozen=True)
class Retention:
    """Ce qui retient un appel : les incidents en cause, et de quoi décider.

    Un outil SQL donne son espace de noms : la décision attend l'analyse de sa requête. Une
    règle de lecture donne sa décision, statique, sans analyse ni attente.
    """

    incidents: tuple[str, ...]
    namespace: str | None = None
    verdict: Verdict | None = None


@dataclass(frozen=True)
class QuarantineNote:
    """Ce que l'événement d'un appel retenu dit de la décision et de l'annotation."""

    decision: Decision
    incidents: tuple[str, ...]
    annotated: bool
    # Attente de la décision, en millisecondes, au plus son budget.
    wait_ms: float
    uncertain_reason: UncertainReason | None = None
    not_annotated_reason: NotAnnotatedReason | None = None

    def fields(self) -> dict[str, Any]:
        """Le champ ``quarantine`` de la facette ``answerlineage_toolCall`` (version 1-3-0)."""
        found: dict[str, Any] = {
            "decision": self.decision,
            "incidents": list(self.incidents),
            "annotated": self.annotated,
            "waitMs": self.wait_ms,
        }
        if self.uncertain_reason is not None:
            found["uncertainReason"] = self.uncertain_reason
        if self.not_annotated_reason is not None:
            found["notAnnotatedReason"] = self.not_annotated_reason
        return found


def _zone_reads(zone: Zone, datasets: Iterable[tuple[str, str]]) -> tuple[ZoneRead, ...]:
    """Les jeux lus qui sont dans la zone, sans doublon, du périmètre aux plus dérivés."""
    reads = set()
    for namespace, name in datasets:
        entry = zone.entry(namespace, name)
        if entry is not None:
            reads.add(ZoneRead(namespace, name, entry.depth))
    return tuple(sorted(reads, key=lambda read: (read.depth, read.namespace, read.name)))


def _certain(zone: Zone, reads: tuple[ZoneRead, ...]) -> Verdict:
    incidents: set[str] = set()
    for read in reads:
        entry = zone.entry(read.namespace, read.name)
        if entry is not None:
            incidents |= entry.incidents
    return Verdict("certain", tuple(sorted(incidents)), reads=reads)


def retention(
    zone: Zone | None,
    tool: str | None,
    sql_tools: Mapping[str, SqlTool],
    read_rules: Mapping[str, ReadRule],
) -> Retention | None:
    """Ce qui retient l'appel de cet outil, ou ``None`` : une recherche dans la zone."""
    if not zone or tool is None:
        return None
    sql = sql_tools.get(tool)
    if sql is not None:
        incidents = zone.namespaces.get(sql.namespace)
        if incidents is None:
            return None
        return Retention(tuple(sorted(incidents)), namespace=sql.namespace)
    rule = read_rules.get(tool)
    if rule is None:
        return None
    reads = _zone_reads(zone, ((dataset.namespace, dataset.name) for dataset in rule.datasets))
    if not reads:
        return None
    verdict = _certain(zone, reads)
    return Retention(verdict.incidents, verdict=verdict)


def sql_verdict(zone: Zone, namespace: str, analysis: SqlAnalysis | None) -> Verdict:
    """La décision sur l'appel d'un outil SQL, d'après l'analyse de sa requête ; sans analyse
    à l'échéance (``None``), incertaine."""
    incidents = tuple(sorted(zone.namespaces.get(namespace, ())))
    if analysis is None:
        return Verdict(
            "uncertain", incidents, namespace=namespace, uncertain_reason="decision_timeout"
        )
    if not analysis.resolved:
        return Verdict(
            "uncertain", incidents, namespace=namespace, uncertain_reason="unresolved_read"
        )
    reads = _zone_reads(
        zone, ((namespace, normalize_name(table.name)) for table in analysis.tables)
    )
    if reads:
        return _certain(zone, reads)
    return Verdict("outside", incidents, namespace=namespace)


def _more(named: int, total: int) -> list[str]:
    return [f"et {total - named} autre(s)"] if total > named else []


def _incidents_line(verdict: Verdict, zone: Zone) -> str:
    ordered = sorted(verdict.incidents, key=lambda incident: (zone.activations[incident], incident))
    named = [
        f"{incident}, en quarantaine depuis le "
        f"{zone.activations[incident].astimezone(UTC):%Y-%m-%d %H:%M} UTC"
        for incident in ordered[:MAX_NAMED_INCIDENTS]
    ]
    label = "Incident" if len(ordered) == 1 else "Incidents"
    return f"{label} : {' ; '.join([*named, *_more(len(named), len(ordered))])}."


def _perimeter_line(verdict: Verdict) -> str:
    if verdict.decision == "uncertain":
        return f"Périmètre possible : espace de noms {verdict.namespace}."
    named = [
        f"{read.namespace} {read.name}" + (" (dérivée)" if read.depth else "")
        for read in verdict.reads[:MAX_NAMED_DATASETS]
    ]
    return f"Périmètre lu : {' ; '.join([*named, *_more(len(named), len(verdict.reads))])}."


def warning_text(verdict: Verdict, zone: Zone) -> str:
    """L'avertissement d'une décision certaine ou incertaine, à gabarit fixe."""
    head = _CERTAIN if verdict.decision == "certain" else _UNCERTAIN
    return "\n".join([head, _incidents_line(verdict, zone), _perimeter_line(verdict), _CONSEQUENCE])


def content_block(text: str) -> bytes:
    """Le bloc de contenu texte MCP qui porte l'avertissement, en ASCII pur."""
    block = {"type": "text", "text": text, "annotations": _ANNOTATIONS}
    return json.dumps(block, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _inserted(held: bytearray, position: int, empty: bool, block: bytes) -> int:
    """Insère le bloc à sa place, suivi d'une virgule devant un tableau non vide."""
    insert = block if empty else block + b","
    held[position:position] = insert
    return len(insert)


class JsonInsertion:
    """Retient le début d'une réponse JSON jusqu'à savoir où insérer le bloc, ou que rien ne
    s'y insère ; puis rend tout ce qu'il a retenu, le bloc à sa place.

    La place est cherchée dans les ``PREFIX_BYTES`` premiers octets : au-delà, ou à la fin
    du corps sans conclusion, rien ne s'insère (``not_whole``).
    """

    def __init__(self, identifier: str | int | None = None) -> None:
        self._identifier = identifier
        self._held = bytearray()
        self._position = 0
        self._empty = False
        # Vrai tant que des octets sont retenus : les en-têtes attendent avec eux.
        self.holding = True
        # Vrai quand la place est trouvée : ``release`` rend le corps retenu, bloc inséré.
        self.ready = False
        self.reason: NotAnnotatedReason | None = None
        # Octets insérés : la longueur annoncée du corps grandit d'autant.
        self.added = 0

    @property
    def concluded(self) -> bool:
        """Vrai quand le bloc est inséré, ou que rien ne s'insérera."""
        return not self.holding

    def feed(self, chunk: bytes) -> bytes:
        """Un morceau du corps ; rend ce qui peut partir aussitôt."""
        if not self.holding:
            return chunk
        self._held += chunk
        prefix = read_result_prefix(bytes(self._held[:PREFIX_BYTES]), self._identifier)
        if prefix.state == "found" and prefix.position is not None:
            self._position, self._empty, self.ready = prefix.position, prefix.empty, True
            return b""
        if prefix.state == "pending" and len(self._held) < PREFIX_BYTES:
            return b""
        self.reason = _reason(prefix)
        return self._flush()

    def release(self, block: bytes | None) -> bytes:
        """Le corps retenu, le bloc inséré à sa place ; sans bloc, tel quel."""
        self.ready = False
        if block is not None:
            self.added = _inserted(self._held, self._position, self._empty, block)
        return self._flush()

    def finish(self) -> bytes:
        """La fin du corps : ce qui reste retenu part tel quel."""
        if not self.holding:
            return b""
        if self.reason is None:
            self.reason = "not_whole"
        return self._flush()

    def _flush(self) -> bytes:
        data, self._held = bytes(self._held), bytearray()
        self.holding = False
        return data


def _reason(prefix: ResultPrefix) -> NotAnnotatedReason:
    """Le motif d'une réponse où rien ne s'insère : une requête ou une notification n'y a pas
    de contenu ; un début non conclu ne se lit pas entier."""
    if prefix.state == "not_reply":
        return "no_content"
    return prefix.reason if prefix.reason is not None else "not_whole"


def _line_end(data: bytes, start: int) -> int:
    """La fin de la ligne qui commence en ``start`` : son premier CR ou LF, ou la fin des
    données."""
    ends = [found for found in (data.find(b"\r", start), data.find(b"\n", start)) if found >= 0]
    return min(ends) if ends else len(data)


class SseInsertion:
    """Laisse passer un flux SSE au fil de l'eau, sauf l'événement qui porte la réponse
    JSON-RPC de l'appel : sa première ligne ``data:`` est retenue le temps de trouver la place
    du bloc, puis rendue, le bloc inséré.

    Les autres événements (progression, requêtes du serveur, notifications) passent aussitôt.
    Une réponse dont la première ligne ``data:`` ne porte pas la place, parce que le JSON se
    poursuit sur d'autres lignes ou au-delà de ``PREFIX_BYTES`` octets, n'est pas annotée
    (``not_whole``). Seule la première ligne ``data:`` de chaque événement est lue.
    """

    def __init__(self, identifier: str | int | None = None) -> None:
        self._identifier = identifier
        # Fini : la place a servi, ou la réponse est passée sans elle. Tout passe tel quel.
        self._done = False
        # La ligne retenue : la première ligne ``data:`` de l'événement en cours, tant qu'on ne
        # sait pas si elle porte la réponse.
        self._line = bytearray()
        self._held = False
        self._at_start = True
        self._after_cr = False
        # La première ligne ``data:`` de l'événement en cours a été lue.
        self._looked = False
        # Ce qui a suivi la place trouvée, dans le même morceau : attend ``release``.
        self._tail = b""
        self._position = 0
        self._empty = False
        self.holding = False
        self.ready = False
        self.reason: NotAnnotatedReason | None = None
        self.added = 0

    @property
    def concluded(self) -> bool:
        """Vrai quand le bloc est inséré, ou que rien ne s'insérera."""
        return self._done

    def feed(self, chunk: bytes) -> bytes:
        """Un morceau du flux ; rend ce qui peut partir aussitôt."""
        if self._done:
            return chunk
        if self.ready:
            self._tail += chunk
            return b""
        out = bytearray()
        start, size = 0, len(chunk)
        while start < size:
            if self._after_cr:
                self._after_cr = False
                if chunk[start] == _LF:
                    out.append(_LF)
                    start += 1
                    continue
            if self._at_start:
                self._at_start = False
                if chunk[start] in (_CR, _LF):
                    # Une ligne vide : l'événement s'achève.
                    self._looked = False
                    self._after_cr = chunk[start] == _CR
                    self._at_start = True
                    out.append(chunk[start])
                    start += 1
                    continue
                self._held = not self._looked
            end = _line_end(chunk, start)
            if self._held:
                self._line += chunk[start:end]
            else:
                out += chunk[start:end]
            complete = end < size
            if complete:
                self._after_cr = chunk[end] == _CR
                self._at_start = True
                if self._held:
                    self._line.append(chunk[end])
                else:
                    out.append(chunk[end])
            start = end + 1 if complete else size
            concluded = self._look(out, complete=complete) if self._held else None
            if concluded == "ready":
                self._tail = chunk[start:]
                return bytes(out)
            if concluded == "done":
                out += chunk[start:]
                return bytes(out)
        return bytes(out)

    def _look(self, out: bytearray, *, complete: bool) -> Literal["ready", "done"] | None:
        """Lit la ligne retenue : porte-t-elle la réponse, et où va le bloc ? ``ready`` quand
        la place est trouvée, ``done`` quand la réponse passe sans elle."""
        content = bytes(self._line[:-1] if complete else self._line)
        colon = content.find(b":")
        field = content if colon < 0 else content[:colon]
        if colon < 0 and not complete and _DATA.startswith(content):
            return None
        if field != _DATA:
            self._pass(out)
            return None
        offset = colon + 1 if colon >= 0 else len(content)
        if content[offset : offset + 1] == b" ":
            offset += 1
        payload = content[offset : offset + PREFIX_BYTES]
        prefix = read_result_prefix(payload, self._identifier)
        if prefix.state == "found" and prefix.position is not None:
            self._position, self._empty = offset + prefix.position, prefix.empty
            self.ready = self.holding = True
            return "ready"
        if prefix.state == "refused" and prefix.reason in ("error_response", "no_content"):
            self.reason = prefix.reason
            self._pass(out)
            self._done = True
            return "done"
        if prefix.state == "pending" and not complete and len(payload) < PREFIX_BYTES:
            return None
        # Une requête ou une notification passe ; un début illisible, en plusieurs lignes ou trop
        # long aussi : s'il portait la réponse, le flux s'achèvera sans place trouvée.
        self._looked = True
        self._pass(out)
        return None

    def _pass(self, out: bytearray) -> None:
        """La ligne retenue part ; le reste de la ligne passe sans être retenu."""
        out += self._line
        self._line = bytearray()
        self._held = False

    def release(self, block: bytes | None) -> bytes:
        """La ligne retenue, le bloc inséré à sa place, et ce qui la suivait ; sans bloc, tels
        quels. Tout passe ensuite au fil de l'eau."""
        self.ready = self.holding = False
        if block is not None:
            self.added = _inserted(self._line, self._position, self._empty, block)
        data = bytes(self._line) + self._tail
        self._line, self._tail = bytearray(), b""
        self._done = True
        return data

    def finish(self) -> bytes:
        """La fin du flux : ce qui reste retenu part tel quel."""
        if self._done:
            return b""
        self._done = True
        self.reason = "not_whole"
        data = bytes(self._line)
        self._line = bytearray()
        return data
