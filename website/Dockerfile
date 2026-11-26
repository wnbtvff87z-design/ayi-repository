FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LEADS_PRODUCTION=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py store.py mailer.py gunicorn.conf.py schema.sql ./
COPY templates ./templates
COPY static ./static
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:create_app()"]
