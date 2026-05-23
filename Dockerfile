FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        nginx \
        libnginx-mod-rtmp \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY rtmp_recorder.py /app/rtmp_recorder.py
COPY nginx.conf /etc/nginx/nginx.conf

RUN mkdir -p /app/recordings /app/logs

EXPOSE 1935
EXPOSE 8080

ENTRYPOINT ["python", "-u", "/app/rtmp_recorder.py"]
CMD ["--start-nginx", "--nginx-conf", "/etc/nginx/nginx.conf"]
