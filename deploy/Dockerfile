# Image de démonstration d'AnswerLineage : l'API et l'explorateur, le worker,
# le proxy et l'entreprise synthétique, installés depuis les versions verrouillées.

FROM public.ecr.aws/docker/library/node:22-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS explorer

WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY ui/ ./
RUN npm run build


FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

RUN pip install --no-cache-dir uv==0.11.32

# L'interpréteur de l'image, sans téléchargement ; les paquets sont installés
# construits, pas en lien vers les sources.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/opt/answerlineage \
    UV_LINK_MODE=copy

WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY api/ api/
COPY corpus/ corpus/
COPY proxy/ proxy/
COPY workers/ workers/
RUN uv sync --locked --all-packages --no-dev --no-editable

COPY --from=explorer /ui/dist /opt/answerlineage-ui

ENV PATH=/opt/answerlineage/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    ANSWERLINEAGE_UI_DIR=/opt/answerlineage-ui

# Aucun service de l'image ne tourne en administrateur.
RUN useradd --system --uid 10001 --no-create-home answerlineage
USER 10001
WORKDIR /

EXPOSE 8000
# Sans journal d'accès : il écrirait les paramètres des adresses demandées.
CMD ["uvicorn", "--factory", "answerlineage_api.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
