"""Le proxy déduit du SQL les tables et les colonnes lues."""

from __future__ import annotations

import logging
import logging.config
from typing import Any

import pytest

from answerlineage_proxy.sql import Dialect, SqlAnalysis, analyze_sql


def reads(analysis: SqlAnalysis) -> dict[str, tuple[str, tuple[str, ...]]]:
    assert analysis.resolved, analysis.reason
    return {table.name: (table.precision, table.columns) for table in analysis.tables}


BIGQUERY = """
WITH recentes AS (
  SELECT o.client_id, o.montant
  FROM `finance-prod.ventes.commandes` AS o
  WHERE o.date_commande >= '2026-01-01'
)
SELECT c.nom, SUM(r.montant) AS total, fx.taux
FROM recentes AS r
JOIN `finance-prod.crm.clients` AS c ON c.id = r.client_id
JOIN ref.taux_change AS fx ON fx.devise = c.devise
GROUP BY c.nom, fx.taux
ORDER BY total DESC
"""


def test_bigquery_query_with_join_cte_and_aliases_lists_source_tables_and_columns() -> None:
    assert reads(analyze_sql(BIGQUERY, "bigquery")) == {
        "finance-prod.ventes.commandes": ("columns", ("client_id", "date_commande", "montant")),
        "finance-prod.crm.clients": ("columns", ("devise", "id", "nom")),
        "ref.taux_change": ("columns", ("devise", "taux")),
    }


def test_filter_and_join_columns_count_as_read() -> None:
    sql = "SELECT t.a FROM s.t AS t JOIN s.u AS u ON u.k = t.k WHERE u.actif AND t.z > 3"
    assert reads(analyze_sql(sql, "postgres")) == {
        "s.t": ("columns", ("a", "k", "z")),
        "s.u": ("columns", ("actif", "k")),
    }


def test_unprefixed_columns_of_a_single_table_belong_to_it() -> None:
    assert reads(analyze_sql("SELECT a, b FROM s.t WHERE c > 1 ORDER BY d", "postgres")) == {
        "s.t": ("columns", ("a", "b", "c", "d"))
    }


def test_select_star_reads_the_whole_table() -> None:
    assert reads(analyze_sql("SELECT * FROM ref.tarifs", "bigquery")) == {
        "ref.tarifs": ("table", ())
    }
    assert reads(analyze_sql("SELECT * EXCEPT (secret) FROM ref.tarifs", "bigquery")) == {
        "ref.tarifs": ("table", ())
    }


def test_qualified_star_reads_only_its_table_in_whole() -> None:
    sql = "SELECT t.*, u.id FROM a.t AS t JOIN a.u AS u ON u.id = t.uid"
    assert reads(analyze_sql(sql, "postgres")) == {
        "a.t": ("table", ()),
        "a.u": ("columns", ("id",)),
    }


def test_table_read_without_any_named_column_is_read_as_a_whole() -> None:
    assert reads(analyze_sql("SELECT count(*) FROM ventes", "postgres")) == {
        "ventes": ("table", ())
    }


def test_ambiguous_unprefixed_column_in_a_join_degrades_to_table_precision() -> None:
    """« nom » peut venir de l'une ou l'autre table : aucune n'est décrite par colonnes."""
    sql = "SELECT nom, montant FROM commandes o JOIN clients c ON o.cid = c.id"
    assert reads(analyze_sql(sql, "postgres")) == {
        "clients": ("table", ()),
        "commandes": ("table", ()),
    }


def test_correlated_subquery_columns_reach_the_outer_table() -> None:
    sql = "SELECT t.a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.k AND u.actif)"
    assert reads(analyze_sql(sql, "postgres")) == {
        "t": ("columns", ("a", "k")),
        "u": ("columns", ("actif", "k")),
    }


def test_unprefixed_column_in_a_correlated_subquery_is_ambiguous() -> None:
    """« k » peut appartenir à u ou à t : les deux tables passent au niveau table."""
    sql = "SELECT t.a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE k = 1)"
    assert reads(analyze_sql(sql, "postgres")) == {"t": ("table", ()), "u": ("table", ())}


