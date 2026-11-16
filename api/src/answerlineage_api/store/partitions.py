"""Partitions mensuelles des tables qui grossissent avec le trafic.

Les événements bruts, les relevés de couverture et le compteur de doublons sont
partitionnés par mois. La clé de partition est posée par la base à l'insertion
et ne change plus : le mois de réception (``recorded_month``), ou le jour de
réception pour le compteur de doublons. Ce n'est jamais l'heure déclarée par
l'émetteur, qui peut se dire de 1970, et une mise à jour ne déplace jamais une
ligne d'une partition à l'autre.

Une partition couvre un mois UTC, du premier jour inclus au premier jour du
mois suivant exclu, et se nomme d'après sa table et son mois :
``event_raw_2026_11``. Les migrations en créent pour le mois courant et les deux
suivants ; la maintenance des workers les crée ensuite à l'avance et purge
celles qui sortent de la conservation.

Le graphe n'est pas partitionné : ses écritures fusionnent des événements sur
des clés naturelles (run, jeu de données, réponse) qui ne contiennent aucune
heure de réception. Les y ajouter casserait l'idempotence et la commutativité.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy import Connection, Table, text

# Tables partitionnées, et leur clé de partition.
PARTITIONED: Final = {
    "event_raw": "recorded_month",
    "coverage_report": "recorded_month",
    "ingest_duplicate": "day",
}
# Mois créés d'avance après le mois courant : au moins deux.
DEFAULT_AHEAD_MONTHS: Final = 2

# Le mois UTC d'une heure de réception, tel que la base le pose.
RECEPTION_MONTH: Final = "CAST(date_trunc('month', timezone('UTC', {column})) AS date)"
CURRENT_MONTH: Final = text(f"SELECT {RECEPTION_MONTH.format(column='now()')}")


def month_of(moment: datetime) -> date:
    """Le premier jour du mois UTC de cet instant."""
    utc = moment.astimezone(UTC)
    return date(utc.year, utc.month, 1)


def shift(month: date, months: int) -> date:
    """Le premier jour du mois situé ``months`` mois plus loin (ou plus tôt)."""
    index = month.year * 12 + month.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def partition_name(table: str, month: date) -> str:
    return f"{table}_{month:%Y_%m}"


def create_partition(table: str, month: date) -> str:
    """L'instruction qui crée la partition d'un mois. Seuls des noms constants y entrent."""
    if table not in PARTITIONED:
        raise ValueError("Cette table n'est pas partitionnée.")
    return (
        f'CREATE TABLE "{partition_name(table, month)}" PARTITION OF "{table}"'
        f" FOR VALUES FROM ('{month.isoformat()}') TO ('{shift(month, 1).isoformat()}')"
    )


def create_partitions_ahead(target: Table, connection: Connection, **_: object) -> None:
    """Crée, avec la table, la partition du mois courant et des mois à venir."""
    current: date = connection.execute(CURRENT_MONTH).scalar_one()
    for ahead in range(DEFAULT_AHEAD_MONTHS + 1):
        connection.execute(text(create_partition(target.name, shift(current, ahead))))
