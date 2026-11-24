// La connexion de l'explorateur : OIDC, flux d'autorisation avec PKCE, sans dépendance.
//
// L'API dit, sans jeton, comment se connecter : son émetteur et le client public de
// l'explorateur (GET /api/v1/authentication). L'explorateur lit la découverte de
// l'émetteur, envoie la personne à son point d'autorisation avec un défi PKCE (S256,
// calculé par WebCrypto), puis échange le code reçu contre un jeton d'accès.
//
// Le jeton reste en mémoire : ni localStorage, ni cookie. Recharger la page en demande
// un nouveau, que l'émetteur donne sans rien redemander tant que sa session tient. Seuls
// le vérificateur PKCE, l'état, le nom occasionnel et l'adresse à rouvrir passent le
// temps de l'aller-retour dans sessionStorage, effacés au retour ; ils ne valent rien
// sans le code que l'émetteur ne rend qu'à l'adresse de l'explorateur.

export interface AuthSettings {
  mode: "oidc" | "disabled";
  issuer: string | null;
  client_id: string | null;
  scope: string | null;
}

interface Endpoints {
  authorization: string;
  token: string;
  endSession: string | null;
}

interface Session {
  accessToken: string;
  idToken: string | null;
  expiresAt: number;
}

interface Pending {
  verifier: string;
  state: string;
  nonce: string;
  returnTo: string;
}

// Ce que l'explorateur montre : la démonstration sans authentification, une session,
// une invitation à se connecter, ou un message fixe.
export type AuthState =
  | { kind: "loading" }
  | { kind: "disabled" }
  | { kind: "signedIn" }
  | { kind: "signedOut"; reason: "signedOut" | "expired" }
  | { kind: "unavailable"; reason: AuthFailure };

export type AuthFailure =
  | "settings" // L'API ne dit pas comment se connecter.
  | "unconfigured" // Aucun client de l'explorateur n'est réglé.
  | "issuer" // L'émetteur ne répond pas, ou pas comme il faut.
  | "refused" // L'émetteur a refusé la connexion, ou l'échange du code.
  | "storage"; // Le navigateur ne garde rien le temps de l'aller-retour.

const PENDING_KEY = "answerlineage.connexion";
const SIGNED_OUT_KEY = "answerlineage.deconnexion";
// La session prend fin un peu avant le jeton : une requête n'arrive pas avec un jeton expiré.
const EXPIRY_MARGIN_MS = 30_000;
const MAX_TOKEN_LENGTH = 16 * 1024;
const TOKEN68 = /^[A-Za-z0-9\-._~+/]+=*$/;
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

let session: Session | null = null;
let expiry: number | undefined;
const listeners = new Set<(state: AuthState) => void>();
let current: AuthState = { kind: "loading" };

function publish(state: AuthState): void {
  current = state;
  for (const listener of listeners) {
    listener(state);
  }
}

export function authState(): AuthState {
  return current;
}

export function onAuthChange(listener: (state: AuthState) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// L'en-tête d'une requête à l'API : le jeton de la session, s'il y en a une.
export function authorization(): Record<string, string> {
  return session === null ? {} : { authorization: `Bearer ${session.accessToken}` };
}

// L'API a refusé le jeton : la session prend fin, et la page invite à se reconnecter.
export function expire(): void {
  if (session === null) {
    return;
  }
  session = null;
  window.clearTimeout(expiry);
  publish({ kind: "signedOut", reason: "expired" });
}

function base64url(bytes: Uint8Array): string {
  let text = "";
  for (const byte of bytes) {
    text += String.fromCharCode(byte);
  }
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function random(): string {
  return base64url(crypto.getRandomValues(new Uint8Array(32)));
}

async function challengeOf(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

// Une adresse de l'émetteur : https, ou http sur la boucle locale.
function trusted(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2048) {
    return false;
  }
  try {
    const url = new URL(value);
    return (
      url.username === "" &&
      url.password === "" &&
      (url.protocol === "https:" || (url.protocol === "http:" && LOOPBACK.has(url.hostname)))
    );
  } catch {
    return false;
  }
}

// La découverte de l'émetteur : elle doit le nommer, et son point de jeton est sur son
// origine, la seule que la politique de sécurité du contenu laisse joindre.
async function discover(issuer: string): Promise<Endpoints | null> {
  try {
    const response = await fetch(`${issuer.replace(/\/$/, "")}/.well-known/openid-configuration`, {
      headers: { accept: "application/json" },
      credentials: "omit",
      redirect: "error",
    });
    if (!response.ok) {
      return null;
    }
    const document = (await response.json()) as Record<string, unknown>;
    const { authorization_endpoint: authorize, token_endpoint: token } = document;
    const endSession = document["end_session_endpoint"];
    if (document["issuer"] !== issuer || !trusted(authorize) || !trusted(token)) {
      return null;
    }
    if (new URL(token).origin !== new URL(issuer).origin) {
      return null;
    }
    return { authorization: authorize, token, endSession: trusted(endSession) ? endSession : null };
  } catch {
    return null;
  }
}

function redirectUri(): string {
  return `${window.location.origin}${window.location.pathname}`;
}

function take<T>(key: string): T | null {
  try {
    const raw = window.sessionStorage.getItem(key);
    window.sessionStorage.removeItem(key);
    return raw === null ? null : (JSON.parse(raw) as T);
  } catch {
    return null;
  }
}

// Va au point d'autorisation de l'émetteur, avec un défi PKCE neuf.
export async function signIn(settings: AuthSettings): Promise<void> {
  if (settings.issuer === null || settings.client_id === null) {
    publish({ kind: "unavailable", reason: "unconfigured" });
    return;
  }
  const endpoints = await discover(settings.issuer);
  if (endpoints === null) {
    publish({ kind: "unavailable", reason: "issuer" });
    return;
  }
  const pending: Pending = {
    verifier: random(),
    state: random(),
    nonce: random(),
    returnTo: window.location.hash,
  };
  try {
    window.sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending));
  } catch {
    publish({ kind: "unavailable", reason: "storage" });
    return;
  }
  const query = new URLSearchParams({
    response_type: "code",
    client_id: settings.client_id,
    redirect_uri: redirectUri(),
    scope: settings.scope ?? "openid",
    state: pending.state,
    nonce: pending.nonce,
    code_challenge: await challengeOf(pending.verifier),
    code_challenge_method: "S256",
  });
  window.location.assign(`${endpoints.authorization}?${query.toString()}`);
}

