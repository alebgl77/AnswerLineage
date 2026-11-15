"""Analyse du SQL : tables et colonnes lues par une requête.

L'analyse est prudente. Elle ne décrit une lecture que si elle la comprend :

- une colonne n'est rattachée à une table que si ce rattachement est certain ;
- une table dont les colonnes lues ne sont pas toutes connues (``SELECT *``,
  colonne sans préfixe dans une jointure) est rendue au niveau « table » ;
- tout le reste (syntaxe inconnue, SQL dynamique, ``UPDATE``, ``DELETE``,
  ``MERGE``, procédure, requête trop longue) est « non résolu ».

La même lecture de la requête en donne la forme sans littéraux (``shape``) :
l'arbre syntaxique, où toute constante est remplacée par le marqueur ``?``,
réécrit sans ses commentaires. Les noms (tables, colonnes, fonctions, alias)
restent. Au moindre doute, aucune forme n'est rendue : mieux vaut pas de forme
qu'une fuite.

``analyze_sql`` ne lève jamais d'exception et ne journalise jamais la requête :
un littéral peut être un contenu, et aucun contenu n'est conservé.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, get_args

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect as SqlglotDialect
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.tokenizer_core import Token, TokenType

Dialect = Literal["bigquery", "snowflake", "postgres"]
Precision = Literal["table", "columns"]
DIALECTS: tuple[str, ...] = get_args(Dialect)

# Au-delà, la requête n'est pas analysée.
MAX_SQL_BYTES = 100_000
# Au-delà, la forme sans littéraux n'est pas gardée.
MAX_SHAPE_CHARS = 32_768


class _DropEverything(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


def silence_sqlglot_logs() -> None:
    """Coupe le journal de sqlglot, qui cite la requête dans ses avertissements.

    Le niveau et le filtre résistent à une reconfiguration des journaux par
    ``logging.config.dictConfig`` (celle du serveur HTTP, par exemple), qui
    réactive les journaux simplement désactivés.
    """
    sqlglot_logger = logging.getLogger("sqlglot")
    sqlglot_logger.setLevel(logging.CRITICAL + 1)
    if not any(isinstance(item, _DropEverything) for item in sqlglot_logger.filters):
        sqlglot_logger.addFilter(_DropEverything())


silence_sqlglot_logs()

# Seul un identifiant de cette forme est recopié dans un événement. Le proxy
# l'applique aussi à ce que rend le processus d'analyse.
IDENTIFIER = re.compile(r"[\w$*-]{1,300}")

# Instructions sans lecture de données.
_NO_READ = (exp.Show, exp.Describe, exp.Use, exp.Set, exp.Transaction, exp.Commit, exp.Rollback)


@dataclass(frozen=True)
class TableRead:
    """Lecture d'une table par une requête."""

    name: str
    precision: Precision
    # Colonnes lues, triées. Vide au niveau « table ».
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class SqlAnalysis:
    resolved: bool
    tables: tuple[TableRead, ...] = ()
    # Code du motif quand la lecture n'est pas résolue.
    reason: str | None = None
    # Forme de la requête sans ses littéraux, quand elle est sûre. Une lecture non
    # résolue peut en avoir une : un UPDATE se lit en entier, sans lecture connue.
    shape: str | None = None


@dataclass
class _Table:
    columns: set[str] = field(default_factory=set)
    # Vrai dès que toutes les colonnes lues ne sont pas connues.
    whole: bool = False


class _UnresolvedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unresolved(reason: str) -> SqlAnalysis:
    return SqlAnalysis(resolved=False, reason=reason)


def _table_name(table: exp.Table) -> str:
    if not isinstance(table.this, exp.Identifier):
        # Fonction de table : elle peut lire n'importe quoi (requête fédérée, lien distant).
        raise _UnresolvedError("table_function")
    parts = [part for part in (table.catalog, table.db, table.name) if part]
    if not parts or not all(IDENTIFIER.fullmatch(part) for part in parts):
        raise _UnresolvedError("unsupported_identifier")
    return ".".join(parts)


def _physical_sources(scope: Scope) -> dict[str, exp.Table]:
    return {
        alias: source for alias, source in scope.sources.items() if isinstance(source, exp.Table)
    }


def _visible_scopes(scope: Scope) -> list[Scope]:
    """La portée et, pour une sous-requête d'expression, celles qui l'englobent."""
    scopes = [scope]
    current = scope
    while current.is_subquery and current.parent is not None:
        current = current.parent
        scopes.append(current)
    return scopes


