// Du graphe reçu aux liens tracés et à la chaîne mise en évidence.
//
// Chaque réponse porte sa propre chaîne : les jeux de données lus, l'outil qui
// les a lus, l'agent, les destinataires. Les liens entre colonnes en sont la
// réunion ; la mise en évidence suit la chaîne des réponses concernées, ou, pour
// remonter, la seule part de cette chaîne dont le nœud dépend.

import type { AnswerNode, GraphView } from "./api";

export interface Link {
  source: string;
  target: string;
  // Réponses dont la chaîne passe par ce lien.
  answers: Set<string>;
}

interface Step {
  source: string;
  target: string;
}

// Les maillons de la chaîne d'une réponse, de la donnée au destinataire.
function steps(answer: AnswerNode): Step[] {
  const reached = answer.agent ?? answer.id;
  const found: Step[] = [];
  for (const read of answer.reads) {
    found.push({ source: read.dataset, target: read.tool ?? reached });
  }
  for (const tool of answer.tools) {
    found.push({ source: tool, target: reached });
  }
  if (answer.agent !== null) {
    found.push({ source: answer.agent, target: answer.id });
  }
  for (const recipient of answer.recipients) {
    found.push({ source: answer.id, target: recipient });
  }
  return found;
}

export function linksOf(view: GraphView): Link[] {
  const links = new Map<string, Link>();
  for (const answer of view.answers) {
    for (const step of steps(answer)) {
      const key = `${step.source}>${step.target}`;
      let link = links.get(key);
      if (link === undefined) {
        link = { source: step.source, target: step.target, answers: new Set() };
        links.set(key, link);
      }
      link.answers.add(answer.id);
    }
  }
  return [...links.values()];
}

export interface Chain {
  nodes: Set<string>;
  // Réponses dont la chaîne passe par le nœud choisi.
  answers: Set<string>;
}

// Ce qu'un nœud touche : en amont ce dont il dépend, en aval ce qui dépend de lui.
export function chainOf(view: GraphView, selected: string): Chain {
  const nodes = new Set<string>([selected]);
  const answers = new Set<string>();
  for (const answer of view.answers) {
    const reads = answer.reads.filter((read) => read.dataset === selected);
    const isDataset = reads.length > 0;
    const isTool = answer.tools.includes(selected);
    const isDownstream =
      answer.id === selected || answer.agent === selected || answer.recipients.includes(selected);
    if (!isDataset && !isTool && !isDownstream) {
      continue;
    }
    answers.add(answer.id);
    nodes.add(answer.id);
    if (answer.agent !== null) {
      nodes.add(answer.agent);
    }
    for (const recipient of answer.recipients) {
      nodes.add(recipient);
    }
    if (isDataset) {
      // Depuis une donnée : seuls les outils qui l'ont lue.
      for (const read of reads) {
        if (read.tool !== null) {
          nodes.add(read.tool);
        }
      }
    } else if (isTool) {
      // Depuis un outil : seules les données qu'il a lues.
      for (const read of answer.reads) {
        if (read.tool === selected) {
          nodes.add(read.dataset);
        }
      }
    } else {
      for (const read of answer.reads) {
        nodes.add(read.dataset);
        if (read.tool !== null) {
          nodes.add(read.tool);
        }
      }
      for (const tool of answer.tools) {
        nodes.add(tool);
      }
    }
  }
  return { nodes, answers };
}

// Remonter : ce dont un nœud dépend, de lui jusqu'aux données. Rien de ce qui
// dépend de lui ne s'allume : depuis une réponse, ni ses destinataires ni les
// autres réponses de son agent.
export function upstreamOf(view: GraphView, selected: string): Chain {
  const nodes = new Set<string>([selected]);
  const answers = new Set<string>();
  for (const answer of view.answers) {
    // Un destinataire dépend des réponses reçues, une réponse de son agent, de ses outils et
    // de ses lectures, un agent des outils et des lectures de ses réponses.
    const isAnswer = answer.id === selected || answer.recipients.includes(selected);
    const isAgent = answer.agent === selected;
    const readByTool = answer.reads.filter((read) => read.tool === selected);
    if (isAnswer || isAgent) {
      answers.add(answer.id);
      if (isAnswer) {
        nodes.add(answer.id);
        if (answer.agent !== null) {
          nodes.add(answer.agent);
        }
      }
      for (const read of answer.reads) {
        nodes.add(read.dataset);
        if (read.tool !== null) {
          nodes.add(read.tool);
        }
      }
      for (const tool of answer.tools) {
        nodes.add(tool);
      }
    } else if (readByTool.length > 0) {
      // Depuis un outil : les seules données qu'il a lues. Une donnée ne dépend de rien.
      answers.add(answer.id);
      for (const read of readByTool) {
        nodes.add(read.dataset);
      }
    }
  }
  return { nodes, answers };
}

export function isLinkInChain(link: Link, chain: Chain): boolean {
  if (!chain.nodes.has(link.source) || !chain.nodes.has(link.target)) {
    return false;
  }
  for (const answer of link.answers) {
    if (chain.answers.has(answer)) {
      return true;
    }
  }
  return false;
}
