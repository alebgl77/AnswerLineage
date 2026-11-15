import { useState } from "react";

import { answerPath } from "./api";
import type { GraphView } from "./api";
import { Graph } from "./Graph";
import { Failure, Loading } from "./Notice";
import { Partial } from "./Overview";
import { describeChain } from "./nodes";
import { formatRoute } from "./route";
import { useFetch } from "./useFetch";

// Une réponse et sa chaîne : ce qu'elle a lu, par quels outils, pour qui.
export function AnswerView({ id }: { id: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const fetched = useFetch<GraphView>(answerPath(id));

  if (fetched.kind === "error") {
    return <Failure status={fetched.status} missing="Cette réponse n'est pas connue du graphe." />;
  }
  if (fetched.kind !== "ready") {
    return <Loading />;
  }
  const view = fetched.data;
  return (
    <>
      <div className="status">
        <p className="summary" role="status" data-testid="summary">
          {describeChain(view, focus ?? selected ?? id)}
        </p>
        <a className="action" href={formatRoute({ view: "overview", dataset: null })}>
          Toutes les réponses récentes
        </a>
      </div>
      {view.truncated && <Partial />}
      <Graph view={view} selected={selected} onSelect={setSelected} anchor={id} onFocus={setFocus} />
    </>
  );
}
