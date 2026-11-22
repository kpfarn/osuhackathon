# Build from this folder: docker build -t my-bot .
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
FROM oven/bun:1.4.4-slim
WORKDIR /bot
# Bun needs a writable cache in the scorer's read-only sandbox.
ENV HOME=/tmp
ENV BUN_RUNTIME_TRANSPILER_CACHE_PATH=/tmp/bun
COPY bot.ts map.ts mechfight.ts ./
ENTRYPOINT ["bun", "/bot/bot.ts"]
