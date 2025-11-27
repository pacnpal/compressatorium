FROM debian:trixie-slim

# Install modern MAME tools (includes a recent chdman with `createdvd` support)
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
      mame-tools \
      bash && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Directory where your images will be mounted
WORKDIR /tmp/images

# Use bash for the ENTRYPOINT script
SHELL ["/bin/bash", "-c"]

# Default mode: createcd (can be overridden at runtime)
ENV CHDMAN_MODE=createcd

# Convert all supported image files to .chd
ENTRYPOINT \
  mode="${CHDMAN_MODE:-createcd}"; \
  case "$mode" in \
    cd)  mode="createcd"  ;; \
    dvd) mode="createdvd" ;; \
    createcd|createdvd) ;; \
    *) echo "Unsupported CHDMAN_MODE: '$mode'. Use 'createcd' or 'createdvd'." >&2; exit 1 ;; \
  esac; \
  shopt -s nullglob; \
  for i in *.gdi *.iso *.cue; do \
    [[ -e "$i" ]] || continue; \
    [[ -e "${i%.*}.chd" ]] && { \
      echo "Skipping '$i' (CHD already exists)."; \
      continue; \
    }; \
    echo "Converting '$i' using chdman ${mode} ..."; \
    if [[ "$mode" == "createdvd" ]]; then \
      chdman createdvd -hs 2048 -f -i "$i" -o "${i%.*}.chd"; \
    else \
      chdman createcd -f -i "$i" -o "${i%.*}.chd"; \
    fi; \
  done