def _lookup(scope: Scope, alias: str) -> object:
    """Source désignée par un préfixe de colonne, cherchée de la portée vers l'extérieur."""
    current: Scope | None = scope
    while current is not None:
        if alias in current.sources:
            return current.sources[alias]
        current = current.parent
    return None


def _column_name(column: exp.Column) -> str:
    name = column.name
    if not IDENTIFIER.fullmatch(name):
        raise _UnresolvedError("unsupported_identifier")
    return name


def _collect(scope: Scope, reads: dict[str, _Table]) -> None:
    """Relève les lectures d'une portée. Dans le doute, la table est lue en entier."""
    expression = scope.expression
    physical = _physical_sources(scope)

    def entry(table: exp.Table) -> _Table:
        return reads.setdefault(_table_name(table), _Table())

    def whole(tables: list[exp.Table]) -> None:
        for table in tables:
            entry(table).whole = True

    local = list(physical.values())
    for table in local:
        # Les colonnes d'une table pivotée ne se déduisent pas de la requête.
        if table.args.get("pivots"):
            entry(table).whole = True
        else:
            entry(table)

    if any(isinstance(projection, exp.Star) for projection in getattr(expression, "selects", [])):
        whole(local)
    for join in expression.args.get("joins") or []:
        if join.method == "NATURAL":
            whole(local)
        # JOIN ... USING (c) lit la colonne c des deux côtés.
        for identifier in join.args.get("using") or []:
            if not IDENTIFIER.fullmatch(identifier.name):
                raise _UnresolvedError("unsupported_identifier")
            for table in local:
                entry(table).columns.add(identifier.name)

    visible = _visible_scopes(scope)
    visible_tables = [table for item in visible for table in _physical_sources(item).values()]
    visible_aliases = {
        alias: table
        for item in reversed(visible)
        for alias, table in _physical_sources(item).items()
    }
    only_tables = all(len(item.sources) == len(_physical_sources(item)) for item in visible)
    output_names = set(getattr(expression, "named_selects", []))

    for column in scope.find_all(exp.Column):
        qualifier = column.table
        if isinstance(column.this, exp.Star):
            # t.* : toutes les colonnes de la table.
            source = _lookup(scope, qualifier) if qualifier else None
            if isinstance(source, exp.Table):
                entry(source).whole = True
            continue
        if qualifier:
            source = _lookup(scope, qualifier)
            if isinstance(source, exp.Table):
                entry(source).columns.add(_column_name(column))
            elif source is None:
                # Préfixe inconnu : champ d'une structure, ou alias hors de portée.
                whole(visible_tables)
            continue

        name = column.name
        if name in visible_aliases:
            # SELECT t FROM t : la ligne entière.
            entry(visible_aliases[name]).whole = True
        elif name in output_names and column.find_ancestor(exp.Order) is not None:
            # ORDER BY sur le nom d'une colonne de sortie : aucune lecture de plus.
            continue
        elif len(visible_tables) == 1 and only_tables:
            entry(visible_tables[0]).columns.add(_column_name(column))
        else:
            # Colonne sans préfixe et plusieurs sources possibles : pas de rattachement sûr.
            whole(visible_tables)


def _statement_reads(
    statement: exp.Expr,
    dialect: Dialect,
    default_catalog: str | None,
    default_db: str | None,
    reads: dict[str, _Table],
) -> None:
    if isinstance(statement, _NO_READ):
        return
    if isinstance(statement, exp.Create) and not isinstance(statement.expression, exp.Query):
        return
    if isinstance(statement, exp.Insert | exp.Create):
        if not isinstance(statement.expression, exp.Query):
            raise _UnresolvedError("unsupported_statement")
    elif not isinstance(statement, exp.Query):
        raise _UnresolvedError("unsupported_statement")
    if any(True for _ in statement.find_all(exp.Command)):
        raise _UnresolvedError("unsupported_statement")

    def qualified_statement(*, columns: bool) -> exp.Expr:
        return qualify(
            statement.copy(),
            dialect=dialect,
            catalog=default_catalog,
            db=default_db,
            qualify_columns=columns,
            validate_qualify_columns=False,
            quote_identifiers=False,
            identify=False,
        )

    try:
        qualified = qualified_statement(columns=True)
    except OptimizeError:
        # Sans schéma, sqlglot ne sait pas rattacher une colonne sans préfixe d'une
        # sous-requête corrélée. Les tables restent qualifiées ; la colonne est
        # traitée comme ambiguë par _collect.
        qualified = qualified_statement(columns=False)
    query = qualified.expression if isinstance(qualified, exp.Insert | exp.Create) else qualified
    root = build_scope(query)
    if root is None:
        raise _UnresolvedError("unsupported_statement")
    scopes = list(root.traverse())
    for scope in scopes:
        _collect(scope, reads)

    # Filet de sécurité : toute table de la requête doit avoir été vue comme la
    # source d'une portée. Sinon la requête contient une forme que l'analyse ne
    # couvre pas, et une lecture pourrait manquer.
    understood = {
        id(source)
        for scope in scopes
        for source in scope.sources.values()
        if isinstance(source, exp.Table)
    }
    cte_names = {cte.alias_or_name for cte in query.find_all(exp.CTE)}
    for table in query.find_all(exp.Table):
        is_cte_reference = not table.db and table.name in cte_names
        if id(table) not in understood and not is_cte_reference:
            raise _UnresolvedError("not_understood")


