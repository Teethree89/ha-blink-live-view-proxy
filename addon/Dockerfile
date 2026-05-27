ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip ffmpeg

WORKDIR /opt/proxy
COPY proxy/ .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY build_config.py /opt/build_config.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
