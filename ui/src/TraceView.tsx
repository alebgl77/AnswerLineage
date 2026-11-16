import { useState } from "react";

import { DEFAULT_LIMIT, answerPath, graphPath, sourcesPath } from "./api";
import type { GraphView, Sources } from "./api";
import { CoverageNote, coverageValue, traceScope } from "./CoverageNote";
import { Graph } from "./Graph";
import { counted } from "./labels";
import { Failure, Loading } from "./Notice";
import { Partial } from "./Overview";
import { describePath } from "./nodes";
import { formatRoute, navigate } from "./route";
import { useFetch } from "./useFetch";

const UNKNOWN = "Cette réponse n'est pas connue du graphe.";

// Ce que la remontée a trouvé, et ce qu'elle a pu ne pas voir.
function TraceResult({ sources }: { sources: Sources }) {
  const lowerBound = sources.truncated;
  return (
    <>
      <dl className="counters" data-testid="counters">
        <div>
          <dt>Jeux de données</dt>
          <dd data-testid="count-datasets">{counted(sources.datasets.length, lowerBound)}</dd>
        </div>
        <div>
          <dt>Outils</dt>
          <dd data-testid="count-tools">{counted(sources.tools.length, lowerBound)}</dd>
        </div>
        <div>
          <dt>Couverture du lignage</dt>
          <dd data-testid="coverage">{coverageValue(sources.coverage)}</dd>
        </div>
      </dl>
      <CoverageNote coverage={sources.coverage} scope={traceScope(sources.period)} />
      {lowerBound && (
        <p className="notice" role="status" data-testid="partial">
          Sources partielles : les runs de cette réponse dépassent ce que la remontée parcourt.
          D'autres données peuvent avoir été lues ; les décomptes sont des minimums.
        </p>
      )}
    </>
  );
}

interface TraceViewProps {
  // Réponse choisie par l'adresse ; nulle quand aucune ne l'est.
  id: string | null;
  // L'adresse nomme une réponse sous une forme qui n'en est pas une.
  malformed: boolean;
}

// Remonter : une réponse choisie, et son chemin jusqu'aux données. Le reste s'efface.
export function TraceView({ id, malformed }: TraceViewProps) {
  const [chosen, setChosen] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const recent = useFetch<GraphView>(graphPath(DEFAULT_LIMIT));
  const sources = useFetch<Sources>(id === null ? null : sourcesPath(id));
  const latest = recent.kind === "ready" ? recent.data : null;
  const isRecent = latest !== null && latest.answers.some((answer) => answer.id === id);
  // Une réponse absente des plus récentes s'affiche avec sa seule chaîne.
  const own = useFetch<GraphView>(
    latest !== null && id !== null && !isRecent ? answerPath(id) : null,
  );

  if (recent.kind === "error") {
    return <Failure status={recent.status} />;
  }
  if (latest === null) {
    return <Loading />;
  }
  if (latest.answers.length === 0) {
    return (
      <p className="notice" role="status">
        Aucune réponse d'agent n'a encore été captée. Le graphe se remplit dès que des événements
        arrivent.
      </p>
    );
  }

  const alone =
    !isRecent && own.kind === "ready" && own.data.answers.some((answer) => answer.id === id);
  const view = alone && own.kind === "ready" ? own.data : latest;
  const named = id !== null && view.answers.some((answer) => answer.id === id) ? id : null;
  const selected = named ?? chosen;
  // Choisir une réponse la met dans l'adresse ; un autre nœud s'allume sans y entrer.
  const select = (picked: string | null) => {
    const isAnswer = picked !== null && view.answers.some((answer) => answer.id === picked);
    setChosen(isAnswer ? null : picked);
    navigate({ view: "trace", id: isAnswer ? picked : null });
  };
  const described = focus ?? selected;
  const failure = malformed ? 404 : sources.kind === "error" ? sources.status : null;

  // Chaque élément garde sa place, présent ou non : le graphe n'est jamais remonté, et le
  // nœud choisi au clavier garde le focus.
  return (
    <>
      {failure !== null && <Failure status={failure} missing={UNKNOWN} />}
      {sources.kind === "loading" && <Loading />}
      {sources.kind === "ready" && <TraceResult sources={sources.data} />}
      {alone && (
        <p className="notice" role="status">
          Cette réponse n'est pas parmi les plus récentes : seule sa chaîne est affichée.
        </p>
      )}
      <div className="status">
        <p className="summary" role="status" data-testid="summary">
          {described === null
            ? "Choisissez une réponse : son chemin jusqu'aux données s'allume, le reste s'efface."
            : describePath(view, described)}
        </p>
        {named !== null && (
          <a className="action" href={formatRoute({ view: "answer", id: named })}>
            Ouvrir cette réponse
          </a>
        )}
      </div>
      {view.truncated && <Partial />}
      <Graph view={view} selected={selected} onSelect={select} onFocus={setFocus} upstream />
    </>
  );
}
