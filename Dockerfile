FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends mame-tools && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/images

SHELL ["/bin/bash", "-c"]

ENTRYPOINT for i in *.gdi *.iso *.cue; do \
    [[ -e "$i" ]] || continue; \
    [[ -e "${i%.*}.chd" ]] && continue; \
    chdman createcd -f -i "$i" -o "${i%.*}.chd"; \
done