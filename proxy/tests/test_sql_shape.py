"""Le SQL conservé sans ses littéraux : la forme tirée de l'arbre syntaxique, ou rien.

Une constante, quelle que soit son écriture, devient le marqueur ``?`` ; un
commentaire disparaît ; les noms restent. Au moindre doute, aucune forme n'est
gardée.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect as SqlglotDialect

from answerlineage_proxy.analyzer import SqlAnalyzer
from answerlineage_proxy.config import SqlTool
from answerlineage_proxy.sql import (
    MAX_SHAPE_CHARS,
    MAX_SQL_BYTES,
    Dialect,
    _is_doubtless,
    analyze_sql,
)
from proxy.tests.conftest import TOOL_RESULT, BenchFactory, exchange, tool_call
from proxy.tests.test_analyzer import PATHOLOGICAL

# Ce qui ne doit survivre dans aucune forme : un nom de client, un nombre, une date.
SECRETS = re.compile(r"dupont|canari|7310427|1110011|1973", re.IGNORECASE)

# Chaque écriture d'une constante, et chaque commentaire, dans chaque dialecte géré.
WRITINGS: dict[Dialect, dict[str, str]] = {
    "bigquery": {
        "chaîne": "SELECT nom FROM crm.clients WHERE nom = 'Dupont'",
        "chaîne entre guillemets": 'SELECT nom FROM crm.clients WHERE nom = "Dupont"',
        "chaîne triple": "SELECT nom FROM crm.clients WHERE nom = '''Dupont'''",
        "chaîne brute": "SELECT nom FROM crm.clients WHERE nom = r'Dupont'",
        "octets": "SELECT nom FROM crm.clients WHERE cle = b'Dupont'",
        "entier": "SELECT nom FROM crm.clients WHERE id = 7310427",
        "décimal": "SELECT nom FROM crm.clients WHERE solde = 7310427.25e3",
        "négatif": "SELECT nom FROM crm.clients WHERE solde > -7310427",
        "hexadécimal": "SELECT nom FROM crm.clients WHERE id = 0x7310427",
        "numérique typé": "SELECT nom FROM crm.clients WHERE solde = NUMERIC '7310427'",
        "date": "SELECT nom FROM crm.clients WHERE creation = DATE '1973-10-04'",
        "horodatage": "SELECT nom FROM crm.clients WHERE vu = TIMESTAMP '1973-10-04 12:34:56'",
        "intervalle": (
            "SELECT nom FROM crm.clients WHERE vu > DATE_SUB(CURRENT_DATE(), INTERVAL 7310427 DAY)"
        ),
        "booléen": "SELECT nom FROM crm.clients WHERE actif IS TRUE",
        "liste IN": "SELECT nom FROM crm.clients WHERE nom IN ('Dupont', 'Canari')",
        "motif LIKE": "SELECT nom FROM crm.clients WHERE nom LIKE '%Dupont%'",
        "chaîne JSON": 'SELECT nom FROM crm.clients WHERE fiche = JSON \'{"nom": "Dupont"}\'',
        "chemin JSON": "SELECT JSON_VALUE(fiche, '$.canari') FROM crm.clients",
        "commentaire de ligne": "SELECT nom FROM crm.clients -- Dupont\n",
        "commentaire dièse": "SELECT nom FROM crm.clients # Dupont\n",
        "commentaire de bloc": "SELECT /* Dupont */ nom FROM crm.clients",
    },
    "postgres": {
        "chaîne": "SELECT nom FROM crm.clients WHERE nom = 'Dupont'",
        "apostrophe doublée": "SELECT nom FROM crm.clients WHERE nom = 'Dupont l''ancien'",
        "chaîne échappée": "SELECT nom FROM crm.clients WHERE nom = E'Dupont\\n'",
        "chaîne Unicode": "SELECT nom FROM crm.clients WHERE nom = U&'Dupont'",
        "chaîne nationale": "SELECT nom FROM crm.clients WHERE nom = N'Dupont'",
        "dollars": "SELECT nom FROM crm.clients WHERE nom = $$Dupont$$",
        "dollars étiquetés": "SELECT nom FROM crm.clients WHERE nom = $canari$Dupont$canari$",
        "entier": "SELECT nom FROM crm.clients WHERE id = 7310427",
        "décimal": "SELECT nom FROM crm.clients WHERE solde = 7310427.25",
        "hexadécimal": "SELECT nom FROM crm.clients WHERE cle = X'7310427A'",
        "binaire": "SELECT nom FROM crm.clients WHERE drapeaux = B'1110011'",
        "date": "SELECT nom FROM crm.clients WHERE creation = DATE '1973-10-04'",
        "horodatage": "SELECT nom FROM crm.clients WHERE vu = TIMESTAMP '1973-10-04 12:34:56'",
        "conversion": "SELECT nom FROM crm.clients WHERE creation = '1973-10-04'::date",
        "intervalle": "SELECT nom FROM crm.clients WHERE age(vu) < INTERVAL '7310427 days'",
        "booléen": "SELECT nom FROM crm.clients WHERE actif = true",
        "paramètre": "SELECT nom FROM crm.clients WHERE id = $1 AND nom = 'Dupont'",
        "liste IN": "SELECT nom FROM crm.clients WHERE nom IN ('Dupont', 'Canari')",
        "tableau": "SELECT nom FROM crm.clients WHERE nom = ANY(ARRAY['Dupont'])",
        "motif LIKE": "SELECT nom FROM crm.clients WHERE nom ILIKE '%dupont%'",
        "motif SIMILAR": "SELECT nom FROM crm.clients WHERE nom SIMILAR TO 'Dupont%'",
        "expression régulière": "SELECT nom FROM crm.clients WHERE nom ~ '^Dupont'",
        "chaîne JSON": 'SELECT nom FROM crm.clients WHERE fiche @> \'{"nom":"Dupont"}\'::jsonb',
        "chemin JSON": "SELECT fiche ->> 'canari', fiche #>> '{canari,a}' FROM crm.clients",
        "collation": "SELECT nom FROM crm.clients WHERE nom = 'Dupont' COLLATE \"C\"",
        "commentaire de ligne": "SELECT nom FROM crm.clients -- Dupont\n",
        "commentaire de bloc": "SELECT /* Dupont /* imbriqué */ */ nom FROM crm.clients",
    },
    "snowflake": {
        "chaîne": "SELECT nom FROM crm.clients WHERE nom = 'Dupont'",
        "dollars": "SELECT nom FROM crm.clients WHERE nom = $$Dupont$$",
        "entier": "SELECT nom FROM crm.clients WHERE id = 7310427",
        "décimal": "SELECT nom FROM crm.clients WHERE solde = 7310427.25",
        "hexadécimal": "SELECT nom FROM crm.clients WHERE cle = X'7310427A'",
        "date": "SELECT nom FROM crm.clients WHERE creation = '1973-10-04'::DATE",
        "date convertie": "SELECT nom FROM crm.clients WHERE creation = TO_DATE('1973-10-04')",
        "intervalle": (
            "SELECT nom FROM crm.clients WHERE vu > CURRENT_DATE - INTERVAL '7310427 days'"
        ),
        "décalage de date": (
            "SELECT nom FROM crm.clients WHERE vu > DATEADD(day, -7310427, CURRENT_DATE())"
        ),
        "booléen": "SELECT nom FROM crm.clients WHERE actif = TRUE",
        "liste IN": "SELECT nom FROM crm.clients WHERE nom IN ('Dupont', 'Canari')",
        "motif LIKE": "SELECT nom FROM crm.clients WHERE nom ILIKE '%dupont%'",
        "chaîne JSON": 'SELECT PARSE_JSON(\'{"nom":"Dupont"}\') FROM crm.clients',
        "chemin JSON": "SELECT fiche:canari::string FROM crm.clients",
        "commentaire de ligne": "SELECT nom FROM crm.clients -- Dupont\n",
        "commentaire double barre": "SELECT nom FROM crm.clients // Dupont\n",
        "commentaire de bloc": "SELECT /* Dupont */ nom FROM crm.clients",
    },
}


@pytest.mark.parametrize(
    ("dialect", "writing"),
    [(dialect, writing) for dialect, writings in WRITINGS.items() for writing in writings],
)
def test_no_constant_and_no_comment_survives_in_the_shape(dialect: Dialect, writing: str) -> None:
    shape = analyze_sql(WRITINGS[dialect][writing], dialect).shape
    assert shape is not None
    assert not SECRETS.search(shape), shape
    # Les noms restent : la forme dit ce que l'agent a demandé.
    assert "clients" in shape.lower()


def test_a_constant_becomes_a_neutral_marker_and_names_stay() -> None:
    sql = (
        "SELECT c.nom, SUM(o.montant) AS total FROM crm.clients AS c "
        "JOIN ventes.commandes AS o ON o.client_id = c.id "
        "WHERE c.nom = 'Dupont' AND o.date_commande >= DATE '2026-01-01' "
        "AND c.ville IN ('Lyon', 'Nantes', 'Lille') GROUP BY c.nom LIMIT 10 -- extraction"
    )
    assert analyze_sql(sql, "postgres").shape == (
        "SELECT c.nom, SUM(o.montant) AS total FROM crm.clients AS c "
        "JOIN ventes.commandes AS o ON o.client_id = c.id "
        "WHERE c.nom = ? AND o.date_commande >= CAST(? AS DATE) "
        "AND c.ville IN (?) GROUP BY c.nom LIMIT ?"
    )


def test_an_in_list_has_one_shape_whatever_its_length() -> None:
    queries = [
        "SELECT a FROM t WHERE b IN (1)",
        "SELECT a FROM t WHERE b IN ('x', 'y')",
        "SELECT a FROM t WHERE b IN (" + ", ".join(map(str, range(50))) + ")",  # noqa: S608
    ]
    shapes = {analyze_sql(sql, "postgres").shape for sql in queries}
    assert shapes == {"SELECT a FROM t WHERE b IN (?)"}


def test_placeholders_already_in_the_query_stay_markers() -> None:
    sql = "SELECT a FROM t WHERE b = %s AND c = ? AND d = %(nom)s AND e = 'Dupont'"
    assert analyze_sql(sql, "postgres").shape == (
        "SELECT a FROM t WHERE b = ? AND c = ? AND d = %(nom)s AND e = ?"
    )


def test_a_comment_left_in_a_shape_is_doubtful() -> None:
    """Dernier filet : une forme redécoupée qui porterait un commentaire n'est pas gardée."""
    bigquery = SqlglotDialect.get_or_raise("bigquery")
    assert _is_doubtless("SELECT a FROM t", [], bigquery)
    assert not _is_doubtless("SELECT a FROM t # Dupont", [], bigquery)


