// Du graphe reçu aux nœuds affichés : libellés courts, détail complet, décompte d'une chaîne.

import type { GraphView } from "./api";
import { chainOf, upstreamOf } from "./graph";
import { lastSegment, plural, shortDay, shortFingerprint, shortHour, shortTime } from "./labels";

export type Kind = "dataset" | "tool" | "agent" | "answer" | "recipient";

export interface NodeView {
  id: string;
  // Début du libellé, omis sur un écran étroit.
  prefix?: string;
  label: string;
  // Nom complet, lu par les lecteurs d'écran et montré au survol.
  detail: string;
}

export interface Column {
  kind: Kind;
  title: string;
  nodes: NodeView[];
}

export function columnsOf(view: GraphView): Column[] {
  return [
    {
      kind: "dataset",
      title: "Données",
      nodes: view.datasets.map((dataset) => ({
        id: dataset.id,
        label: lastSegment(dataset.name),
        detail: `${dataset.name} (${dataset.namespace})`,
      })),
    },
    {
      kind: "tool",
      title: "Outils",
      nodes: view.tools.map((tool) => ({
        id: tool.id,
        label: tool.name,
        detail: `${tool.name}, serveur ${tool.server}`,
      })),
    },
    {
      kind: "agent",
      title: "Agents",
      nodes: view.agents.map((agent) => ({
        id: agent.id,
        label: agent.name,
        detail: `${agent.name} (${agent.namespace})`,
      })),
    },
    {
      kind: "answer",
      title: "Réponses",
      nodes: view.answers.map((answer) => ({
        id: answer.id,
        prefix: shortDay(answer.produced_at),
        label: shortHour(answer.produced_at),
        detail: `Réponse ${shortFingerprint(answer.fingerprint)} du ${shortTime(answer.produced_at)} UTC`,
      })),
    },
    {
      kind: "recipient",
      title: "Destinataires",
      nodes: view.recipients.map((recipient) => ({
        id: recipient.id,
        label: shortFingerprint(recipient.pseudonym),
        detail: `Destinataire ${recipient.pseudonym}`,
      })),
    },
  ];
}

// « taux_change (bigquery) : 12 réponses, 6 destinataires. »
export function describeChain(view: GraphView, id: string): string | null {
  const node = columnsOf(view)
    .flatMap((column) => column.nodes)
    .find((candidate) => candidate.id === id);
  if (node === undefined) {
    return null;
  }
  const chain = chainOf(view, id);
  const recipients = plural(
    view.recipients.filter((recipient) => chain.nodes.has(recipient.id)).length,
    "destinataire",
    "destinataires",
  );
  if (view.answers.some((answer) => answer.id === id)) {
    // Une réponse se décrit par ce qu'elle a lu, pas par le nombre de réponses.
    const datasets = view.datasets.filter((dataset) => chain.nodes.has(dataset.id)).length;
    const tools = view.tools.filter((tool) => chain.nodes.has(tool.id)).length;
    return `${node.detail} : ${plural(datasets, "jeu de données", "jeux de données")}, ${plural(
      tools,
      "outil",
      "outils",
    )}, ${recipients}.`;
  }
  return `${node.detail} : ${plural(chain.answers.size, "réponse", "réponses")}, ${recipients}.`;
}

// Remonter : « Réponse 1a2b3c4d du 13/11 17:52 UTC : 1 agent, 2 outils, 3 jeux de données. »
export function describePath(view: GraphView, id: string): string | null {
  const node = columnsOf(view)
    .flatMap((column) => column.nodes)
    .find((candidate) => candidate.id === id);
  if (node === undefined) {
    return null;
  }
  const chain = upstreamOf(view, id);
  const reached = (ids: { id: string }[]) =>
    ids.filter((item) => item.id !== id && chain.nodes.has(item.id)).length;
  const parts = [
    [reached(view.agents), "agent", "agents"],
    [reached(view.tools), "outil", "outils"],
    [reached(view.datasets), "jeu de données", "jeux de données"],
  ] as const;
  const found = parts
    .filter(([count]) => count > 0)
    .map(([count, one, many]) => plural(count, one, many));
  return found.length === 0
    ? `${node.detail} : rien en amont dans le graphe.`
    : `${node.detail} : ${found.join(", ")}.`;
}

export function describeView(view: GraphView): string {
  return `${plural(view.answers.length, "réponse", "réponses")}, ${plural(
    view.datasets.length,
    "jeu de données",
    "jeux de données",
  )}, ${plural(view.tools.length, "outil", "outils")}, ${plural(
    view.agents.length,
    "agent",
    "agents",
  )}, ${plural(view.recipients.length, "destinataire", "destinataires")}.`;
}
