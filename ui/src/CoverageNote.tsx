// La couverture du lignage d'un incident : ce que la propagation a pu voir, et ce qu'elle ignore.

import type { Coverage, CoverageHole } from "./api";
import { plural, shortTime } from "./labels";

// « 99 % », « 99,5 % » : arrondi par défaut, pour ne jamais annoncer plus que mesuré.
export function percent(rate: number): string {
  // Le millionième arrondi d'abord : 0,57 × 1000 vaut 569,99… en virgule flottante.
  const tenths = Math.floor(Math.round(rate * 1_000_000) / 1000) / 10;
  return `${String(tenths).replace(".", ",")}\u00a0%`;
}

// Ce que la case de la couverture affiche, à côté des compteurs.
export function coverageValue(coverage: Coverage): string {
  if (coverage.status === "unknown") {
    return "non mesurée";
  }
  if (coverage.status === "partial") {
    return "partielle";
  }
  return coverage.rate === null ? "aucun appel vu" : percent(coverage.rate);
}

function holeLabel(hole: CoverageHole): string {
  const count = hole.count ?? 0;
  switch (hole.kind) {
    case "events_lost":
      return plural(count, "événement perdu", "événements perdus");
    case "missing_from_graph":
      return plural(count, "événement absent du graphe", "événements absents du graphe");
    case "uninspected":
      return `${plural(count, "requête non inspectée", "requêtes non inspectées")}, qui cachent peut-être des appels`;
    case "silence":
      return "aucun relevé (silence)";
    case "before_first_report":
      return "avant son premier relevé";
  }
}

function summary(coverage: Coverage): string {
  const seen = plural(coverage.calls_seen, "appel vu", "appels vus");
  const rate = coverage.rate === null ? "" : `, ${percent(coverage.rate)} présents dans le graphe`;
  if (coverage.status === "unknown") {
    return "Aucun relevé d'émetteur ne couvre cette fenêtre : la couverture du lignage n'est pas mesurée. Une réponse dont l'appel n'a pas été capté n'apparaît pas ici.";
  }
  if (coverage.status === "partial") {
    return `Les relevés des émetteurs ne couvrent qu'une partie de cette fenêtre (${seen}${rate}) : une réponse dont l'appel tombe dans un trou peut manquer.`;
  }
  if (coverage.holes.length > 0) {
    return `Toute la fenêtre est couverte par les relevés des émetteurs (${seen}${rate}), avec des trous connus : une réponse dont l'appel tombe dans l'un d'eux peut manquer.`;
  }
  return `Toute la fenêtre est couverte par les relevés des émetteurs, sans trou connu (${seen}${rate}).`;
}

export function CoverageNote({ coverage }: { coverage: Coverage }) {
  return (
    <div className="coverage" data-testid="coverage-note">
      <p className="hint">{summary(coverage)}</p>
      {coverage.holes.length > 0 && (
        <ul className="holes" data-testid="coverage-holes" aria-label="Trous connus du lignage">
          {coverage.holes.map((hole) => (
            <li
              key={`${hole.emitter}/${hole.kind}/${hole.window.from}`}
              className={`hole hole-${hole.kind}`}
            >
              <span className="hole-emitter">{hole.emitter}</span> : {holeLabel(hole)}, du{" "}
              {shortTime(hole.window.from)} au {shortTime(hole.window.to)}
            </li>
          ))}
        </ul>
      )}
      {coverage.truncated && (
        <p className="notice" role="status">
          La mesure de la couverture dépasse ses bornes : ses trous peuvent être incomplets. Une
          fenêtre plus courte la rend complète.
        </p>
      )}
    </div>
  );
}
