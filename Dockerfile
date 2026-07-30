FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
COPY requirements.txt ./
RUN pip install -r requirements.txt --no-cache-dir

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