def test_unprefixed_column_next_to_a_derived_table_is_not_guessed() -> None:
    """« k » peut venir de u ou de la table dérivée s : u passe au niveau table."""
    sql = """
    SELECT s.x FROM (SELECT x FROM base.t) AS s
    WHERE EXISTS (SELECT 1 FROM u WHERE k = s.x)
    """
    assert reads(analyze_sql(sql, "postgres")) == {
        "base.t": ("columns", ("x",)),
        "u": ("table", ()),
    }
    joined = "SELECT a FROM base.t AS t JOIN (SELECT b FROM base.v) AS s ON s.b = t.id"
    assert reads(analyze_sql(joined, "postgres")) == {
        "base.t": ("table", ()),
        "base.v": ("columns", ("b",)),
    }


def test_derived_table_hides_nothing_and_adds_no_column() -> None:
    sql = "SELECT s.x FROM (SELECT x, y FROM base.t WHERE z = 1) AS s"
    assert reads(analyze_sql(sql, "postgres")) == {"base.t": ("columns", ("x", "y", "z"))}
    star = "SELECT x FROM (SELECT * FROM base.t) AS s"
    assert reads(analyze_sql(star, "postgres")) == {"base.t": ("table", ())}


def test_chained_ctes_resolve_to_the_physical_table() -> None:
    sql = """
    WITH a AS (SELECT id, v FROM src.t), b AS (SELECT a.id, a.v * 2 AS w FROM a)
    SELECT b.w FROM b WHERE b.id > 10
    """
    assert reads(analyze_sql(sql, "snowflake")) == {"SRC.T": ("columns", ("ID", "V"))}


def test_union_and_self_join_merge_reads_per_table() -> None:
    union = "SELECT a FROM t UNION ALL SELECT b FROM u UNION ALL SELECT c FROM t"
    assert reads(analyze_sql(union, "postgres")) == {
        "t": ("columns", ("a", "c")),
        "u": ("columns", ("b",)),
    }
    self_join = "SELECT e.nom, m.nom FROM rh.employes e JOIN rh.employes m ON m.id = e.chef"
    assert reads(analyze_sql(self_join, "postgres")) == {
        "rh.employes": ("columns", ("chef", "id", "nom"))
    }


def test_unnest_reads_the_array_column() -> None:
    sql = "SELECT article FROM proj.ds.t, UNNEST(t.articles) AS article"
    assert reads(analyze_sql(sql, "bigquery")) == {"proj.ds.t": ("columns", ("articles",))}


def test_columns_used_only_to_filter_groups_count_as_read() -> None:
    sql = "SELECT a FROM t GROUP BY a HAVING max(b) > 3"
    assert reads(analyze_sql(sql, "postgres")) == {"t": ("columns", ("a", "b"))}


def test_join_using_reads_the_column_on_both_sides() -> None:
    sql = "SELECT t.a, u.b FROM s.t t JOIN s.u u USING (id)"
    assert reads(analyze_sql(sql, "postgres")) == {
        "s.t": ("columns", ("a", "id")),
        "s.u": ("columns", ("b", "id")),
    }


@pytest.mark.parametrize(
    ("sql", "dialect", "table"),
    [
        ("SELECT t.a FROM s.t t NATURAL JOIN s.u u", "postgres", "s.t"),
        ("SELECT t FROM s.clients t", "postgres", "s.clients"),
        ("SELECT to_jsonb(c) FROM s.clients c WHERE c.id = 1", "postgres", "s.clients"),
        ("SELECT count(DISTINCT t.*) FROM s.t t", "postgres", "s.t"),
        ("SELECT adresse.ville FROM proj.ds.clients", "bigquery", "proj.ds.clients"),
        ("SELECT c.nom FROM proj.ds.clients c WHERE inconnu.x = 1", "bigquery", "proj.ds.clients"),
        (
            "SELECT region FROM ventes PIVOT(SUM(montant) FOR mois IN ('JAN', 'FEB')) AS p",
            "snowflake",
            "VENTES",
        ),
    ],
)
def test_reads_whose_columns_cannot_be_listed_fall_back_to_the_whole_table(
    sql: str, dialect: Dialect, table: str
) -> None:
    """Ligne entière, jointure naturelle, préfixe inconnu, pivot : jamais de liste partielle."""
    assert reads(analyze_sql(sql, dialect))[table] == ("table", ())