def test_a_time_unit_written_as_a_string_is_kept() -> None:
    """Un vocabulaire clos, qui ne dit rien des données : DATE_TRUNC('month', d)."""
    shape = analyze_sql("SELECT DATE_TRUNC('month', d) FROM t WHERE n = 'Dupont'", "postgres")
    assert shape.shape == "SELECT DATE_TRUNC('MONTH', d) FROM t WHERE n = ?"


def test_a_statement_whose_reads_are_unknown_keeps_its_shape() -> None:
    analysis = analyze_sql("UPDATE crm.clients SET nom = 'Dupont' WHERE id = 7310427", "postgres")
    assert analysis.reason == "unsupported_statement"
    assert analysis.shape == "UPDATE crm.clients SET nom = ? WHERE id = ?"


def test_several_statements_keep_one_shape() -> None:
    shape = analyze_sql("SELECT a FROM t WHERE b = 'Dupont'; SELECT c FROM u", "postgres").shape
    assert shape == "SELECT a FROM t WHERE b = ?; SELECT c FROM u"


def test_a_name_with_the_form_of_an_identifier_is_kept() -> None:
    """Limite connue : un alias d'un seul mot, choisi par l'auteur, reste un nom."""
    shape = analyze_sql('SELECT nom AS "Dupont" FROM crm.clients', "postgres").shape
    assert shape == 'SELECT nom AS "Dupont" FROM crm.clients'


