# Stage 1: Build
FROM node:22-slim AS builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src/ ./src/
RUN npm run build

# Stage 2: Runtime
# @zoom/rtms native addon (rtms.node) requires GLIBCXX_3.4.32 which is not in
# Bookworm's libstdc++6. Pull the newer version from Debian Trixie (testing).
FROM node:22-slim
RUN echo "deb http://deb.debian.org/debian trixie main" > /etc/apt/sources.list.d/trixie.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends -t trixie libstdc++6 \
    && apt-get install -y --no-install-recommends curl \
    && rm /etc/apt/sources.list.d/trixie.list \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=builder /build/dist ./dist
ENV NODE_ENV=production
EXPOSE 8080
CMD ["node", "dist/index.js"]