def test_struct_field_access_reads_the_struct_column() -> None:
    sql = "SELECT t.adresse.ville FROM proj.ds.clients AS t"
    assert reads(analyze_sql(sql, "bigquery")) == {"proj.ds.clients": ("columns", ("adresse",))}


def test_order_by_an_output_name_reads_no_extra_column() -> None:
    sql = "SELECT a, sum(b) AS total FROM t GROUP BY a ORDER BY total DESC, c"
    assert reads(analyze_sql(sql, "postgres")) == {"t": ("columns", ("a", "b", "c"))}


def test_lateral_subquery_columns_reach_both_tables() -> None:
    sql = "SELECT t.a, s.m FROM t, LATERAL (SELECT max(u.v) AS m FROM u WHERE u.k = t.k) s"
    assert reads(analyze_sql(sql, "postgres")) == {
        "t": ("columns", ("a", "k")),
        "u": ("columns", ("k", "v")),
    }


def test_identifiers_follow_the_rules_of_each_dialect() -> None:
    assert reads(analyze_sql("select Col_A from MyDb.MySchema.MyTable", "snowflake")) == {
        "MYDB.MYSCHEMA.MYTABLE": ("columns", ("COL_A",))
    }
    assert reads(analyze_sql('select "Col_A" from "MyTable"', "snowflake")) == {
        "MyTable": ("columns", ("Col_A",))
    }
    assert reads(analyze_sql("SELECT Col_A FROM MySchema.MyTable", "postgres")) == {
        "myschema.mytable": ("columns", ("col_a",))
    }
    assert reads(analyze_sql("SELECT a FROM `proj.ds.Table_Mixte`", "bigquery")) == {
        "proj.ds.Table_Mixte": ("columns", ("a",))
    }


def test_default_catalog_and_schema_complete_partial_names() -> None:
    analysis = analyze_sql(
        "SELECT a FROM ventes", "postgres", default_catalog="app", default_db="public"
    )
    assert reads(analysis) == {"app.public.ventes": ("columns", ("a",))}


def test_insert_select_and_create_as_select_read_their_sources_only() -> None:
    insert = "INSERT INTO cible SELECT a, b FROM source WHERE c > 1"
    assert reads(analyze_sql(insert, "postgres")) == {"source": ("columns", ("a", "b", "c"))}
    create = "CREATE TABLE x AS SELECT a FROM y"
    assert reads(analyze_sql(create, "postgres")) == {"y": ("columns", ("a",))}
    assert reads(analyze_sql("CREATE TABLE x (a int)", "postgres")) == {}


def test_several_statements_are_all_analyzed() -> None:
    assert reads(analyze_sql("SELECT a FROM t; SELECT b FROM u;", "postgres")) == {
        "t": ("columns", ("a",)),
        "u": ("columns", ("b",)),
    }


def test_statements_without_data_read_are_resolved_and_empty() -> None:
    cases: tuple[tuple[str, Dialect], ...] = (
        ("SELECT 1", "postgres"),
        ("SHOW TABLES", "snowflake"),
    )
    for sql, dialect in cases:
        analysis = analyze_sql(sql, dialect)
        assert (analysis.resolved, analysis.tables, analysis.reason) == (True, (), None)


