import { useEffect, useState } from "react";

import { AnswerView } from "./AnswerView";
import { authState, onAuthChange, signIn, signOut, startAuth } from "./auth";
import type { AuthFailure, AuthSettings, AuthState } from "./auth";
import { ImpactView } from "./ImpactView";
import { Overview } from "./Overview";
import { useRoute } from "./route";
import { TraceView } from "./TraceView";

// Les messages de la connexion, écrits d'avance : aucun ne recopie ce que l'émetteur renvoie.
const FAILURES: Record<AuthFailure, string> = {
  settings: "L'API ne dit pas comment se connecter. Est-elle lancée ?",
  unconfigured:
    "L'explorateur n'a pas de client chez l'émetteur OIDC : l'exploitant le règle dans ANSWERLINEAGE_OIDC_EXPLORER_CLIENT_ID.",
  issuer: "L'émetteur OIDC ne répond pas comme il le devrait. Réessayez dans un instant.",
  refused: "La connexion n'a pas abouti.",
  storage: "Le navigateur ne garde pas l'état de la connexion le temps de l'aller-retour.",
};

function useAuth(): [AuthState, AuthSettings | null] {
  const [state, setState] = useState<AuthState>(authState());
  const [settings, setSettings] = useState<AuthSettings | null>(null);
  useEffect(() => {
    const stop = onAuthChange(setState);
    void startAuth().then(setSettings);
    return stop;
  }, []);
  return [state, settings];
}

function Access({ state, settings }: { state: AuthState; settings: AuthSettings | null }) {
  if (state.kind === "loading" || (state.kind === "signedIn" && settings === null)) {
    return (
      <p className="notice" role="status">
        Connexion…
      </p>
    );
  }
  const message =
    state.kind === "unavailable"
      ? FAILURES[state.reason]
      : state.kind === "signedOut" && state.reason === "expired"
        ? "La session a expiré : reconnectez-vous pour lire le lignage."
        : "Vous êtes déconnecté. Le lignage ne se lit qu'avec un compte qui en a le rôle.";
  const retry = settings !== null && settings.client_id !== null;
  return (
    <div className="access">
      <p className="notice" role="alert">
        {message}
      </p>
      {retry && (
        <button type="button" onClick={() => void signIn(settings)}>
          Se connecter
        </button>
      )}
    </div>
  );
}

export function App() {
  const route = useRoute();
  const [auth, settings] = useAuth();
  const mode = route.view === "trace" ? "trace" : route.view === "impact" ? "impact" : "overview";
  const current = (shown: typeof mode) => (mode === shown ? "page" : undefined);
  const open = auth.kind === "disabled" || auth.kind === "signedIn";
  return (
    <main className="explorer">
      {auth.kind === "disabled" && (
        <p className="banner" role="note">
          Démonstration : l'authentification est désactivée. Cette API ne doit être jointe que
          par la boucle locale de ce poste.
        </p>
      )}
      <header className="bar">
        <h1>AnswerLineage</h1>
        <p className="tagline">De la donnée lue à la personne qui a reçu la réponse</p>
        <nav aria-label="Vues de l'explorateur">
          <a href="#/" aria-current={current("overview")}>
            Vue d'ensemble
          </a>
          <a href="#/remonter" aria-current={current("trace")}>
            Remonter
          </a>
          <a href="#/propager" aria-current={current("impact")}>
            Propager
          </a>
        </nav>
        {auth.kind === "signedIn" && settings !== null && (
          <button type="button" className="signout" onClick={() => void signOut(settings)}>
            Se déconnecter
          </button>
        )}
      </header>
      {!open && <Access state={auth} settings={settings} />}
      {open && route.view === "overview" && <Overview dataset={route.dataset} />}
      {open && route.view === "answer" && <AnswerView key={route.id} id={route.id} />}
      {open && route.view === "trace" && (
        <TraceView id={route.id} malformed={route.malformed === true} />
      )}
      {open && route.view === "impact" && (
        <ImpactView dataset={route.dataset} from={route.from} to={route.to} />
      )}
    </main>
  );
}