function start(accessToken: string, idToken: string | null, lifetime: number): void {
  const expiresAt = Date.now() + lifetime * 1000;
  session = { accessToken, idToken, expiresAt };
  window.clearTimeout(expiry);
  expiry = window.setTimeout(expire, Math.max(0, expiresAt - Date.now() - EXPIRY_MARGIN_MS));
  publish({ kind: "signedIn" });
}

// Le retour de l'émetteur : le code, échangé contre un jeton ; puis l'adresse d'avant.
async function complete(settings: AuthSettings, query: URLSearchParams): Promise<void> {
  const pending = take<Pending>(PENDING_KEY);
  // L'adresse perd le code tout de suite : il ne reste ni dans l'historique ni à l'écran.
  // Elle retrouve la vue d'avant la connexion, que la page relit (replaceState ne le dit pas).
  window.history.replaceState(null, "", `${window.location.pathname}${pending?.returnTo ?? ""}`);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
  const code = query.get("code");
  if (
    pending === null ||
    code === null ||
    query.get("state") !== pending.state ||
    settings.issuer === null ||
    settings.client_id === null
  ) {
    publish({ kind: "unavailable", reason: "refused" });
    return;
  }
  const endpoints = await discover(settings.issuer);
  if (endpoints === null) {
    publish({ kind: "unavailable", reason: "issuer" });
    return;
  }
  try {
    const response = await fetch(endpoints.token, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", accept: "application/json" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code,
        redirect_uri: redirectUri(),
        client_id: settings.client_id,
        code_verifier: pending.verifier,
      }),
      credentials: "omit",
      redirect: "error",
    });
    const answer = (await response.json()) as Record<string, unknown>;
    const token = answer["access_token"];
    const kind = answer["token_type"];
    const lifetime = answer["expires_in"];
    const identity = answer["id_token"];
    if (
      !response.ok ||
      typeof token !== "string" ||
      token.length > MAX_TOKEN_LENGTH ||
      !TOKEN68.test(token) ||
      typeof kind !== "string" ||
      kind.toLowerCase() !== "bearer"
    ) {
      publish({ kind: "unavailable", reason: "refused" });
      return;
    }
    const seconds = typeof lifetime === "number" && lifetime > 0 ? Math.min(lifetime, 86_400) : 300;
    start(token, typeof identity === "string" ? identity : null, seconds);
  } catch {
    publish({ kind: "unavailable", reason: "refused" });
  }
}

// Au chargement : le mode de l'API, puis le retour de l'émetteur, ou l'aller vers lui.
export async function startAuth(): Promise<AuthSettings | null> {
  let settings: AuthSettings;
  try {
    const response = await fetch("/api/v1/authentication", {
      headers: { accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error("réglage illisible");
    }
    settings = (await response.json()) as AuthSettings;
  } catch {
    publish({ kind: "unavailable", reason: "settings" });
    return null;
  }
  if (settings.mode === "disabled") {
    publish({ kind: "disabled" });
    return settings;
  }
  if (settings.issuer === null || settings.client_id === null) {
    publish({ kind: "unavailable", reason: "unconfigured" });
    return settings;
  }
  const query = new URLSearchParams(window.location.search);
  if (query.has("code") || query.has("error")) {
    await complete(settings, query);
    return settings;
  }
  if (take<boolean>(SIGNED_OUT_KEY) === true) {
    publish({ kind: "signedOut", reason: "signedOut" });
    return settings;
  }
  await signIn(settings);
  return settings;
}

// La déconnexion : la session est oubliée ici, puis chez l'émetteur s'il le permet.
export async function signOut(settings: AuthSettings): Promise<void> {
  const identity = session?.idToken ?? null;
  session = null;
  window.clearTimeout(expiry);
  try {
    window.sessionStorage.setItem(SIGNED_OUT_KEY, "true");
  } catch {
    // Sans stockage, la page proposera aussitôt de se reconnecter : rien n'est perdu.
  }
  const endpoints = settings.issuer === null ? null : await discover(settings.issuer);
  if (endpoints?.endSession != null && settings.client_id !== null) {
    const query = new URLSearchParams({
      client_id: settings.client_id,
      post_logout_redirect_uri: redirectUri(),
    });
    if (identity !== null) {
      query.set("id_token_hint", identity);
    }
    window.location.assign(`${endpoints.endSession}?${query.toString()}`);
    return;
  }
  publish({ kind: "signedOut", reason: "signedOut" });
}
