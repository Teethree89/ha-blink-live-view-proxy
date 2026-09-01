# Standalone proxy image, for hosts with no systemd and no Supervisor — a NAS,
# a Docker-only box, Home Assistant Container on something that is not Debian.
#
# The add-on image (addon/Dockerfile) is a different thing: it is built by
# Supervisor from a Home Assistant base image and driven by bashio. This one is
# plain Docker and configures itself from the environment.
FROM python:3.13-slim

# ffmpeg does the HLS packaging and the push-to-talk AAC encode.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/proxy

# Requirements first, so a code change does not reinstall blinkpy every build.
COPY proxy/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy/ ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Everything that must survive a container replacement: the Blink refresh
# token, the generated proxy token, the config, HLS and live-view caches.
VOLUME ["/data"]
EXPOSE 8088

ENV BLINK_PROXY_CONFIG=/data/config.json \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/entrypoint.sh"]
