"""Couverture du lignage : relevés des émetteurs, émetteurs connus, appels par émetteur.

Les événements reçus avant cette révision reçoivent leur émetteur (l'espace de
noms de leur job, sous sa forme NFC) et le fait d'être ou non l'événement d'un
appel d'outil.

Révision : 0006
Précédente : 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("event_raw", sa.Column("emitter", sa.Text, nullable=True))
    op.add_column("event_raw", sa.Column("tool_call", sa.Boolean, nullable=True))
    op.execute(
        """
        UPDATE event_raw
        SET emitter = normalize(payload -> 'job' ->> 'namespace', NFC),
            tool_call = coalesce(payload -> 'run' -> 'facets' ? 'answerlineage_toolCall', false)
        """
    )
    op.alter_column("event_raw", "emitter", nullable=False)
    op.alter_column("event_raw", "tool_call", nullable=False)
    op.create_index(
        "ix_event_raw_emitter_event_time",
        "event_raw",
        ["emitter", "event_time"],
        postgresql_where=sa.text("tool_call"),
    )

    op.create_table(
        "coverage_report",
        sa.Column("emitter", sa.Text, nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls_seen", sa.BigInteger, nullable=False),
        sa.Column("events_emitted", sa.BigInteger, nullable=False),
        sa.Column("events_lost", sa.BigInteger, nullable=False),
        sa.Column("uninspected", sa.BigInteger, nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("emitter", "window_start", "window_end", name="pk_coverage_report"),
        sa.CheckConstraint("window_start < window_end", name="ck_coverage_report_window"),
        sa.CheckConstraint(
            "calls_seen >= 0 AND events_emitted >= 0 AND events_lost >= 0 AND uninspected >= 0",
            name="ck_coverage_report_counts",
        ),
    )
    op.create_table(
        "coverage_emitter",
        sa.Column("emitter", sa.Text, nullable=False),
        sa.Column("last_report_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("silent_since", sa.DateTime(timezone=True)),
        sa.Column("signalled_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("emitter", name="pk_coverage_emitter"),
    )


def downgrade() -> None:
    op.drop_table("coverage_emitter")
    op.drop_table("coverage_report")
    op.drop_index("ix_event_raw_emitter_event_time", table_name="event_raw")
    op.drop_column("event_raw", "tool_call")
    op.drop_column("event_raw", "emitter")