# Écritures d'une constante, telles que le découpage du SQL les reconnaît.
_LITERAL_TOKENS = frozenset(
    {
        TokenType.STRING,
        TokenType.NUMBER,
        TokenType.BIT_STRING,
        TokenType.HEX_STRING,
        TokenType.BYTE_STRING,
        TokenType.NATIONAL_STRING,
        TokenType.NATIONAL_RAW_STRING,
        TokenType.RAW_STRING,
        TokenType.HEREDOC_STRING,
        TokenType.UNICODE_STRING,
    }
)
# Constantes de l'arbre syntaxique. Un chemin JSON (``j ->> 'cle'``) est une
# chaîne écrite par l'auteur de la requête : il devient un marqueur, lui aussi.
_CONSTANTS = (
    exp.Literal,
    exp.Boolean,
    exp.HexString,
    exp.BitString,
    exp.ByteString,
    exp.National,
    exp.RawString,
    exp.UnicodeString,
    exp.JSONPath,
)
# Seules chaînes admises dans une forme : les unités de temps, que certains
# dialectes écrivent entre apostrophes (``DATE_TRUNC('MONTH', d)`` en PostgreSQL).
# Un vocabulaire clos, qui ne dit rien des données.
_TIME_UNITS = frozenset(
    {
        "NANOSECOND",
        "MICROSECOND",
        "MILLISECOND",
        "SECOND",
        "MINUTE",
        "HOUR",
        "DAY",
        "WEEK",
        "ISOWEEK",
        "MONTH",
        "QUARTER",
        "YEAR",
        "ISOYEAR",
        "DECADE",
        "CENTURY",
        "MILLENNIUM",
        "DOW",
        "DOY",
        "ISODOW",
        "EPOCH",
    }
)
# Marques d'un commentaire : aucune forme n'en contient, même dans un nom.
_COMMENT_MARKERS = ("--", "/*", "*/")
# Noms d'une forme redécoupée, écrits nus ou entre guillemets.
_NAME_TOKENS = frozenset({TokenType.VAR, TokenType.IDENTIFIER})


def is_plain_shape(shape: str) -> bool:
    """Contrôle de surface d'une forme : bornée, imprimable, sans commentaire.

    Le proxy l'applique aussi à ce que rend le processus d'analyse.
    """
    return (
        0 < len(shape) <= MAX_SHAPE_CHARS
        and shape.isprintable()
        and not any(marker in shape for marker in _COMMENT_MARKERS)
    )


def _is_admitted_name(name: str) -> bool:
    """Vrai pour un nom qu'un événement peut recopier, qualifié ou non.

    Un nom entre guillemets est un texte libre : seul celui qui a la forme d'un
    identifiant reste dans une forme, comme pour les tables et les colonnes lues.
    """
    return all(IDENTIFIER.fullmatch(part) for part in name.split("."))


def _marker() -> exp.Placeholder:
    # « jdbc » : le marqueur s'écrit « ? » dans tous les dialectes, « %s » sinon en PostgreSQL.
    return exp.Placeholder(jdbc=True)


def _is_constant(node: exp.Expr) -> bool:
    if isinstance(node, _CONSTANTS):
        return True
    if isinstance(node, exp.Placeholder):
        # Un marqueur reçu sans nom : réécrit comme les autres.
        return node.this is None
    if isinstance(node, exp.Neg | exp.Interval | exp.Parameter):
        # -5, INTERVAL '3 days', $1 : la constante entière.
        return isinstance(node.this, exp.Expr) and _is_constant(node.this)
    return False


