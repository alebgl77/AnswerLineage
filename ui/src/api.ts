// Ce que l'API de lecture renvoie à l'explorateur.

export interface DatasetNode {
  id: string;
  namespace: string;
  name: string;
  kind: string;
}

export interface ToolNode {
  id: string;
  server: string;
  name: string;
}

export interface AgentNode {
  id: string;
  namespace: string;
  name: string;
}

export interface RecipientNode {
  id: string;
  pseudonym: string;
}

export interface AnswerRead {
  dataset: string;
  tool: string | null;
}

export interface AnswerNode {
  id: string;
  fingerprint: string;
  produced_at: string;
  agent: string | null;
  channels: string[];
  recipients: string[];
  tools: string[];
  reads: AnswerRead[];
}

export interface GraphView {
  datasets: DatasetNode[];
  tools: ToolNode[];
  agents: AgentNode[];
  answers: AnswerNode[];
  recipients: RecipientNode[];
  more: boolean;
  // Curseur de la page suivante ; nul quand il n'y a plus de réponse.
  next_cursor: string | null;
  truncated: boolean;
}

export interface DatasetName {
  namespace: string;
  name: string;
  kind: string;
}

export interface DatasetList {
  datasets: DatasetName[];
  more: boolean;
}

export type HoleKind =
  | "events_lost"
  | "uninspected"
  | "missing_from_graph"
  | "silence"
  | "before_first_report";

// Un trou connu du lignage : une fenêtre où des appels ont pu échapper au graphe.
export interface CoverageHole {
  emitter: string;
  kind: HoleKind;
  window: { from: string; to: string };
  // Nul pour un silence, où rien n'est compté.
  count: number | null;
}

// La couverture du lignage sur la fenêtre : la part des appels vus présents dans le graphe.
export interface Coverage {
  status: "measured" | "partial" | "unknown";
  // De 0 à 1 ; nul quand aucun appel n'a été vu.
  rate: number | null;
  calls_seen: number;
  events_in_graph: number;
  events_lost: number;
  uninspected: number;
  holes: CoverageHole[];
  truncated: boolean;
}

export interface ImpactResult {
  dataset: DatasetName;
  window: { from: string; to: string };
  // ``lower_bound`` : le résultat est tronqué, les décomptes sont des minimums.
  counts: { answers: number; recipients: number; lower_bound: boolean };
  coverage: Coverage;
  graph: GraphView;
  truncated: boolean;
}

export const DEFAULT_LIMIT = 60;
export const MAX_LIMIT = 200;

// L'API a répondu, mais par un refus : le statut dit lequel.
export class ApiError extends Error {
  constructor(readonly status: number) {
    super(`L'API a répondu ${status}.`);
  }
}

export async function fetchJson<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(path, { headers: { accept: "application/json" }, signal });
  if (!response.ok) {
    throw new ApiError(response.status);
  }
  return (await response.json()) as T;
}

export function graphPath(limit: number): string {
  return `/api/v1/graph?limit=${limit}`;
}

export function answerPath(id: string): string {
  return `/api/v1/graph?answer=${encodeURIComponent(id)}`;
}

export function datasetsPath(): string {
  return "/api/v1/datasets?limit=500";
}

export function impactPath(namespace: string, name: string, from: string, to: string): string {
  const query = new URLSearchParams({ namespace, name, from, to, limit: String(MAX_LIMIT) });
  return `/api/v1/impact?${query.toString()}`;
}
