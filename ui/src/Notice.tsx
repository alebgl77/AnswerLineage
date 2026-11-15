// Messages d'attente et d'échec, les mêmes dans toutes les vues.

export function Loading() {
  return (
    <p className="notice" role="status">
      Chargement…
    </p>
  );
}

interface FailureProps {
  // Statut de la réponse de l'API ; 0 quand elle n'a pas répondu.
  status: number;
  // Message quand la ressource demandée n'existe pas.
  missing?: string;
}

export function Failure({ status, missing }: FailureProps) {
  const message =
    status === 404 && missing !== undefined
      ? missing
      : status === 422
        ? "La demande est invalide : vérifiez le jeu de données et la fenêtre."
        : "Le graphe n'a pas pu être chargé. L'API d'AnswerLineage est-elle lancée ?";
  return (
    <p className="notice notice-error" role="alert">
      {message}
    </p>
  );
}
