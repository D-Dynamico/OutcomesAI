FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY pytest.ini .

# One image, three roles: the compose file picks the command (api, worker, mock-ai).
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