def test_a_qualified_name_written_in_one_piece_is_kept() -> None:
    shape = analyze_sql("SELECT id FROM `proj.jeu.clients` WHERE id = 7", "bigquery").shape
    assert shape == "SELECT id FROM `proj.jeu.clients` WHERE id = ?"


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        # Un nom entre guillemets est un texte libre : une donnée, une instruction, une balise.
        ('SELECT nom AS "Jeanne Dupont" FROM crm.clients', "postgres"),
        ("SELECT * FROM `canari; DROP TABLE comptes`", "bigquery"),
        ('SELECT "<b>canari</b>" FROM crm.clients', "postgres"),
        ('SELECT a FROM db.sch."Ma Table" t', "snowflake"),
        ("SELECT `Nom Client` FROM `proj.jeu.clients`", "bigquery"),
        # Un caractère sans rendu, que le contrôle « imprimable » laisse passer.
        ("SELECT * FROM crm.`clients\ufe0f`", "bigquery"),
        ("SELECT * FROM `proj..clients`", "bigquery"),
    ],
)
def test_no_shape_is_kept_when_a_name_is_not_an_identifier(sql: str, dialect: Dialect) -> None:
    """Seul un nom qu'un événement peut recopier reste dans une forme ; sinon, pas de forme."""
    assert analyze_sql(sql, dialect).shape is None


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        # Syntaxe inconnue : la requête ne se lit pas en entier.
        ("SELEC nom FRM clients WHERE nom = 'Dupont'", "postgres"),
        ("SELECT nom FROM clients WHERE nom = 'Dupont", "bigquery"),
        # Repli de l'analyseur sur du texte brut.
        ("CALL rappel('Dupont')", "postgres"),
        ("EXECUTE IMMEDIATE 'SELECT Dupont'", "bigquery"),
        # Une constante que l'analyseur change en nom ou en unité inconnue.
        ("SELECT DATE_TRUNC('dupont', d) FROM t", "postgres"),
        ("SELECT DATEADD('dupont', 1, d) FROM t", "snowflake"),
        # Une instruction qui n'est qu'une constante.
        ("'Dupont'", "postgres"),
        ("", "postgres"),
        ("-- Dupont", "postgres"),
    ],
)
def test_no_shape_is_kept_when_in_doubt(sql: str, dialect: Dialect) -> None:
    assert analyze_sql(sql, dialect).shape is None


