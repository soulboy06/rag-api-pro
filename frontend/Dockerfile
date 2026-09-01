# Build the React/Vite application in an isolated Node stage.
FROM node:20-alpine AS build

WORKDIR /app

# Copy the lockfile first so dependency installation is cacheable and repeatable.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

# Serve the generated static files from a small production image.
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

