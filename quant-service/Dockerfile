FROM python:3.12-slim

ARG APP_GIT_SHA=unknown
ARG APP_RELEASE=unknown
ARG APP_BUILD_CREATED_AT=unknown

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY quant-service/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY quant-service/app ./app
COPY quant-service/tests ./tests
COPY quant-service/alembic.ini ./alembic.ini
COPY quant-service/migrations ./migrations
COPY quant-service/entrypoint.py ./entrypoint.py
COPY scripts/audit-fuyao-capabilities.py ./tools/audit-fuyao-capabilities.py

ENV APP_GIT_SHA=${APP_GIT_SHA} \
    APP_RELEASE=${APP_RELEASE} \
    APP_BUILD_CREATED_AT=${APP_BUILD_CREATED_AT}
LABEL org.opencontainers.image.revision=${APP_GIT_SHA} \
      org.opencontainers.image.version=${APP_RELEASE} \
      org.opencontainers.image.created=${APP_BUILD_CREATED_AT}

EXPOSE 8000
CMD ["python", "entrypoint.py", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