def _without_constants(statement: exp.Expr) -> exp.Expr:
    """Copie de l'instruction où chaque constante est un marqueur, sans commentaire."""
    copy = statement.copy()

    def stops(node: exp.Expr) -> bool:
        return _is_constant(node) or isinstance(node, exp.Command)

    constants = [node for node in copy.walk(prune=stops) if stops(node)]
    for node in constants:
        if isinstance(node, exp.Command) or node is copy:
            # Repli de l'analyseur sur du texte brut : la requête n'est pas lue en entier.
            raise _UnresolvedError("not_understood")
        node.replace(_marker())
    for node in copy.find_all(exp.In):
        # IN (?, ?, ?) devient IN (?) : la forme ne dépend pas de la longueur de la liste.
        items = node.expressions
        if items and all(isinstance(item, exp.Placeholder) for item in items):
            node.set("expressions", [_marker()])
    for node in copy.walk():
        node.comments = None
    return copy


def _is_doubtless(shape: str, source: Sequence[Token], dialect: SqlglotDialect) -> bool:
    """Vrai si la forme ne garde rien d'une constante, d'un commentaire ni d'un texte libre.

    La forme est redécoupée : aucune constante, sauf une unité de temps, ni aucun
    commentaire n'y figure. Aucun de ses mots n'est le texte d'une constante de
    la requête qui n'y était pas aussi écrite comme un nom : une constante que
    l'analyseur aurait changée en nom serait sinon recopiée. Et chacun de ses
    noms a la forme d'un identifiant : un nom entre guillemets peut porter une
    donnée, une instruction ou une balise.
    """
    if not is_plain_shape(shape):
        return False
    written = {token.text.upper() for token in source if token.token_type in _LITERAL_TOKENS}
    named = {token.text.upper() for token in source if token.token_type not in _LITERAL_TOKENS}
    hidden = written - named
    for token in dialect.tokenize(shape):
        word = token.text.upper()
        if token.comments:
            return False
        if token.token_type in _LITERAL_TOKENS:
            if word not in _TIME_UNITS:
                return False
        elif word in hidden or (
            token.token_type in _NAME_TOKENS and not _is_admitted_name(token.text)
        ):
            return False
    return True


def _shape(
    statements: Sequence[exp.Expr], source: Sequence[Token], dialect: SqlglotDialect
) -> str | None:
    """Forme sans littéraux des instructions, ``None`` au moindre doute."""
    try:
        shape = "; ".join(
            _without_constants(statement).sql(dialect=dialect, comments=False)
            for statement in statements
        )
        return shape if _is_doubtless(shape, source, dialect) else None
    except Exception:
        # Construction impossible, ou trop profonde : pas de forme.
        return None


def analyze_sql(
    sql: str,
    dialect: Dialect,
    *,
    default_catalog: str | None = None,
    default_db: str | None = None,
    max_bytes: int = MAX_SQL_BYTES,
) -> SqlAnalysis:
    """Déduit d'une requête SQL les tables et les colonnes qu'elle lit, et sa forme."""
    if dialect not in DIALECTS:
        return _unresolved("unsupported_dialect")
    silence_sqlglot_logs()
    shape: str | None = None
    try:
        if len(sql.encode("utf-8", errors="replace")) > max_bytes:
            return _unresolved("too_large")
        reader = SqlglotDialect.get_or_raise(dialect)
        # Un seul découpage : il sert à l'arbre et au contrôle de la forme.
        tokens = reader.tokenize(sql)
        statements = [item for item in reader.parser().parse(tokens, sql) if item is not None]
        if not statements:
            return _unresolved("empty")
        shape = _shape(statements, tokens, reader)
        reads: dict[str, _Table] = {}
        for statement in statements:
            _statement_reads(statement, dialect, default_catalog, default_db, reads)
    except _UnresolvedError as error:
        return SqlAnalysis(resolved=False, reason=error.reason, shape=shape)
    except RecursionError:
        return SqlAnalysis(resolved=False, reason="too_deep", shape=shape)
    except Exception:
        # Erreur de syntaxe ou cas que l'analyseur ne couvre pas : l'appel passe,
        # la lecture est non résolue. Le message, qui cite la requête, est ignoré.
        return SqlAnalysis(resolved=False, reason="not_understood", shape=shape)

    tables = []
    for name in sorted(reads):
        table = reads[name]
        if table.whole or not table.columns:
            tables.append(TableRead(name=name, precision="table"))
        else:
            tables.append(
                TableRead(name=name, precision="columns", columns=tuple(sorted(table.columns)))
            )
    return SqlAnalysis(resolved=True, tables=tuple(tables), shape=shape)
