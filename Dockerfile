FROM python:3.13-slim

# ffmpeg supplies ffprobe for the library scanner; deno + yt-dlp-ejs are what YouTube's JS
# challenges require. Baked in so a container rebuild can't silently regress the capability.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

ARG DENO_VERSION=v2.9.4
# Pinned: the installer fetches "latest" otherwise, so a rebuild could pull a Deno with
# breaking changes and snap yt-dlp's JS runtime with no code change on our side.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s "$DENO_VERSION" \
    && /usr/local/bin/deno --version

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt "yt-dlp==2026.7.4" "yt-dlp-ejs==0.8.0"

# tiddl in its own venv so its pins can't collide with the app's. Pinned: 3.4.4 is the
# version whose TIDDL_AUTH override and `download url` CLI are verified working; 2.x has a
# different CLI entirely and unpinned installs silently resolve down on older Pythons.
RUN python -m venv /opt/tiddl-venv \
    && /opt/tiddl-venv/bin/pip install --no-cache-dir 'tiddl==3.4.4'

WORKDIR /app
COPY buskarr /app/buskarr
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# HOME must be on the persistent volume: tiddl rewrites $HOME/.tiddl/auth.json on every
# token refresh, and a volatile HOME silently reverts Tidal to unauthenticated. compose
# also sets this; the image should be correct on its own.
ENV PYTHONUNBUFFERED=1 HOME=/state/home DENO_DIR=/tmp/deno
EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
