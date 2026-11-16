import { AnswerView } from "./AnswerView";
import { ImpactView } from "./ImpactView";
import { Overview } from "./Overview";
import { useRoute } from "./route";
import { TraceView } from "./TraceView";

export function App() {
  const route = useRoute();
  const mode = route.view === "trace" ? "trace" : route.view === "impact" ? "impact" : "overview";
  const current = (shown: typeof mode) => (mode === shown ? "page" : undefined);
  return (
    <main className="explorer">
      <header className="bar">
        <h1>AnswerLineage</h1>
        <p className="tagline">De la donnée lue à la personne qui a reçu la réponse</p>
        <nav aria-label="Vues de l'explorateur">
          <a href="#/" aria-current={current("overview")}>
            Vue d'ensemble
          </a>
          <a href="#/remonter" aria-current={current("trace")}>
            Remonter
          </a>
          <a href="#/propager" aria-current={current("impact")}>
            Propager
          </a>
        </nav>
      </header>
      {route.view === "overview" && <Overview dataset={route.dataset} />}
      {route.view === "answer" && <AnswerView key={route.id} id={route.id} />}
      {route.view === "trace" && <TraceView id={route.id} malformed={route.malformed === true} />}
      {route.view === "impact" && (
        <ImpactView dataset={route.dataset} from={route.from} to={route.to} />
      )}
    </main>
  );
}
