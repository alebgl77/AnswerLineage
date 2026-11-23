"""Quarantaine des incidents : activée, puis levée, et annoncée par webhook.

``incident_quarantine`` tient une ligne par activation, rattachée à l'incident :
son mode, d'une liste fermée, son état, actif puis levé, et les heures de
l'activation et de la levée, posées par la base. Au plus une quarantaine active
par incident (``uq_incident_quarantine_active``). Les transitions forment elles
aussi une liste fermée, tenue par une contrainte nommée
(``ck_incident_quarantine_transition``) : une ligne naît active, seule une
quarantaine active se lève, sans changer d'incident, de mode ni d'heure
d'activation, et une levée est définitive.

La file des webhooks reçoit les deux nouveaux événements, l'activation et la
levée, et chaque message garde la quarantaine de l'incident à l'heure du
changement : son état et son mode, ou rien.

La descente retire des messages en attente ceux des quarantaines, que la
version précédente ne connaît pas, puis les colonnes et la table : les
quarantaines sont perdues, les incidents ne changent pas.

Révision : 0015
Précédente : 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None

_MODES = ("warn",)
_STATES = ("active", "lifted")
# Chaque événement de l'analyse et l'état de l'analyse qu'il annonce.
_ANALYSIS_EVENTS = (
    ("incident.declared", "pending"),
    ("incident.analysed", "done"),
    ("incident.analysis_failed", "failed"),
)
# Chaque événement de la quarantaine et l'état de la quarantaine qu'il annonce.
_QUARANTINE_EVENTS = (
    ("incident.quarantined", "active"),
    ("incident.quarantine_lifted", "lifted"),
)

_TRANSITION_FUNCTION = """
CREATE FUNCTION incident_quarantine_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'INSERT' AND NEW.state = 'active')
        OR (TG_OP = 'UPDATE' AND OLD.state = 'active' AND NEW.state = 'lifted' AND NEW.incident_id = OLD.incident_id AND NEW.mode = OLD.mode AND NEW.activated_at = OLD.activated_at) THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'transition de quarantaine refusée'
        USING ERRCODE = 'check_violation',
              CONSTRAINT = 'ck_incident_quarantine_transition',
              TABLE = 'incident_quarantine';
END
$$
"""  # noqa: E501 — le corps de la fonction, tel que le schéma déclaré l'écrit.
_TRANSITION_CONSTRAINT = """
CREATE CONSTRAINT TRIGGER ck_incident_quarantine_transition
AFTER INSERT OR UPDATE ON incident_quarantine
FOR EACH ROW EXECUTE FUNCTION incident_quarantine_transition()
"""


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _events(events: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    return tuple(event for event, _ in events)


def _analysis_state(*, with_quarantine: bool) -> str:
    admitted = [
        f"(event_type = '{event}' AND analysis_state = '{state}')"
        for event, state in _ANALYSIS_EVENTS
    ]
    if with_quarantine:
        admitted.append(_in("event_type", _events(_QUARANTINE_EVENTS)))
    return " OR ".join(admitted)


def _event_types(*, with_quarantine: bool) -> str:
    events = _events(_ANALYSIS_EVENTS)
    if with_quarantine:
        events += _events(_QUARANTINE_EVENTS)
    return _in("event_type", events)


def upgrade() -> None:
    op.create_table(
        "incident_quarantine",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column(
            "incident_id",
            sa.Uuid,
            sa.ForeignKey("incident.id", name="fk_incident_quarantine_incident_id_incident"),
            nullable=False,
        ),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("lifted_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name="pk_incident_quarantine"),
        sa.CheckConstraint(_in("mode", _MODES), name="ck_incident_quarantine_mode"),
        sa.CheckConstraint(_in("state", _STATES), name="ck_incident_quarantine_state"),
        sa.CheckConstraint(
            "(state = 'lifted') = (lifted_at IS NOT NULL)", name="ck_incident_quarantine_lifted"
        ),
        sa.CheckConstraint(
            "lifted_at IS NULL OR activated_at <= lifted_at",
            name="ck_incident_quarantine_lifted_after",
        ),
    )
    op.create_index(
        "ix_incident_quarantine_incident_id_id", "incident_quarantine", ["incident_id", "id"]
    )
    op.create_index(
        "uq_incident_quarantine_active",
        "incident_quarantine",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )
    op.execute(_TRANSITION_FUNCTION)
    op.execute(_TRANSITION_CONSTRAINT)

    op.add_column("webhook_queue", sa.Column("quarantine_state", sa.Text))
    op.add_column("webhook_queue", sa.Column("quarantine_mode", sa.Text))
    for name in ("ck_webhook_queue_event_type", "ck_webhook_queue_analysis_state"):
        op.drop_constraint(name, "webhook_queue", type_="check")
    op.create_check_constraint(
        "ck_webhook_queue_event_type", "webhook_queue", _event_types(with_quarantine=True)
    )
    op.create_check_constraint(
        "ck_webhook_queue_analysis_state",
        "webhook_queue",
        _analysis_state(with_quarantine=True),
    )
    op.create_check_constraint(
        "ck_webhook_queue_quarantine_event",
        "webhook_queue",
        " AND ".join(
            f"(event_type <> '{event}' OR quarantine_state = '{state}')"
            for event, state in _QUARANTINE_EVENTS
        ),
    )
    op.create_check_constraint(
        "ck_webhook_queue_quarantine_state",
        "webhook_queue",
        f"quarantine_state IS NULL OR {_in('quarantine_state', _STATES)}",
    )
    op.create_check_constraint(
        "ck_webhook_queue_quarantine_mode",
        "webhook_queue",
        f"quarantine_mode IS NULL OR {_in('quarantine_mode', _MODES)}",
    )
    op.create_check_constraint(
        "ck_webhook_queue_quarantine_given",
        "webhook_queue",
        "(quarantine_state IS NULL) = (quarantine_mode IS NULL)",
    )


def downgrade() -> None:
    # Les annonces des quarantaines ne partiront pas : la version précédente ne les connaît pas.
    op.execute(
        "DELETE FROM webhook_queue"
        " WHERE event_type IN ('incident.quarantined', 'incident.quarantine_lifted')"
    )
    for name in (
        "ck_webhook_queue_quarantine_given",
        "ck_webhook_queue_quarantine_mode",
        "ck_webhook_queue_quarantine_state",
        "ck_webhook_queue_quarantine_event",
        "ck_webhook_queue_analysis_state",
        "ck_webhook_queue_event_type",
    ):
        op.drop_constraint(name, "webhook_queue", type_="check")
    op.create_check_constraint(
        "ck_webhook_queue_event_type", "webhook_queue", _event_types(with_quarantine=False)
    )
    op.create_check_constraint(
        "ck_webhook_queue_analysis_state",
        "webhook_queue",
        _analysis_state(with_quarantine=False),
    )
    op.drop_column("webhook_queue", "quarantine_mode")
    op.drop_column("webhook_queue", "quarantine_state")

    op.drop_table("incident_quarantine")
    op.execute("DROP FUNCTION incident_quarantine_transition()")
