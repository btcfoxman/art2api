FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY main.py .
RUN mkdir -p /app/data
EXPOSE 8797
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8797", "--no-access-log"]
