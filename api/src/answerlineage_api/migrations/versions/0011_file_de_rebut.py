"""File de rebut des événements que la projection n'a pas pu intégrer au graphe.

``dead_letter`` désigne chaque événement mis au rebut par son identifiant et
son mois de réception, comme la file, avec son émetteur, le motif (un code
d'une liste fermée), le nombre d'essais, la première et la dernière heure
d'échec. La file garde désormais l'heure du premier essai en échec
(``first_failed_at``) ; un événement déjà remis à plus tard prend son heure de
mise en file, faute de mieux : la première heure d'échec n'était pas gardée.

La descente remet en file les événements du rebut, avec leurs essais, avant de
retirer la table : aucun n'est perdu.

Révision : 0011
Précédente : 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None

_REASONS = (
    "invalid_identifier",
    "invalid_event",
    "constraint_violation",
    "invalid_value",
    "database_error",
    "projection_error",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _moment(name: str, *, nullable: bool = True) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.add_column("ingest_queue", _moment("first_failed_at"))
    op.execute("UPDATE ingest_queue SET first_failed_at = enqueued_at WHERE attempts > 0")
    op.create_check_constraint(
        "ck_ingest_queue_first_failure",
        "ingest_queue",
        "(attempts = 0) = (first_failed_at IS NULL)",
    )

    op.create_table(
        "dead_letter",
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("recorded_month", sa.Date, nullable=False),
        sa.Column("emitter", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        _moment("first_failed_at", nullable=False),
        _moment("last_failed_at", nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="pk_dead_letter"),
        sa.CheckConstraint(_in("reason", _REASONS), name="ck_dead_letter_reason"),
        sa.CheckConstraint("attempts >= 1", name="ck_dead_letter_attempts"),
        sa.CheckConstraint("first_failed_at <= last_failed_at", name="ck_dead_letter_failures"),
    )
    op.create_index(
        "ix_dead_letter_last_failed_at_event_id", "dead_letter", ["last_failed_at", "event_id"]
    )
    op.create_index("ix_dead_letter_emitter_reason", "dead_letter", ["emitter", "reason"])


def downgrade() -> None:
    # Les plus anciens échecs d'abord : la file les reprend dans cet ordre.
    op.execute(
        """
        INSERT INTO ingest_queue (event_id, recorded_month, attempts, last_error, first_failed_at)
        SELECT event_id, recorded_month, attempts, reason, first_failed_at
        FROM dead_letter
        ORDER BY last_failed_at, event_id
        ON CONFLICT (event_id) DO NOTHING
        """
    )
    op.drop_table("dead_letter")
    op.drop_constraint("ck_ingest_queue_first_failure", "ingest_queue", type_="check")
    op.drop_column("ingest_queue", "first_failed_at")
