import { useEffect, useState } from "react";

import { ApiError, fetchJson } from "./api";

export type Fetched<T> =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; data: T }
  // ``status`` vaut 0 quand l'API n'a pas répondu du tout.
  | { kind: "error"; status: number };

// Lit une ressource de l'API ; ``path`` nul ne lit rien.
export function useFetch<T>(path: string | null): Fetched<T> {
  const [state, setState] = useState<Fetched<T>>({ kind: path === null ? "idle" : "loading" });

  useEffect(() => {
    if (path === null) {
      setState({ kind: "idle" });
      return;
    }
    const controller = new AbortController();
    setState({ kind: "loading" });
    fetchJson<T>(path, controller.signal)
      .then((data) => setState({ kind: "ready", data }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setState({ kind: "error", status: error instanceof ApiError ? error.status : 0 });
        }
      });
    return () => controller.abort();
  }, [path]);

  return state;
}
