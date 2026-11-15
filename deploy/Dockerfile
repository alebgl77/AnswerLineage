# Image de démonstration d'AnswerLineage : l'API et l'explorateur, le worker,
# le proxy et l'entreprise synthétique, installés depuis les versions verrouillées.

FROM public.ecr.aws/docker/library/node:22-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS explorer

WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY ui/ ./
RUN npm run build


# L'environnement Python se construit dans une étape à part : ni les sources,
# ni uv n'entrent dans l'image finale.
FROM public.ecr.aws/docker/library/python:3.13-alpine3.23@sha256:bf88c40985006a79ca25458159a4da1dffc457f9ae234e948b2d6df56ca5ebd2 AS environment

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


# Une base Alpine : quelques paquets système seulement, donc peu de
# vulnérabilités à suivre. pip n'y reste pas : rien ne s'installe dans une
# image qui tourne.
FROM public.ecr.aws/docker/library/python:3.13-alpine3.23@sha256:bf88c40985006a79ca25458159a4da1dffc457f9ae234e948b2d6df56ca5ebd2

RUN python -m pip uninstall --yes pip

COPY --from=environment /opt/answerlineage /opt/answerlineage
COPY --from=explorer /ui/dist /opt/answerlineage-ui

ENV PATH=/opt/answerlineage/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    ANSWERLINEAGE_UI_DIR=/opt/answerlineage-ui

# Aucun service de l'image ne tourne en administrateur.
USER 10001:10001
WORKDIR /

EXPOSE 8000
# Sans journal d'accès : il écrirait les paramètres des adresses demandées.
CMD ["uvicorn", "--factory", "answerlineage_api.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
