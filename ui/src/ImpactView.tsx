import { useState } from "react";
import type { FormEvent } from "react";

import { MAX_LIMIT, datasetsPath, graphPath, impactPath } from "./api";
import type { DatasetList, DatasetName, GraphView, ImpactResult } from "./api";
import { Graph } from "./Graph";
import { Failure, Loading } from "./Notice";
import { Partial } from "./Overview";
import { describeChain } from "./nodes";
import { navigate } from "./route";
import type { DatasetKey } from "./route";
import { useFetch } from "./useFetch";

const HOUR = 3_600_000;

// « 2026-11-11T06:00:00Z » ↔ « 2026-11-11T06:00 », la valeur d'un champ de date et d'heure.
function toField(instant: string): string {
  return instant.slice(0, 16);
}

function toInstant(field: string): string {
  return `${field}:00Z`;
}

// À défaut de fenêtre dans l'adresse : les vingt-quatre heures qui précèdent la dernière réponse.
function defaultWindow(latest: GraphView | null): { from: string; to: string } {
  const last = latest?.answers[0];
  if (last === undefined) {
    return { from: "", to: "" };
  }
  const end = Math.ceil(new Date(last.produced_at).getTime() / HOUR) * HOUR;
  return {
    from: new Date(end - 24 * HOUR).toISOString().slice(0, 16),
    to: new Date(end).toISOString().slice(0, 16),
  };
}

interface FormProps {
  datasets: DatasetName[];
  dataset: DatasetKey | null;
  from: string;
  to: string;
}

function ImpactForm({ datasets, dataset, from, to }: FormProps) {
  const known = datasets.findIndex(
    (item) => item.namespace === dataset?.namespace && item.name === dataset?.name,
  );
  const [index, setIndex] = useState(known);
  const [start, setStart] = useState(from);
  const [end, setEnd] = useState(to);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const chosen = datasets[index];
    if (chosen === undefined || start === "" || end === "") {
      return;
    }
    navigate({
      view: "impact",
      dataset: { namespace: chosen.namespace, name: chosen.name },
      from: toInstant(start),
      to: toInstant(end),
    });
  };

  return (
    <form className="incident" onSubmit={submit} aria-label="Incident à propager">
      <label>
        <span>Jeu de données faux</span>
        <select value={index} onChange={(event) => setIndex(Number(event.target.value))} required>
          <option value={-1} disabled>
            Choisir…
          </option>
          {datasets.map((item, rank) => (
            <option key={`${item.namespace}/${item.name}`} value={rank}>
              {item.name} ({item.namespace})
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Faux depuis (UTC)</span>
        <input
          type="datetime-local"
          value={start}
          onChange={(event) => setStart(event.target.value)}
          required
        />
      </label>
      <label>
        <span>Corrigé à (UTC)</span>
        <input
          type="datetime-local"
          value={end}
          onChange={(event) => setEnd(event.target.value)}
          required
        />
      </label>
      <button type="submit" className="action action-main">
        Propager
      </button>
    </form>
  );
}

function Result({ result }: { result: ImpactResult }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const view = result.graph;
  const source =
    view.datasets.find(
      (node) => node.namespace === result.dataset.namespace && node.name === result.dataset.name,
    )?.id ?? null;
  const described = focus ?? selected ?? source;

  return (
    <>
      <dl className="counters" data-testid="counters">
        <div>
          <dt>Réponses touchées</dt>
          <dd data-testid="count-answers">{result.counts.answers}</dd>
        </div>
        <div>
          <dt>Destinataires</dt>
          <dd data-testid="count-recipients">{result.counts.recipients}</dd>
        </div>
        <div>
          <dt>Couverture du lignage</dt>
          <dd data-testid="coverage">non mesurée</dd>
        </div>
      </dl>
      <p className="hint">
        La couverture du lignage sur cette fenêtre n'est pas encore mesurée : une réponse dont la
        lecture n'a pas été captée n'apparaît pas ici.
      </p>
      {result.truncated && (
        <p className="notice" role="status">
          Résultat partiel : l'incident touche plus de runs que la propagation n'en parcourt.
        </p>
      )}
      {view.answers.length === 0 ? (
        <p className="notice" role="status">
          Aucune réponse ne s'appuie sur une lecture de ce jeu de données pendant cette fenêtre.
        </p>
      ) : (
        <>
          <div className="status">
            <p className="summary" role="status" data-testid="summary">
              {described === null ? null : describeChain(view, described)}
            </p>
          </div>
          {view.more && (
            <p className="notice" role="status">
              Les {MAX_LIMIT} réponses touchées les plus récentes sont affichées, sur{" "}
              {result.counts.answers}.
            </p>
          )}
          {view.truncated && <Partial />}
          <Graph
            view={view}
            selected={selected}
            onSelect={setSelected}
            anchor={source}
            onFocus={setFocus}
          />
        </>
      )}
    </>
  );
}

interface ImpactViewProps {
  dataset: DatasetKey | null;
  from: string;
  to: string;
}

// Un jeu de données, une fenêtre : les réponses et les destinataires touchés s'allument.
export function ImpactView({ dataset, from, to }: ImpactViewProps) {
  const datasets = useFetch<DatasetList>(datasetsPath());
  const needsDefault = from === "" || to === "";
  const latest = useFetch<GraphView>(needsDefault ? graphPath(1) : null);
  const complete = dataset !== null && !needsDefault;
  const result = useFetch<ImpactResult>(
    complete ? impactPath(dataset.namespace, dataset.name, from, to) : null,
  );

  if (datasets.kind === "error") {
    return <Failure status={datasets.status} />;
  }
  if (datasets.kind !== "ready" || latest.kind === "loading") {
    return <Loading />;
  }
  const proposed = defaultWindow(latest.kind === "ready" ? latest.data : null);
  const start = from === "" ? proposed.from : toField(from);
  const end = to === "" ? proposed.to : toField(to);

  return (
    <>
      <ImpactForm
        key={`${dataset?.namespace}/${dataset?.name}/${start}/${end}`}
        datasets={datasets.data.datasets}
        dataset={dataset}
        from={start}
        to={end}
      />
      {result.kind === "loading" && <Loading />}
      {result.kind === "error" && (
        <Failure status={result.status} missing="Ce jeu de données n'est pas connu du graphe." />
      )}
      {result.kind === "ready" && <Result key={`${from}/${to}`} result={result.data} />}
      {result.kind === "idle" && (
        <p className="notice" role="status">
          Choisissez le jeu de données reconnu faux et la période pendant laquelle il l'a été.
        </p>
      )}
    </>
  );
}
