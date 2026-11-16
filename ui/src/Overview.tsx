import { useState } from "react";

import { DEFAULT_LIMIT, MAX_LIMIT, graphPath } from "./api";
import type { GraphView } from "./api";
import { Graph } from "./Graph";
import { Failure, Loading } from "./Notice";
import { describeChain, describeView } from "./nodes";
import { formatRoute, navigate } from "./route";
import type { DatasetKey } from "./route";
import { useFetch } from "./useFetch";

interface OverviewProps {
  // Jeu de données choisi par l'adresse de la page.
  dataset: DatasetKey | null;
}

// Les réponses les plus récentes et tout ce qu'elles touchent.
export function Overview({ dataset }: OverviewProps) {
  const [limit, setLimit] = useState(DEFAULT_LIMIT);
  const [chosen, setChosen] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const fetched = useFetch<GraphView>(graphPath(limit));

  if (fetched.kind === "error") {
    return <Failure status={fetched.status} />;
  }
  if (fetched.kind !== "ready") {
    return <Loading />;
  }
  const view = fetched.data;
  if (view.answers.length === 0) {
    return (
      <p className="notice" role="status">
        Aucune réponse d'agent n'a encore été captée. Le graphe se remplit dès que des événements
        arrivent.
      </p>
    );
  }

  // Le jeu de données de l'adresse l'emporte sur un nœud choisi d'une autre colonne.
  const named =
    dataset === null
      ? undefined
      : view.datasets.find(
          (node) => node.namespace === dataset.namespace && node.name === dataset.name,
        );
  const selected = named?.id ?? chosen;
  const select = (id: string | null) => {
    const picked = view.datasets.find((node) => node.id === id);
    setChosen(picked === undefined ? id : null);
    navigate({
      view: "overview",
      dataset: picked === undefined ? null : { namespace: picked.namespace, name: picked.name },
    });
  };

  const described = focus ?? selected;
  const answer = view.answers.find((node) => node.id === selected);
  const summary =
    (described === null ? null : describeChain(view, described)) ??
    `Les plus récentes : ${describeView(view)}`;

  return (
    <>
      <div className="status">
        <p className="summary" role="status" data-testid="summary">
          {summary}
        </p>
        {named !== undefined && (
          <a
            className="action"
            href={formatRoute({ view: "impact", dataset, from: "", to: "" })}
          >
            Propager un incident sur ce jeu de données
          </a>
        )}
        {answer !== undefined && (
          <a className="action" href={formatRoute({ view: "answer", id: answer.id })}>
            Ouvrir cette réponse
          </a>
        )}
        {view.more && limit < MAX_LIMIT && (
          <button
            type="button"
            className="action"
            onClick={() => setLimit(Math.min(limit * 2, MAX_LIMIT))}
          >
            Afficher plus de réponses
          </button>
        )}
      </div>
      {dataset !== null && named === undefined && (
        <p className="notice" role="status">
          Ce jeu de données n'apparaît pas dans les réponses affichées.{" "}
          <a href={formatRoute({ view: "impact", dataset, from: "", to: "" })}>
            Propager un incident sur ce jeu de données
          </a>
        </p>
      )}
      {view.truncated && <Partial />}
      <Graph view={view} selected={selected} onSelect={select} onFocus={setFocus} />
    </>
  );
}

export function Partial() {
  return (
    <p className="notice" role="status">
      Vue partielle : certaines chaînes dépassent la taille parcourue.
    </p>
  );
}
