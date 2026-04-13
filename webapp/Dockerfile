# syntax=docker/dockerfile:1.6

# ---- build ----
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# ---- serve ----
# Same pattern as admin/: tiny nginx hands out the static bundle on
# plaintext :80, the public nginx on the host terminates TLS and proxies
# /app/ here.
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html/app
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
