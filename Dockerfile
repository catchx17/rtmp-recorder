FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY rtmp_recorder.py /app/rtmp_recorder.py

RUN mkdir -p /app/recordings /app/logs

EXPOSE 1935

ENTRYPOINT ["python", "-u", "/app/rtmp_recorder.py"]