def test_no_shape_is_kept_for_a_query_over_the_size_limit() -> None:
    sql = "SELECT a FROM t WHERE b = 'Dupont' -- " + "x" * MAX_SQL_BYTES  # noqa: S608
    assert analyze_sql(sql, "postgres").shape is None


def test_no_shape_is_kept_when_it_would_be_too_long() -> None:
    columns = ", ".join(f"colonne_{index}" for index in range(MAX_SHAPE_CHARS // 10))
    analysis = analyze_sql(f"SELECT {columns} FROM t", "postgres")  # noqa: S608
    assert analysis.resolved
    assert analysis.shape is None


def test_no_shape_is_kept_for_a_query_nested_too_deeply() -> None:
    sql = "SELECT " + "(" * 3000 + "'Dupont'" + ")" * 3000
    assert analyze_sql(sql, "postgres").shape is None


@pytest.mark.anyio
async def test_no_shape_is_kept_when_the_budget_is_exceeded() -> None:
    analyzer = SqlAnalyzer(budget=0.05)
    try:
        result = await analyzer.analyze(PATHOLOGICAL, "postgres")
        assert result.origin == "over_budget"
        assert result.analysis.shape is None
    finally:
        await analyzer.aclose()


@pytest.mark.anyio
async def test_the_shape_is_computed_with_the_analysis_in_its_own_process() -> None:
    analyzer = SqlAnalyzer()
    try:
        result = await analyzer.analyze("SELECT t.a FROM s.t AS t WHERE t.b = 'Dupont'", "postgres")
        assert result.origin == "analyzed"
        assert result.analysis.shape == "SELECT t.a FROM s.t AS t WHERE t.b = ?"
        # Servie par le cache, la forme est la même.
        again = await analyzer.analyze("SELECT t.a FROM s.t AS t WHERE t.b = 'Dupont'", "postgres")
        assert (again.origin, again.analysis.shape) == ("cache", result.analysis.shape)
    finally:
        await analyzer.aclose()


# Requêtes gabarits : {s}, {s2} chaînes, {n} nombre, {d} date, {ts} horodatage.
TEMPLATES: dict[Dialect, str] = {
    "bigquery": (
        "SELECT c.nom, SUM(o.montant) FROM `crm.clients` AS c "
        "JOIN `ventes.commandes` AS o ON o.client_id = c.id "
        "WHERE c.nom = {s} AND c.ville IN ({s}, {s2}) AND c.email LIKE {s2} "
        "AND o.montant > {n} AND o.date_commande >= DATE {d} AND o.vu < TIMESTAMP {ts} "
        "GROUP BY c.nom LIMIT {n}"
    ),
    "postgres": (
        "SELECT c.nom, SUM(o.montant) FROM crm.clients AS c "
        "JOIN ventes.commandes AS o ON o.client_id = c.id "
        "WHERE c.nom = {s} AND c.ville IN ({s}, {s2}) AND c.email ILIKE {s2} "
        "AND o.montant > {n} AND o.date_commande >= {d}::date AND o.vu < TIMESTAMP {ts} "
        "AND o.fiche ->> {s} = {s2} GROUP BY c.nom LIMIT {n}"
    ),
    "snowflake": (
        "SELECT c.nom, SUM(o.montant) FROM crm.clients AS c "
        "JOIN ventes.commandes AS o ON o.client_id = c.id "
        "WHERE c.nom = {s} AND c.ville IN ({s}, {s2}) AND c.email ILIKE {s2} "
        "AND o.montant > {n} AND o.date_commande >= TO_DATE({d}) AND o.vu < {ts}::TIMESTAMP "
        "GROUP BY c.nom LIMIT {n}"
    ),
}


def string(value: str, dialect: Dialect) -> str:
    """Une chaîne écrite comme le dialecte l'écrit, échappements compris."""
    return exp.Literal.string(value).sql(dialect=dialect)


def query(
    dialect: Dialect, text: str, other: str, number: int | float, day: date, moment: datetime
) -> str:
    return TEMPLATES[dialect].format(
        s=string(text, dialect),
        s2=string(other, dialect),
        n=repr(number),
        d=string(day.isoformat(), dialect),
        ts=string(moment.isoformat(sep=" "), dialect),
    )


REFERENCE = {
    dialect: analyze_sql(
        query(dialect, "a", "b", 1, date(2026, 1, 1), datetime(2026, 1, 1)), dialect
    ).shape
    for dialect in TEMPLATES
}


def test_the_templates_have_a_shape() -> None:
    for dialect, shape in REFERENCE.items():
        assert shape is not None, dialect
        assert "?" in shape


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    dialect=st.sampled_from(sorted(TEMPLATES)),
    text=st.text(max_size=40),
    other=st.text(alphabet=st.characters(codec="utf-8"), min_size=1, max_size=40),
    number=st.integers(min_value=0) | st.floats(min_value=0, allow_nan=False, allow_infinity=False),
    day=st.dates(),
    moment=st.datetimes(),
)
def test_random_literals_never_survive_in_the_shape(
    dialect: Dialect, text: str, other: str, number: int | float, day: date, moment: datetime
) -> None:
    """Quels que soient les littéraux, la forme est celle du gabarit, ou il n'y en a pas."""
    shape = analyze_sql(query(dialect, text, other, number, day, moment), dialect).shape
    assert shape in (None, REFERENCE[dialect])


SQL_TOOLS = {"execute_sql": SqlTool(argument="query", dialect="bigquery", namespace="bigquery")}


@pytest.mark.anyio
async def test_a_customer_name_in_a_literal_never_reaches_the_event(bench: BenchFactory) -> None:
    """Critère d'acceptation : la forme conservée ne contient pas le nom du client."""
    sql = (
        "SELECT c.nom, c.solde FROM `crm.clients` AS c "
        "WHERE c.nom = 'Jeanne Dupont-Canari' /* demandé par Dupont */"
    )
    body = tool_call("execute_sql", {"query": sql})
    async with bench(sql_tools=SQL_TOOLS) as b:
        response = await exchange("POST", b.proxy_url + "/mcp", content=body)
        assert (response.status, response.body) == (200, TOOL_RESULT)
        await b.proxy.drain()

    (event,) = b.sink.events
    serialized = json.dumps(event, ensure_ascii=False)
    assert not SECRETS.search(serialized)
    assert event["job"]["facets"]["sql"] == {
        "_producer": event["producer"],
        "_schemaURL": (
            "https://openlineage.io/spec/facets/1-1-0/SQLJobFacet.json#/$defs/SQLJobFacet"
        ),
        "query": "SELECT c.nom, c.solde FROM `crm.clients` AS c WHERE c.nom = ?",
        "dialect": "bigquery",
    }


@pytest.mark.anyio
async def test_an_event_without_a_safe_shape_has_no_sql_facet(bench: BenchFactory) -> None:
    body = tool_call("execute_sql", {"query": "SELEC nom FRM clients WHERE nom = 'Dupont'"})
    async with bench(sql_tools=SQL_TOOLS) as b:
        await exchange("POST", b.proxy_url + "/mcp", content=body)
        await b.proxy.drain()
    (event,) = b.sink.events
    assert "facets" not in event["job"]
    assert not SECRETS.search(json.dumps(event))
