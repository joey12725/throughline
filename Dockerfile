# Throughline: one image for every compose service. The API (`throughline
# serve`), the owner commands (migrate, tokens, backups) and the scheduler that
# runs server connectors and maintenance jobs (`throughline-companion run
# --server`) all start from it, so an install or update builds once.
# Installed from uv.lock. postgresql-client, gnupg, zstd, rsync and ssh serve
# encrypted backups, their off-site copy and pre-upgrade snapshots; ffmpeg the
# media connectors.

# Stage 1: the web interface. vite builds web/ into ../src/throughline/web/static.
FROM node:25-slim AS web
WORKDIR /app/web
# The pnpm version that wrote web/pnpm-lock.yaml (CI pins the same one).
RUN npm install -g pnpm@12.15.0
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web ./
# Type-checked lens fixtures read the zoo's channel catalogue.
COPY fixtures/zoo_channels.json /app/fixtures/zoo_channels.json
RUN pnpm build

# Stage 2: the API, CLI, companion scheduler and the built interface (served under /ui/).
# fixtures/ (the invented public twin) is part of the image so a demo install
# can be seeded with `docker compose exec throughline python -m fixtures`.
FROM python:3.12-slim
LABEL org.opencontainers.image.title="Throughline" \
      org.opencontainers.image.description="Self-hosted personal data hub" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client gnupg zstd rsync openssh-client ca-certificates ffmpeg \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY . .
COPY --from=web /app/src/throughline/web/static ./src/throughline/web/static
RUN uv sync --frozen --no-dev --all-packages --extra cookies \
 && useradd --create-home --uid 10001 throughline \
 && install -d -o throughline -g throughline /home/throughline/companion \
 && install -d -o throughline -g throughline -m 700 /home/throughline/.throughline \
 && install -d -o throughline -g throughline -m 700 /home/throughline/pairing
ENV PATH="/app/.venv/bin:$PATH" \
    THROUGHLINE_COMPANION_HOME=/home/throughline/companion \
    PYTHONUNBUFFERED=1
USER throughline
EXPOSE 8000
CMD ["throughline", "serve", "--host", "0.0.0.0"]
