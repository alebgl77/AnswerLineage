"""Partitions mensuelles : événements bruts, relevés de couverture, compteur de doublons.

Chaque table est recréée, partitionnée par mois de réception, et ses lignes y
sont recopiées : une partition par mois où une ligne a été reçue, jusqu'au mois
courant et aux deux suivants. La clé de partition est posée par la base à la
réception (``recorded_month``, ou ``day`` pour le compteur de doublons) et ne
change plus.

La file de traitement désigne désormais l'événement par son identifiant et son
mois de réception, sans clé étrangère : une clé étrangère vers une table
partitionnée empêcherait d'en supprimer une partition. ``partition_alert``
garde les partitions manquantes que signale le contrôle des workers.

Les lignes sont mises de côté dans une copie temporaire le temps de recréer
la table : la table recréée reprend ainsi les noms que la base donne d'elle-même
à ses contraintes.

La descente recrée les tables sans partition et y recopie les lignes. Deux
lignes d'une même clé reçues à cheval sur deux mois se réunissent : un
événement est gardé une fois, une fenêtre de relevé garde ses plus grands
compteurs.

Révision : 0008
Précédente : 0007
"""

from __future__ import annotations

from datetime import date
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None

AHEAD_MONTHS = 2
_MONTH = "CAST(date_trunc('month', timezone('UTC', {column})) AS date)"


def _shift(month: date, months: int) -> date:
    index = month.year * 12 + month.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _create_partitions(table: str, oldest: str) -> None:
    """Une partition par mois, du plus ancien reçu (``oldest``, en UTC) aux mois à venir."""
    bind = op.get_bind()
    current: date = bind.execute(sa.text(f"SELECT {_MONTH.format(column='now()')}")).scalar_one()
    first: date | None = bind.execute(sa.text(oldest)).scalar_one()
    month = current if first is None else min(first, current)
    while month <= _shift(current, AHEAD_MONTHS):
        following = _shift(month, 1)
        op.execute(
            f'CREATE TABLE "{table}_{month:%Y_%m}" PARTITION OF "{table}"'
            f" FOR VALUES FROM ('{month.isoformat()}') TO ('{following.isoformat()}')"
        )
        month = following


