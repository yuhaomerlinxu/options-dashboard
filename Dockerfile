FROM python:3.12-slim
ARG LB_VERSION=v0.30.0
ARG TARGETARCH=amd64
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
 && cd /tmp && F=longbridge-terminal-linux-musl-${TARGETARCH}.tar.gz \
 && curl -fsSLO https://github.com/longbridge/longbridge-terminal/releases/download/${LB_VERSION}/$F \
 && curl -fsSLO https://github.com/longbridge/longbridge-terminal/releases/download/${LB_VERSION}/$F.sha256 \
 && [ "$(cut -d' ' -f1 $F.sha256)" = "$(sha256sum $F | cut -d' ' -f1)" ] \
 && tar xzf $F && install -m 755 $(find . -name longbridge -type f | head -1) /usr/local/bin/longbridge \
 && rm -rf /tmp/* && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
RUN useradd -m app
WORKDIR /app
COPY --chown=app:app server.py providers.py mcp_client.py entrypoint.sh ./
COPY --chown=app:app static/index.html static/
USER app
ENV PORT=8080 PYTHONUNBUFFERED=1 LONGBRIDGE_NO_ANALYTICS=1
EXPOSE 8080
CMD ["./entrypoint.sh"]
