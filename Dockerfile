FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt /app/
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
       https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && printf '%s\n' \
       'Types: deb' \
       'URIs: https://apt.postgresql.org/pub/repos/apt' \
       'Suites: trixie-pgdg' \
       'Components: main' \
       'Signed-By: /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc' \
       > /etc/apt/sources.list.d/pgdg.sources \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt playwright==1.61.0 \
    && python -m playwright install --with-deps chromium

COPY . /app
RUN chmod +x /app/docker/entrypoint.sh
ENTRYPOINT ["/app/docker/entrypoint.sh"]