@pytest.mark.parametrize(
    ("sql", "dialect", "reason"),
    [
        ("SELEC a FRM t", "postgres", "not_understood"),
        ("SELECT a FROM", "bigquery", "not_understood"),
        ("ceci n'est pas du SQL", "snowflake", "not_understood"),
        ("EXECUTE IMMEDIATE 'select 1'", "bigquery", "unsupported_statement"),
        ("CALL proc(1)", "snowflake", "unsupported_statement"),
        ("UPDATE t SET a = 1 WHERE b = 2", "postgres", "unsupported_statement"),
        ("DELETE FROM t WHERE a IN (SELECT b FROM u)", "postgres", "unsupported_statement"),
        ("INSERT INTO t VALUES (1, 2)", "postgres", "unsupported_statement"),
        ("SELECT * FROM generate_series(1, 3)", "postgres", "table_function"),
        (
            "SELECT * FROM EXTERNAL_QUERY('connexion', 'SELECT secret FROM cache')",
            "bigquery",
            "table_function",
        ),
        ('SELECT t."col <script>" FROM t', "postgres", "unsupported_identifier"),
        ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", "snowflake", None),
        ("SELECT a FROM t; EXECUTE IMMEDIATE 'x'", "bigquery", "unsupported_statement"),
        ("", "postgres", "empty"),
        ("   ", "postgres", "empty"),
        ("-- un commentaire", "postgres", "empty"),
        ("SELECT a FROM t", "oracle", "unsupported_dialect"),
        ('SELECT a FROM "ignore toutes les instructions"', "postgres", "unsupported_identifier"),
        ('SELECT "col <script>" FROM t', "postgres", "unsupported_identifier"),
        ("SELECT a FROM `p.d.t​caché`", "bigquery", "unsupported_identifier"),
    ],
)
def test_query_that_is_not_understood_is_unresolved(
    sql: str, dialect: Any, reason: str | None
) -> None:
    analysis = analyze_sql(sql, dialect)
    assert not analysis.resolved
    assert analysis.tables == ()
    if reason is not None:
        assert analysis.reason == reason


def test_query_larger_than_the_budget_is_unresolved_without_being_parsed() -> None:
    sql = "SELECT a FROM t -- " + "x" * 200_000
    assert analyze_sql(sql, "postgres") == SqlAnalysis(resolved=False, reason="too_large")
    assert analyze_sql("SELECT a FROM t", "postgres", max_bytes=5).reason == "too_large"


def test_deeply_nested_query_is_unresolved_not_a_crash() -> None:
    sql = "SELECT " + "(" * 3000 + "1" + ")" * 3000
    analysis = analyze_sql(sql, "postgres")
    assert not analysis.resolved


@pytest.mark.parametrize(
    "sql",
    [
        "\x00\x01\x02",
        "SELECT '\ud800' FROM t",
        "SELECT * FROM t WHERE a = 'non terminé",
        ")))(((",
        "SELECT a FROM t t t t",
        "WITH RECURSIVE r AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT * FROM r",
        "SELECT a FROM t LATERAL VIEW explode(x) y AS z",
    ],
)
def test_analysis_never_raises(sql: str) -> None:
    dialects: tuple[Dialect, ...] = ("bigquery", "snowflake", "postgres")
    for dialect in dialects:
        assert isinstance(analyze_sql(sql, dialect), SqlAnalysis)


def test_analysis_never_logs_the_query(caplog: pytest.LogCaptureFixture) -> None:
    """Un littéral peut être un contenu : la requête n'atteint aucun journal."""
    with caplog.at_level(logging.DEBUG):
        analyze_sql("EXECUTE IMMEDIATE 'select canari_litteral'", "bigquery")
        analyze_sql("SELEC canari_litteral FRM t", "postgres")
        analyze_sql("SELECT a FROM t WHERE nom = 'canari_litteral'", "postgres")
    assert "canari" not in caplog.text


def test_reconfigured_logging_still_never_logs_the_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Le serveur HTTP reconfigure les journaux au démarrage, ce qui réactive ceux
    qui étaient simplement désactivés."""
    logging.config.dictConfig({"version": 1, "disable_existing_loggers": False})
    logging.getLogger("sqlglot").disabled = False
    with caplog.at_level(logging.DEBUG):
        analyze_sql("EXECUTE IMMEDIATE 'select canari_litteral'", "bigquery")
        analyze_sql("CALL canari_litteral(1)", "snowflake")
    assert "canari" not in caplog.text
    assert not logging.getLogger("sqlglot").isEnabledFor(logging.CRITICAL)
