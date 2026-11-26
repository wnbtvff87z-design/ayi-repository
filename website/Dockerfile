FROM python:3.12-slim
WORKDIR /app
ENV TZ=Europe/Madrid PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 LEADS_PRODUCTION=1
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py store.py mailer.py schema.sql gunicorn.conf.py ./
COPY templates ./templates
COPY static ./static
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:create_app()"]
