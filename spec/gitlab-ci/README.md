# Schéma de `.gitlab-ci.yml`

`ci.json` est la copie exacte du schéma JSON de `.gitlab-ci.yml` publié par GitLab 19.5.4 (`app/assets/javascripts/editor/schema/ci.json`, étiquette `v19.5.4-ee`, commit `ed0804f7094af2858b24c2e42c5cfd4887a39a21`). C'est celui que l'éditeur de pipeline de GitLab applique. Ne pas le modifier à la main.

Aucun GitLab n'est joignable depuis les tests du dépôt : `tests/test_ci_parity.py` valide `.gitlab-ci.yml` contre cette copie, puis contrôle ce que le schéma ne voit pas. Le schéma suit les versions de GitLab ; une nouvelle copie se prend d'une version publiée depuis plus de sept jours.

Licence MIT (Expat), selon l'avis de GitLab :

```text
Copyright (c) 2011-present GitLab Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
