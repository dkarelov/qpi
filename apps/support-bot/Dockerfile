FROM node:24-alpine AS builder

WORKDIR /bot

COPY upstream/package.json ./package.json
COPY upstream/package-lock.json ./package-lock.json

RUN apk add --no-cache python3 build-base
RUN npm ci

COPY upstream/src ./src
COPY upstream/tsconfig.json ./tsconfig.json

RUN npm run build

FROM node:24-alpine AS production

WORKDIR /bot

COPY upstream/package.json ./package.json
COPY upstream/package-lock.json ./package-lock.json

RUN apk add --no-cache python3 build-base
RUN npm ci --omit=dev && npm cache clean --force

COPY --from=builder /bot/build ./build

ENV NODE_ENV=production

CMD ["node", "./build/index.js"]

