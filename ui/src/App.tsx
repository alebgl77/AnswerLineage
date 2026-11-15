import { AnswerView } from "./AnswerView";
import { ImpactView } from "./ImpactView";
import { Overview } from "./Overview";
import { useRoute } from "./route";

export function App() {
  const route = useRoute();
  const propagating = route.view === "impact";
  return (
    <main className="explorer">
      <header className="bar">
        <h1>AnswerLineage</h1>
        <p className="tagline">De la donnée lue à la personne qui a reçu la réponse</p>
        <nav aria-label="Vues de l'explorateur">
          <a href="#/" aria-current={propagating ? undefined : "page"}>
            Vue d'ensemble
          </a>
          <a href="#/propager" aria-current={propagating ? "page" : undefined}>
            Propager
          </a>
        </nav>
      </header>
      {route.view === "overview" && <Overview dataset={route.dataset} />}
      {route.view === "answer" && <AnswerView key={route.id} id={route.id} />}
      {route.view === "impact" && (
        <ImpactView dataset={route.dataset} from={route.from} to={route.to} />
      )}
    </main>
  );
}
