# The Discord bridge (bridge/README.md). Base pinned by digest, deps pinned by hash.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

# uid/gid 1000 = itzg/minecraft-server's default, so the mod and the bridge share mc_data/mcve.
RUN groupadd -g 1000 bridge && useradd -u 1000 -g 1000 -M -d /nonexistent -s /usr/sbin/nologin bridge
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /app/requirements.txt
# No setuid/setgid binaries (su, mount, passwd, ...): defence in depth under the empty capability bounding set.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +
COPY core.py bridge.py entrypoint.sh /app/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
# Starts as root only to own /mcve (Docker creates a missing bind source root-owned), then drops to 1000.
ENTRYPOINT ["/app/entrypoint.sh"]
