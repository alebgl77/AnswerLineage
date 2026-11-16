"""Barrière du graphe, entre sa reconstruction et les workers.

La reconstruction (``rebuild.py``) vide le graphe puis le projette seule, de la
file. Seule, parce que deux projections concurrentes sur un graphe vide
s'interbloquent : elles créent ensemble les mêmes jeux de données, outils et
destinataires, dans des ordres différents, et chaque interblocage remet un
événement à plus tard, compte un essai, et ralentit la reprise au lieu de
l'accélérer. Seule aussi pour vider le graphe sans attendre ni bloquer un lot
en cours.

La barrière est un verrou consultatif de PostgreSQL. La reconstruction le tient
en exclusif, pour sa session, tant qu'elle tourne. Un worker le prend en
partage, sans l'attendre, au début de chaque lot et de chaque analyse
d'incident, pour la transaction : s'il le trouve tenu, il passe son tour, et sa
boucle continue (émetteurs muets, partitions). La reconstruction, qui le prend
en attendant, laisse finir les lots en cours avant de vider le graphe.

La barrière tombe avec la session qui la tient : une reconstruction arrêtée,
même brutalement, rend la main aux workers, qui projettent ce qu'elle a laissé
dans la file. Une analyse attend pourtant que la file ait rendu au graphe tout
ce qu'une reconstruction inachevée y a mis (``REBUILD_PENDING``) : sur un graphe
partiel, elle manquerait des réponses touchées.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Clé du verrou consultatif de la barrière, propre à AnswerLineage (« ALGRAPHE »).
GATE_KEY: Final = 0x414C_4752_4150_4845

_SHARE = text("SELECT pg_try_advisory_xact_lock_shared(:key)")

# Vrai tant qu'une reconstruction inachevée a laissé dans la file des événements
# à projeter : les siens, ou ceux qui y attendaient quand elle a commencé.
REBUILD_PENDING: Final = """EXISTS (
    SELECT 1 FROM graph_rebuild r
    WHERE r.finished_at IS NULL
      AND EXISTS (SELECT 1 FROM ingest_queue q WHERE q.id <= r.queue_boundary)
)"""


async def share(conn: AsyncConnection) -> bool:
    """Prend la barrière en partage pour la transaction en cours, sans l'attendre.

    Rend faux quand une reconstruction la tient : le graphe n'est pas à écrire
    ni à lire pour une analyse. La session qui tient la barrière la prend aussi.
    """
    shared: bool = (await conn.execute(_SHARE, {"key": GATE_KEY})).scalar_one()
    return shared
