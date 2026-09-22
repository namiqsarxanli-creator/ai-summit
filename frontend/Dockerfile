# FACEMARK frontend — Next.js dev container.
#
# Node is already the right major version on most dev machines, but
# containerizing it keeps the documented `docker compose up` workflow
# (IMPLEMENTATION_PLAN.md M01 acceptance criterion #1: "all three
# services healthy") reproducible regardless of what is installed on
# the host.

FROM node:24-slim

WORKDIR /app

# Installed first so this layer caches across source-only changes.
COPY package.json ./
RUN npm install

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev"]