def _recorded_at() -> sa.Column[object]:
    return sa.Column(
        "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _recorded_month(table: str) -> tuple[sa.Column[object], sa.CheckConstraint]:
    return (
        sa.Column(
            "recorded_month",
            sa.Date,
            nullable=False,
            server_default=sa.text(_MONTH.format(column="now()")),
        ),
        sa.CheckConstraint(
            f"recorded_month = {_MONTH.format(column='recorded_at')}",
            name=f"ck_{table}_recorded_month",
        ),
    )


def _event_raw(*keys: sa.schema.SchemaItem, **options: Any) -> None:
    op.create_table(
        "event_raw",
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("producer", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("emitter", sa.Text, nullable=False),
        sa.Column("tool_call", sa.Boolean, nullable=False),
        _recorded_at(),
        *keys,
        **options,
    )
    op.create_index("ix_event_raw_run_id", "event_raw", ["run_id"])
    op.create_index("ix_event_raw_recorded_at", "event_raw", ["recorded_at"])
    op.create_index(
        "ix_event_raw_emitter_event_time",
        "event_raw",
        ["emitter", "event_time"],
        postgresql_where=sa.text("tool_call"),
    )


def _coverage_report(*keys: sa.schema.SchemaItem, **options: Any) -> None:
    op.create_table(
        "coverage_report",
        sa.Column("emitter", sa.Text, nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls_seen", sa.BigInteger, nullable=False),
        sa.Column("events_emitted", sa.BigInteger, nullable=False),
        sa.Column("events_lost", sa.BigInteger, nullable=False),
        sa.Column("uninspected", sa.BigInteger, nullable=False),
        _recorded_at(),
        sa.CheckConstraint("window_start < window_end", name="ck_coverage_report_window"),
        sa.CheckConstraint(
            "calls_seen >= 0 AND events_emitted >= 0 AND events_lost >= 0 AND uninspected >= 0",
            name="ck_coverage_report_counts",
        ),
        *keys,
        **options,
    )


def _ingest_duplicate(**options: Any) -> None:
    op.create_table(
        "ingest_duplicate",
        sa.Column("emitter", sa.Text, nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("duplicates", sa.BigInteger, nullable=False),
        sa.PrimaryKeyConstraint("emitter", "day", name="pk_ingest_duplicate"),
        sa.CheckConstraint("duplicates > 0", name="ck_ingest_duplicate_positive"),
        **options,
    )
    op.create_index(
        "ix_ingest_duplicate_day_emitter", "ingest_duplicate", [sa.text("day DESC"), "emitter"]
    )


_EVENT_COLUMNS = (
    "event_id, run_id, event_type, event_time, producer, payload, emitter, tool_call, recorded_at"
)
_REPORT_COLUMNS = (
    "emitter, window_start, window_end, calls_seen, events_emitted, events_lost, uninspected,"
    " recorded_at"
)


def _oldest(month: str, table: str) -> str:
    """La requête du plus ancien mois de réception d'une table mise de côté."""
    return f"SELECT min({month}) FROM {table}"  # noqa: S608 — seuls des noms constants entrent.


def _set_aside(table: str) -> str:
    """Met les lignes d'une table de côté, puis la supprime.

    La copie est temporaire : elle disparaît avec la transaction de la migration.
    La table recréée reprend ainsi les noms que la base donne à ses contraintes.
    """
    copy = f"{table}_copy"
    op.execute(f'CREATE TEMPORARY TABLE "{copy}" ON COMMIT DROP AS TABLE "{table}"')
    op.drop_table(table)
    return copy


def upgrade() -> None:
    op.drop_constraint("fk_ingest_queue_event_id_event_raw", "ingest_queue", type_="foreignkey")

    events = _set_aside("event_raw")
    _event_raw(
        *_recorded_month("event_raw"),
        sa.PrimaryKeyConstraint("event_id", "recorded_month", name="pk_event_raw"),
        postgresql_partition_by="RANGE (recorded_month)",
    )
    _create_partitions("event_raw", _oldest(_MONTH.format(column="recorded_at"), events))
    op.execute(
        f"INSERT INTO event_raw ({_EVENT_COLUMNS}, recorded_month)"  # noqa: S608 — noms constants.
        f" SELECT {_EVENT_COLUMNS}, {_MONTH.format(column='recorded_at')} FROM {events}"
    )
    op.add_column("ingest_queue", sa.Column("recorded_month", sa.Date))
    op.execute(
        "UPDATE ingest_queue q SET recorded_month = e.recorded_month"
        " FROM event_raw e WHERE e.event_id = q.event_id"
    )
    op.alter_column("ingest_queue", "recorded_month", nullable=False)

    reports = _set_aside("coverage_report")
    _coverage_report(
        *_recorded_month("coverage_report"),
        sa.PrimaryKeyConstraint(
            "emitter", "window_start", "window_end", "recorded_month", name="pk_coverage_report"
        ),
        postgresql_partition_by="RANGE (recorded_month)",
    )
    _create_partitions("coverage_report", _oldest(_MONTH.format(column="recorded_at"), reports))
    op.execute(
        f"INSERT INTO coverage_report ({_REPORT_COLUMNS}, recorded_month)"  # noqa: S608 — idem.
        f" SELECT {_REPORT_COLUMNS}, {_MONTH.format(column='recorded_at')} FROM {reports}"
    )

    duplicates = _set_aside("ingest_duplicate")
    _ingest_duplicate(postgresql_partition_by="RANGE (day)")
    _create_partitions(
        "ingest_duplicate", _oldest("CAST(date_trunc('month', day) AS date)", duplicates)
    )
    op.execute(
        "INSERT INTO ingest_duplicate (emitter, day, duplicates)"  # noqa: S608 — idem.
        f" SELECT emitter, day, duplicates FROM {duplicates}"
    )

    op.create_table(
        "partition_alert",
        sa.Column("table_name", sa.Text, nullable=False),
        sa.Column("month", sa.Date, nullable=False),
        sa.Column("signalled_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("table_name", "month", name="pk_partition_alert"),
    )


def downgrade() -> None:
    op.drop_table("partition_alert")

    duplicates = _set_aside("ingest_duplicate")
    _ingest_duplicate()
    op.execute(
        "INSERT INTO ingest_duplicate (emitter, day, duplicates)"  # noqa: S608 — noms constants.
        f" SELECT emitter, day, duplicates FROM {duplicates}"
    )

    reports = _set_aside("coverage_report")
    _coverage_report(
        sa.PrimaryKeyConstraint("emitter", "window_start", "window_end", name="pk_coverage_report")
    )
    op.execute(
        f"INSERT INTO coverage_report ({_REPORT_COLUMNS})"  # noqa: S608 — idem.
        f" SELECT {_REPORT_COLUMNS} FROM {reports}"
        " ON CONFLICT (emitter, window_start, window_end) DO UPDATE SET"
        " calls_seen = GREATEST(coverage_report.calls_seen, EXCLUDED.calls_seen),"
        " events_emitted = GREATEST(coverage_report.events_emitted, EXCLUDED.events_emitted),"
        " events_lost = GREATEST(coverage_report.events_lost, EXCLUDED.events_lost),"
        " uninspected = GREATEST(coverage_report.uninspected, EXCLUDED.uninspected),"
        " recorded_at = LEAST(coverage_report.recorded_at, EXCLUDED.recorded_at)"
    )

    events = _set_aside("event_raw")
    _event_raw(sa.PrimaryKeyConstraint("event_id", name="pk_event_raw"))
    op.execute(
        f"INSERT INTO event_raw ({_EVENT_COLUMNS})"  # noqa: S608 — idem.
        f" SELECT {_EVENT_COLUMNS} FROM {events}"
        " ORDER BY recorded_at ON CONFLICT (event_id) DO NOTHING"
    )
    op.drop_column("ingest_queue", "recorded_month")
    op.create_foreign_key(
        "fk_ingest_queue_event_id_event_raw",
        "ingest_queue",
        "event_raw",
        ["event_id"],
        ["event_id"],
    )
