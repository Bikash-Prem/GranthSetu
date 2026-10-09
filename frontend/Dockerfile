# Gateway image: builds the React app and serves it from nginx, which also
# load-balances /api to the API replicas. Build context: repository root.
FROM node:20-alpine AS build
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
ARG VITE_REPO_URL=""
ENV VITE_REPO_URL=$VITE_REPO_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY infra/nginx/gateway.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
