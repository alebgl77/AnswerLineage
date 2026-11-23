"""Zone des quarantaines : les jeux dérivés de leur périmètre que le graphe connaît.

``incident_quarantine_zone`` tient une ligne par quarantaine dont les workers ont
calculé la zone : l'heure du graphe lu qui l'a donnée, celle du dernier calcul, qui
la suit (``ck_incident_quarantine_zone_checked_after``), le nombre de ses jeux
dérivés, de 0 à 50 (``ck_incident_quarantine_zone_derived``), et si une borne en a
écarté d'autres. ``incident_quarantine_derived`` tient ces jeux, par espace de noms
et nom, avec leur profondeur, de 1 à 3 (``ck_incident_quarantine_derived_depth``).

Deux tables à part, rattachées à la quarantaine : ses transitions refusent toute
écriture d'une quarantaine active, qui ne peut donc rien porter de plus.

La descente retire les deux tables : les zones sont perdues, les quarantaines
inchangées ; les workers les recalculent à la montée suivante.

Révision : 0020
Précédente : 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None

_MAX_DEPTH = 3
_MAX_DERIVED = 50


def upgrade() -> None:
    op.create_table(
        "incident_quarantine_zone",
        sa.Column(
            "quarantine_id",
            sa.BigInteger,
            sa.ForeignKey(
                "incident_quarantine.id",
                name="fk_incident_quarantine_zone_quarantine_id_incident_quarantine",
            ),
            nullable=False,
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("truncated", sa.Boolean, nullable=False),
        sa.Column("derived", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("quarantine_id", name="pk_incident_quarantine_zone"),
        sa.CheckConstraint(
            "computed_at <= checked_at", name="ck_incident_quarantine_zone_checked_after"
        ),
        sa.CheckConstraint(
            f"derived BETWEEN 0 AND {_MAX_DERIVED}", name="ck_incident_quarantine_zone_derived"
        ),
    )
    op.create_table(
        "incident_quarantine_derived",
        sa.Column(
            "quarantine_id",
            sa.BigInteger,
            sa.ForeignKey(
                "incident_quarantine_zone.quarantine_id",
                name="fk_incident_quarantine_derived_zone",
            ),
            nullable=False,
        ),
        sa.Column("namespace", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("depth", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint(
            "quarantine_id", "namespace", "name", name="pk_incident_quarantine_derived"
        ),
        sa.CheckConstraint(
            f"depth BETWEEN 1 AND {_MAX_DEPTH}", name="ck_incident_quarantine_derived_depth"
        ),
    )


def downgrade() -> None:
    op.drop_table("incident_quarantine_derived")
    op.drop_table("incident_quarantine_zone")
