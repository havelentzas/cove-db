# cove-backup: Postgres client tools (same major version as the server), age for encryption and restic
# for off-site copies. Used by the `backup` service (no internet, DB access) and the `offsite` service
# (internet, no DB access). Build context is the repo root.
FROM restic/restic:0.20.0@sha256:93999df80d4ac6cb148e82d2ef1fe6a81996f17ff535e772723538211b31555a AS restic

FROM pgvector/pgvector:pg17@sha256:b513c48566e4777cd3370451400000c78eda6a08131d68f4c31d4a76f27bd8fd
RUN apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends age ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN rm -f /usr/local/bin/gosu   # the server entrypoint's privilege-drop helper; unused here
COPY --from=restic /usr/bin/restic /usr/local/bin/restic
COPY db/backup.sh db/offsite.sh /usr/local/lib/cove/
USER 1000:1000
