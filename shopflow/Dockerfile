# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/pip pip install --prefix=/install -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN pip install --prefix=/install --no-deps .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /install /usr/local
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin shop
USER 10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "shopflow"]
