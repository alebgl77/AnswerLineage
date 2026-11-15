"""Schéma initial du graphe (US-0201, EF-GRA-01).

Révision : 0001
Précédente : aucune
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def _id(table: str) -> tuple[sa.Column[int], sa.PrimaryKeyConstraint]:
    return (
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
    )


def _recorded_at() -> sa.Column[object]:
    return sa.Column(
        "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _event_time() -> sa.Column[object]:
    return sa.Column("event_time", sa.DateTime(timezone=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "agent",
        *_id("agent"),
        sa.Column("namespace", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        _recorded_at(),
        sa.UniqueConstraint("namespace", "name", name="uq_agent_namespace_name"),
    )
    op.create_table(
        "tool",
        *_id("tool"),
        sa.Column("server", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        _recorded_at(),
        sa.UniqueConstraint("server", "name", name="uq_tool_server_name"),
    )
    op.create_table(
        "dataset",
        *_id("dataset"),
        sa.Column("namespace", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        _recorded_at(),
        sa.UniqueConstraint("namespace", "name", name="uq_dataset_namespace_name"),
        sa.CheckConstraint(
            "kind IN ('table', 'document', 'api', 'memory', 'answer')", name="ck_dataset_kind"
        ),
    )
    op.create_table(
        "dataset_version",
        *_id("dataset_version"),
        sa.Column(
            "dataset_id",
            sa.BigInteger,
            sa.ForeignKey("dataset.id", name="fk_dataset_version_dataset_id_dataset"),
            nullable=False,
        ),
        sa.Column("version", sa.Text, nullable=False),
        _recorded_at(),
        sa.UniqueConstraint("dataset_id", "version", name="uq_dataset_version_dataset_id_version"),
    )
    op.create_table(
        "run",
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.PrimaryKeyConstraint("run_id", name="pk_run"),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column(
            "agent_id", sa.BigInteger, sa.ForeignKey("agent.id", name="fk_run_agent_id_agent")
        ),
        sa.Column("tool_id", sa.BigInteger, sa.ForeignKey("tool.id", name="fk_run_tool_id_tool")),
        sa.Column("parent_run_id", sa.Uuid),
        sa.Column("trace_id", sa.Text),
        sa.Column("span_id", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("state_time", sa.DateTime(timezone=True), nullable=False),
        _recorded_at(),
        sa.CheckConstraint("kind IN ('agent', 'tool', 'job')", name="ck_run_kind"),
        sa.CheckConstraint(
            "state IN ('START', 'RUNNING', 'COMPLETE', 'ABORT', 'FAIL', 'OTHER')",
            name="ck_run_state",
        ),
    )
    op.create_index("ix_run_parent_run_id", "run", ["parent_run_id"])
    op.create_index("ix_run_trace_id", "run", ["trace_id"])
    op.create_index("ix_run_agent_id", "run", ["agent_id"])
    op.create_index("ix_run_tool_id", "run", ["tool_id"])

    op.create_table(
        "read_edge",
        *_id("read_edge"),
        sa.Column(
            "run_id",
            sa.Uuid,
            sa.ForeignKey("run.run_id", name="fk_read_edge_run_id_run"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            sa.BigInteger,
            sa.ForeignKey("dataset.id", name="fk_read_edge_dataset_id_dataset"),
            nullable=False,
        ),
        sa.Column(
            "dataset_version_id",
            sa.BigInteger,
            sa.ForeignKey(
                "dataset_version.id", name="fk_read_edge_dataset_version_id_dataset_version"
            ),
        ),
        sa.Column("precision", sa.Text, nullable=False),
        sa.Column("columns", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("origin", sa.Text, nullable=False),
        _event_time(),
        _recorded_at(),
        sa.UniqueConstraint(
            "run_id",
            "dataset_id",
            "dataset_version_id",
            name="uq_read_edge_run_id_dataset_id_dataset_version_id",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "precision IN ('table', 'columns', 'subset', 'keys')", name="ck_read_edge_precision"
        ),
        sa.CheckConstraint("origin IN ('analysis', 'reconciliation')", name="ck_read_edge_origin"),
    )
    op.create_index("ix_read_edge_dataset_id_event_time", "read_edge", ["dataset_id", "event_time"])
    op.create_index("ix_read_edge_dataset_version_id", "read_edge", ["dataset_version_id"])

    op.create_table(
        "write_edge",
        *_id("write_edge"),
        sa.Column(
            "run_id",
            sa.Uuid,
            sa.ForeignKey("run.run_id", name="fk_write_edge_run_id_run"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            sa.BigInteger,
            sa.ForeignKey("dataset.id", name="fk_write_edge_dataset_id_dataset"),
            nullable=False,
        ),
        sa.Column(
            "dataset_version_id",
            sa.BigInteger,
            sa.ForeignKey(
                "dataset_version.id", name="fk_write_edge_dataset_version_id_dataset_version"
            ),
        ),
        _event_time(),
        _recorded_at(),
        sa.UniqueConstraint(
            "run_id",
            "dataset_id",
            "dataset_version_id",
            name="uq_write_edge_run_id_dataset_id_dataset_version_id",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_write_edge_dataset_id_event_time", "write_edge", ["dataset_id", "event_time"]
    )
    op.create_index("ix_write_edge_dataset_version_id", "write_edge", ["dataset_version_id"])

    op.create_table(
        "answer",
        *_id("answer"),
        sa.Column(
            "run_id",
            sa.Uuid,
            sa.ForeignKey("run.run_id", name="fk_answer_run_id_run"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.Text, nullable=False),
        _event_time(),
        _recorded_at(),
        sa.UniqueConstraint("run_id", "fingerprint", name="uq_answer_run_id_fingerprint"),
    )
    op.create_table(
        "delivery",
        *_id("delivery"),
        sa.Column(
            "answer_id",
            sa.BigInteger,
            sa.ForeignKey("answer.id", name="fk_delivery_answer_id_answer"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("message_ref", sa.Text, nullable=False),
        _event_time(),
        _recorded_at(),
        sa.UniqueConstraint(
            "answer_id", "channel", "message_ref", name="uq_delivery_answer_id_channel_message_ref"
        ),
    )
    op.create_index("ix_delivery_channel_message_ref", "delivery", ["channel", "message_ref"])

    op.create_table(
        "recipient",
        *_id("recipient"),
        sa.Column("pseudonym", sa.Text, nullable=False),
        _recorded_at(),
        sa.UniqueConstraint("pseudonym", name="uq_recipient_pseudonym"),
    )
    op.create_table(
        "delivery_recipient",
        sa.Column(
            "delivery_id",
            sa.BigInteger,
            sa.ForeignKey("delivery.id", name="fk_delivery_recipient_delivery_id_delivery"),
            nullable=False,
        ),
        sa.Column(
            "recipient_id",
            sa.BigInteger,
            sa.ForeignKey("recipient.id", name="fk_delivery_recipient_recipient_id_recipient"),
            nullable=False,
        ),
        _recorded_at(),
        sa.PrimaryKeyConstraint("delivery_id", "recipient_id", name="pk_delivery_recipient"),
    )
    op.create_index("ix_delivery_recipient_recipient_id", "delivery_recipient", ["recipient_id"])


def downgrade() -> None:
    for table in (
        "delivery_recipient",
        "recipient",
        "delivery",
        "answer",
        "write_edge",
        "read_edge",
        "run",
        "dataset_version",
        "dataset",
        "tool",
        "agent",
    ):
        op.drop_table(table)
