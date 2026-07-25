FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Разовые операционные скрипты (миграции секретов и т.п.) — чтобы их можно было
# запускать как `docker compose exec backend python -m scripts.<name>`, а не
# пробрасывать файл через stdin (аудит 2026-07-25).
COPY scripts ./scripts
COPY alembic.ini ./alembic.ini
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
