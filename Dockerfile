ARG PYTHON_IMAGE
ARG POSTGRES_IMAGE
FROM ${POSTGRES_IMAGE} AS postgres_client
COPY build/stage_postgres_client.sh /tmp/stage_postgres_client.sh
RUN sh /tmp/stage_postgres_client.sh
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY --from=postgres_client /opt/game-census-postgres /opt/game-census-postgres
COPY build/postgres_client.sh /usr/local/bin/pg_dump
COPY build/postgres_client.sh /usr/local/bin/pg_restore
RUN chmod 755 /usr/local/bin/pg_dump /usr/local/bin/pg_restore && pg_dump --version && pg_restore --version
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock
COPY src /app/src
COPY tests /app/tests
COPY pyproject.toml /app/pyproject.toml
COPY LICENSE /app/LICENSE
COPY tools/dev.py /app/tools/dev.py
COPY tools/probe_sources.py /app/tools/probe_sources.py
COPY build/toolchain.lock.json /app/build/toolchain.lock.json
ENTRYPOINT ["python", "-m", "game_census"]
