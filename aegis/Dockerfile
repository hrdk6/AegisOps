# syntax=docker/dockerfile:1
# Build context: repository root (the image bundles SLOs, runbooks, scenarios and the
# declared ShopFlow manifests used by the demo fault injector's reset).
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY aegis/requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip pip install --prefix=/install -r requirements.lock
COPY aegis/pyproject.toml ./
COPY aegis/src ./src
RUN pip install --prefix=/install --no-deps .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /install /usr/local
WORKDIR /app
COPY config /app/config
COPY knowledge /app/knowledge
COPY benchmarks/scenarios /app/benchmarks/scenarios
COPY deploy/base/shop /app/deploy/shop
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin aegis
USER 10001
EXPOSE 8000 8090
CMD ["aegis-api"]
