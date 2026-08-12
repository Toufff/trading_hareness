FROM python:3.12-slim

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

EXPOSE 8000
CMD ["python", "entrypoint.py", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
