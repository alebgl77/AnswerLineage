## Ce que change cette merge request

<!-- Le changement et sa raison, en quelques phrases. -->

## Liste de contrôle

- [ ] `make check` passe sur le poste.
- [ ] Les tests qui justifient le changement sont écrits, et la documentation de `docs/` suit le code.
- [ ] Le modèle de menaces (`docs/securite/modele-de-menaces.toml`, présenté par `docs/securite/modele-de-menaces.md`) est à jour : un composant, une frontière ou une entrée nouveaux y ont leurs menaces, chacune avec sa parade ou une acceptation à signer par le mainteneur. Un composant absent du modèle fait échouer `make policy`.
- [ ] Un chemin protégé modifié (workflows, `scripts/`, configuration d'agents, exceptions, modèle de menaces) est signalé ici : il attend la revue du mainteneur et le label `agent-config-approved`.
