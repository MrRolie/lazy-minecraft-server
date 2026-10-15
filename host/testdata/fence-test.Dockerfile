# A throwaway netns with iptables, for host/test_host.py's real test of host/bin/mc-bridge-fence (rv3):
# run with --cap-add NET_ADMIN --network none, so the rules land in the container's own network namespace.
FROM ubuntu@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
RUN apt-get update && apt-get install -y --no-install-recommends iptables && rm -rf /var/lib/apt/lists/*
