"""Instances des relevés : plusieurs proxys devant un même serveur ne se confondent plus.

Un relevé de couverture porte désormais l'instance de son émetteur, et la table
les range par émetteur, instance et fenêtre. Les relevés reçus avant cette
révision n'ont pas d'instance : ils gardent leur place sous l'instance vide,
qui est celle d'un relevé sans instance. La longueur d'une instance est bornée
comme celle d'un identifiant reçu.

La descente range de nouveau les relevés par émetteur et fenêtre : les
instances d'une même fenêtre, reçues le même mois, s'y réunissent en une ligne
dont chaque compteur est leur somme, la mesure de l'émetteur.

Révision : 0009
Précédente : 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

_INSTANCE_MAX = 255
_COUNTERS = ("calls_seen", "events_emitted", "events_lost", "uninspected")
_KEY = ("emitter", "window_start", "window_end", "recorded_month")


def upgrade() -> None:
    op.add_column(
        "coverage_report",
        sa.Column("instance", sa.Text, nullable=False, server_default=sa.text("''")),
    )
    op.create_check_constraint(
        "ck_coverage_report_instance",
        "coverage_report",
        f"char_length(instance) <= {_INSTANCE_MAX}",
    )
    op.drop_constraint("pk_coverage_report", "coverage_report", type_="primary")
    op.create_primary_key(
        "pk_coverage_report",
        "coverage_report",
        ["emitter", "instance", "window_start", "window_end", "recorded_month"],
    )


def downgrade() -> None:
    keys = ", ".join(_KEY)
    sums = ", ".join(f"sum({counter}) AS {counter}" for counter in _COUNTERS)
    op.execute(
        "CREATE TEMPORARY TABLE coverage_report_merged ON COMMIT DROP AS"  # noqa: S608 — noms constants.
        f" SELECT {keys}, {sums}, min(recorded_at) AS recorded_at"
        f" FROM coverage_report GROUP BY {keys}"
    )
    op.execute("DELETE FROM coverage_report")
    op.drop_constraint("pk_coverage_report", "coverage_report", type_="primary")
    op.drop_constraint("ck_coverage_report_instance", "coverage_report", type_="check")
    op.drop_column("coverage_report", "instance")
    columns = ", ".join((*_KEY, *_COUNTERS, "recorded_at"))
    op.execute(
        f"INSERT INTO coverage_report ({columns})"  # noqa: S608 — noms constants.
        f" SELECT {columns} FROM coverage_report_merged"
    )
    op.create_primary_key("pk_coverage_report", "coverage_report", list(_KEY))
