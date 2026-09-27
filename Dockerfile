FROM debian:trixie-slim

# apt statt pip: libvirt-python muss so nicht kompiliert werden.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 python3-flask python3-waitress python3-libvirt \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin vmdash

WORKDIR /app
COPY app/ /app/app/
COPY client/ /app/client/
# Mitschnitt und Template-XML für den Mock-Modus
COPY testdata/boot-debian13-luks.txt testdata/sap-template.xml /app/testdata/

ARG VERSION=dev
ENV VMDASH_VERSION=${VERSION} \
    VMDASH_PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER vmdash
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('VMDASH_PORT','8000'), timeout=3)" || exit 1

# Genau ein Prozess (waitress mit Threads): Jobs liegen im Arbeitsspeicher.
CMD ["python3", "-m", "app"]
