import { useLayoutEffect, useMemo, useRef, useState } from "react";

import type { GraphView } from "./api";
import { chainOf, isLinkInChain, linksOf, upstreamOf } from "./graph";
import { columnsOf } from "./nodes";

interface Box {
  left: number;
  right: number;
  middle: number;
}

interface Geometry {
  width: number;
  height: number;
  boxes: Map<string, Box>;
}

const EMPTY: Geometry = { width: 0, height: 0, boxes: new Map() };

function sameGeometry(a: Geometry, b: Geometry): boolean {
  if (a.width !== b.width || a.height !== b.height || a.boxes.size !== b.boxes.size) {
    return false;
  }
  for (const [id, box] of a.boxes) {
    const other = b.boxes.get(id);
    if (
      other === undefined ||
      other.left !== box.left ||
      other.right !== box.right ||
      other.middle !== box.middle
    ) {
      return false;
    }
  }
  return true;
}

function measure(container: HTMLElement): Geometry {
  const origin = container.getBoundingClientRect();
  const boxes = new Map<string, Box>();
  for (const element of container.querySelectorAll<HTMLElement>("[data-node]")) {
    const rect = element.getBoundingClientRect();
    boxes.set(element.dataset.node ?? "", {
      left: Math.round(rect.left - origin.left),
      right: Math.round(rect.right - origin.left),
      middle: Math.round(rect.top - origin.top + rect.height / 2),
    });
  }
  return { width: Math.round(origin.width), height: Math.round(origin.height), boxes };
}

function curve(source: Box, target: Box): string {
  const bend = (source.right + target.left) / 2;
  return `M${source.right} ${source.middle}C${bend} ${source.middle} ${bend} ${target.middle} ${target.left} ${target.middle}`;
}

interface GraphProps {
  view: GraphView;
  // Nœud choisi par un clic, s'il y en a un.
  selected: string | null;
  onSelect: (id: string | null) => void;
  // Nœud mis en évidence quand rien n'est survolé ni choisi : la source d'un incident, par exemple.
  anchor?: string | null;
  // Prévient du nœud dont la chaîne est mise en évidence, pour le décrire à côté du graphe.
  onFocus?: (id: string | null) => void;
  // Remonter : seul ce dont le nœud dépend est mis en évidence, jusqu'aux données.
  upstream?: boolean;
}

// Les cinq colonnes et leurs liens. La chaîne du nœud survolé, choisi ou ancré est mise en évidence.
export function Graph({
  view,
  selected,
  onSelect,
  anchor = null,
  onFocus,
  upstream = false,
}: GraphProps) {
  const [hovered, setHovered] = useState<string | null>(null);
  const [geometry, setGeometry] = useState<Geometry>(EMPTY);
  const container = useRef<HTMLDivElement>(null);

  const columns = useMemo(() => columnsOf(view), [view]);
  const links = useMemo(() => linksOf(view), [view]);
  const focus = hovered ?? selected ?? anchor;
  const chain = useMemo(
    () => (focus === null ? null : (upstream ? upstreamOf : chainOf)(view, focus)),
    [view, focus, upstream],
  );

  const hover = (id: string | null) => {
    setHovered(id);
    onFocus?.(id ?? selected ?? anchor);
  };

  useLayoutEffect(() => {
    const element = container.current;
    if (element === null) {
      return;
    }
    const update = () => {
      const next = measure(element);
      setGeometry((current) => (sameGeometry(current, next) ? current : next));
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [view]);

  return (
    <div className={chain === null ? "graph" : "graph has-focus"} ref={container}>
      <svg
        className="links"
        data-testid="links"
        aria-hidden="true"
        width={geometry.width}
        height={geometry.height}
        viewBox={`0 0 ${geometry.width} ${geometry.height}`}
      >
        {links.map((link) => {
          const source = geometry.boxes.get(link.source);
          const target = geometry.boxes.get(link.target);
          if (source === undefined || target === undefined) {
            return null;
          }
          const lit = chain !== null && isLinkInChain(link, chain);
          return (
            <path
              key={`${link.source}>${link.target}`}
              className={lit ? "link is-on" : "link"}
              d={curve(source, target)}
            />
          );
        })}
      </svg>
      {columns.map((column) => (
        <section
          key={column.kind}
          className="column"
          data-column={column.kind}
          aria-labelledby={`column-${column.kind}`}
        >
          <h2 id={`column-${column.kind}`}>
            <span className="column-title">{column.title}</span>
            <span className="count">{column.nodes.length}</span>
          </h2>
          <ul>
            {column.nodes.map((node) => {
              const state = chain === null ? "" : chain.nodes.has(node.id) ? " is-on" : " is-off";
              const source = node.id === anchor ? " is-source" : "";
              return (
                <li key={node.id}>
                  <button
                    type="button"
                    className={`node node-${column.kind}${state}${source}`}
                    data-node={node.id}
                    aria-pressed={selected === node.id}
                    aria-label={node.detail}
                    title={node.detail}
                    onClick={() => onSelect(selected === node.id ? null : node.id)}
                    onMouseEnter={() => hover(node.id)}
                    onMouseLeave={() => hover(null)}
                    onFocus={() => hover(node.id)}
                    onBlur={() => hover(null)}
                  >
                    {node.prefix !== undefined && (
                      <span className="node-prefix">{node.prefix} </span>
                    )}
                    {node.label}
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
