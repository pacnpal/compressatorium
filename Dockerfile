FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends mame-tools && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/images

# Use bash for all shell commands (including ENTRYPOINT)
SHELL ["/bin/bash", "-c"]

# Default mode (can be overridden when running the container)
ENV CHDMAN_MODE=createcd

# Convert all supported image files to .chd
ENTRYPOINT for i in *.gdi *.iso *.cue; do \
    [[ -e "$i" ]] || continue; \
    [[ -e "${i%.*}.chd" ]] && continue; \
    echo "Converting '$i' using chdman ${CHDMAN_MODE} ..."; \
    chdman "${CHDMAN_MODE}" -f -i "$i" -o "${i%.*}.chd"; \
done
