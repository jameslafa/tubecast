FROM python:3.13-slim
# Deno runs the JavaScript YouTube uses to guard its video URLs; yt-dlp needs it for YouTube
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno
# ffmpeg cuts the sponsor segments out and embeds the cover image. A static build: Debian's
# package and its libraries would add 420 MB
COPY --from=mwader/static-ffmpeg:8.1.2 /ffmpeg /ffprobe /usr/local/bin/
# yt-dlp lives in a virtualenv the tubecast user owns, so tubecast can update it while running
RUN useradd --system --no-create-home tubecast && python -m venv /opt/venv \
    && mkdir /data && chown tubecast /data
ENV PATH=/opt/venv/bin:$PATH
RUN pip install --no-cache-dir pyyaml==6.0.3 "yt-dlp[default]" && chown -R tubecast /opt/venv
WORKDIR /app
COPY tubecast ./tubecast
USER tubecast
ENV PYTHONUNBUFFERED=1 HOME=/tmp DENO_DIR=/tmp/deno PORT=8091 TUBECAST_DATA_DIR=/data
EXPOSE 8091
VOLUME /data
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['PORT'])"
CMD ["python", "-m", "tubecast"]
