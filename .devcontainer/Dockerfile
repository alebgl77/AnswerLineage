# Conteneur de développement d'AnswerLineage.
# Il ne contient ni identifiant de publication ni jeton : tout ce qu'un agent de
# code y trouve peut être lu sans risque.
FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git make \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.11.32

RUN useradd --create-home --shell /bin/bash dev
USER dev
