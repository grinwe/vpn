# syntax=docker/dockerfile:1.6

# ---- build ----
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# ---- serve ----
# Tiny nginx just to hand out the static bundle. The *public* nginx on
# the host terminates TLS and proxies /admin/ here, so this container
# only needs to speak plaintext HTTP on 80 and bind to loopback via the
# compose port mapping.
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html/admin
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
