# Slim API image: FastAPI + in-process queue + CPU OCR. No Redis, Docling or PyTorch.
# Works on Render (Docker runtime), Cloud Run or a laptop. PORT is set by the host.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
