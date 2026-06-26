# Adhan Pager — SIP call-to-prayer paging service
# Builds PJSIP 2.14.1 with the pjsua2 Python bindings, then installs the app.
FROM python:3.11-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PJ_VERSION=2.14.1 \
    LD_LIBRARY_PATH=/usr/local/lib \
    CONFIG_PATH=/config/config.yaml \
    AUDIO_DIR=/audio \
    PYTHONUNBUFFERED=1

# ---- system + build deps ----
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config swig \
        libasound2-dev libssl-dev libsrtp2-dev libopus-dev \
        wget ca-certificates ffmpeg tzdata \
    && ln -sf /usr/bin/python3 /usr/local/bin/python \
    && rm -rf /var/lib/apt/lists/*

# ---- build PJSIP with shared libs + pjsua2 python module ----
WORKDIR /tmp
RUN wget -q "https://github.com/pjsip/pjproject/archive/refs/tags/${PJ_VERSION}.tar.gz" \
    && tar xzf "${PJ_VERSION}.tar.gz" && rm "${PJ_VERSION}.tar.gz"
WORKDIR /tmp/pjproject-${PJ_VERSION}
RUN printf 'export CFLAGS += -fPIC\n' > user.mak \
    && ./configure --enable-shared --disable-video \
    && make dep && make && make install && ldconfig
# pjsua2 SWIG python bindings
WORKDIR /tmp/pjproject-${PJ_VERSION}/pjsip-apps/src/swig/python
RUN make && make install
RUN python -c "import pjsua2; print('pjsua2 OK')"

# ---- python app deps ----
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Fail the build early if the offline geo dataset can't load. Use python3 explicitly:
# `python` is symlinked to the Debian system interpreter above, but pip (and uvicorn at
# runtime) use /usr/local's python3, which is where geonamescache actually installs.
RUN python3 -c "import geonamescache as g; c=g.GeonamesCache(); assert len(c.get_cities())>1000 and len(c.get_countries())>100; print('geonamescache OK:', len(c.get_cities()), 'cities')"
RUN python3 -c "import bcrypt, itsdangerous; print('auth deps OK')"

# ---- app ----
COPY app ./app
RUN rm -rf /tmp/pjproject-${PJ_VERSION}

VOLUME ["/config", "/audio"]
EXPOSE 8080
# SIP signalling (UDP/TCP) + a generous RTP range for media
EXPOSE 5060/udp 5060/tcp

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
