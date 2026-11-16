// Libellés courts des nœuds : ils tiennent dans une colonne étroite.

const TIME = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
});

export function shortTime(instant: string): string {
  return TIME.format(new Date(instant)).replace(",", "");
}

// « 13/11 » et « 17:52 » : le jour s'efface quand la place manque.
export function shortDay(instant: string): string {
  return shortTime(instant).slice(0, 5);
}

export function shortHour(instant: string): string {
  return shortTime(instant).slice(-5);
}

// Dernier segment d'un nom pointé : « finance.ref.taux_change » se lit « taux_change ».
export function lastSegment(name: string): string {
  const cut = name.lastIndexOf(".");
  return cut === -1 ? name : name.slice(cut + 1);
}

export function shortFingerprint(fingerprint: string): string {
  const cut = fingerprint.indexOf(":");
  return fingerprint.slice(cut + 1, cut + 9);
}

export function plural(count: number, one: string, many: string): string {
  return `${count} ${count > 1 ? many : one}`;
}

// Un résultat tronqué ne donne que des minimums : il le dit à chaque décompte.
export function counted(count: number, lowerBound: boolean): string {
  return lowerBound ? `au moins ${count}` : String(count);
}
