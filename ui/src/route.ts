// L'adresse de la page dit ce que l'explorateur montre : elle se partage telle quelle.
//
//   #/                                              les réponses les plus récentes
//   #/donnees?namespace=…&name=…                    les mêmes, un jeu de données choisi
//   #/reponse/<identifiant>                         une réponse et sa chaîne
//   #/remonter                                      les réponses récentes, à remonter
//   #/remonter/<identifiant>                        le chemin d'une réponse jusqu'aux données
//   #/propager?namespace=…&name=…&from=…&to=…       un incident et ce qu'il touche

import { useEffect, useState } from "react";

export interface DatasetKey {
  namespace: string;
  name: string;
}

export type Route =
  | { view: "overview"; dataset: DatasetKey | null }
  | { view: "answer"; id: string }
  // ``malformed`` : l'adresse nomme une réponse sous une forme qui n'en est pas une.
  | { view: "trace"; id: string | null; malformed?: boolean }
  | { view: "impact"; dataset: DatasetKey | null; from: string; to: string };

const ANSWER_ID = /^[0-9a-f]{48}$/;
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$/;

function datasetOf(query: URLSearchParams): DatasetKey | null {
  const namespace = query.get("namespace");
  const name = query.get("name");
  return namespace && name ? { namespace, name } : null;
}

function instantOf(query: URLSearchParams, key: string): string {
  const value = query.get(key) ?? "";
  return INSTANT.test(value) ? value : "";
}

export function parseRoute(hash: string): Route {
  const [path = "", search = ""] = hash.replace(/^#/, "").split("?", 2);
  const query = new URLSearchParams(search);
  const segments = path.split("/").filter((segment) => segment !== "");
  if (segments[0] === "reponse" && segments[1] !== undefined && ANSWER_ID.test(segments[1])) {
    return { view: "answer", id: segments[1] };
  }
  if (segments[0] === "remonter") {
    const named = segments[1];
    if (named === undefined) {
      return { view: "trace", id: null };
    }
    // Une valeur qui n'est pas un identifiant n'est ni gardée ni envoyée à l'API.
    return ANSWER_ID.test(named)
      ? { view: "trace", id: named }
      : { view: "trace", id: null, malformed: true };
  }
  if (segments[0] === "propager") {
    return {
      view: "impact",
      dataset: datasetOf(query),
      from: instantOf(query, "from"),
      to: instantOf(query, "to"),
    };
  }
  return { view: "overview", dataset: segments[0] === "donnees" ? datasetOf(query) : null };
}

function datasetQuery(dataset: DatasetKey): URLSearchParams {
  return new URLSearchParams({ namespace: dataset.namespace, name: dataset.name });
}

export function formatRoute(route: Route): string {
  if (route.view === "answer") {
    return `#/reponse/${route.id}`;
  }
  if (route.view === "trace") {
    return route.id === null ? "#/remonter" : `#/remonter/${route.id}`;
  }
  if (route.view === "impact") {
    const query = route.dataset === null ? new URLSearchParams() : datasetQuery(route.dataset);
    if (route.from !== "") {
      query.set("from", route.from);
    }
    if (route.to !== "") {
      query.set("to", route.to);
    }
    const search = query.toString();
    return search === "" ? "#/propager" : `#/propager?${search}`;
  }
  return route.dataset === null ? "#/" : `#/donnees?${datasetQuery(route.dataset).toString()}`;
}

export function navigate(route: Route): void {
  const target = formatRoute(route);
  if (window.location.hash !== target) {
    window.location.hash = target;
  }
}

export function useRoute(): Route {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const update = () => setHash(window.location.hash);
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return parseRoute(hash);
}
