FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml /app/
COPY tv /app/tv
RUN pip install --no-cache-dir .

COPY tv/entrypoint.sh /usr/local/bin/tv-entrypoint
RUN chmod 0755 /usr/local/bin/tv-entrypoint

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8081
ENTRYPOINT ["tv-entrypoint"]
CMD ["serve"]
