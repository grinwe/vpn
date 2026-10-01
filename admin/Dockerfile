# syntax=docker/dockerfile:1.6

# Путь, под которым SPA будет опубликован. Vite запекает его во все
# asset-URL, а внутренний nginx внутри контейнера (см. ниже) отдаёт
# статику из одноимённой подпапки. Изменение → полный ребилд.
ARG VITE_ADMIN_BASE_PATH=admin

# ---- build ----
FROM node:20-alpine AS build
ARG VITE_ADMIN_BASE_PATH
ENV VITE_ADMIN_BASE_PATH=${VITE_ADMIN_BASE_PATH}
# Версия выката (файл VERSION в корне репозитория). Vite запекает её в бандл —
# без неё админка не смогла бы показать, какой билд отдаётся браузеру.
ARG VITE_APP_VERSION=0.0.0-dev
ENV VITE_APP_VERSION=${VITE_APP_VERSION}
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# ---- serve ----
# Tiny nginx just to hand out the static bundle. The *public* nginx on
# the host terminates TLS and proxies /${ADMIN_BASE_PATH}/ here, so this
# container only needs to speak plaintext HTTP on 80. nginx auto-renders
# default.conf.template on startup via envsubst (filter пинит только
# ADMIN_BASE_PATH, чтобы не затронуть $uri и прочие nginx-переменные).
FROM nginx:1.27-alpine
ARG VITE_ADMIN_BASE_PATH
COPY --from=build /app/dist /usr/share/nginx/html/${VITE_ADMIN_BASE_PATH}
COPY nginx.conf /etc/nginx/templates/default.conf.template
EXPOSE 80
