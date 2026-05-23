# RTMP Push Recorder

Generic RTMP push-stream recorder.

The Docker image contains:

- `nginx` with the RTMP module: receives RTMP publishers on port `1935`
- `ffmpeg`: pulls the stream from local nginx and records it to files
- `rtmp_recorder.py`: starts/manages the recorder and prints status logs

This is more compatible than using `ffmpeg -listen 1` as a bare RTMP server.

## RTMP URL

Default push URL:

```text
rtmp://SERVER_IP:1935/live/stream
```

If your camera/app has separate fields:

```text
Server: rtmp://SERVER_IP:1935/live
Stream key: stream
```

## Run With Docker

```bash
mkdir -p /root/rtmp-recorder/recordings /root/rtmp-recorder/logs
cd /root/rtmp-recorder

docker pull ghcr.io/catchx17/rtmp-recorder:latest

docker rm -f rtmp-recorder 2>/dev/null || true
docker run -d \
  --name rtmp-recorder \
  --restart unless-stopped \
  -p 1935:1935 \
  -v "$PWD/recordings:/app/recordings" \
  -v "$PWD/logs:/app/logs" \
  ghcr.io/catchx17/rtmp-recorder:latest
```

View logs:

```bash
docker logs -f rtmp-recorder
```

The container logs should show status lines like:

```text
Status: waiting for stream
Recording file opened: /app/recordings/recording_20260523_121500.mp4
Status: recording
```

When a publisher stops streaming, the container keeps running and waits for the
next publisher. It does not exit after one live session.

## Run With Docker Compose

```bash
mkdir -p recordings logs
docker compose up -d
docker compose logs -f
```

## Files

Recordings are written to:

```text
recordings/
```

ffmpeg logs are written to:

```text
logs/
```

Default filename format:

```text
recording_YYYYMMDD_HHMMSS.mp4
```

Default segment length is 10 minutes:

```text
--segment-time 600
```

## Common Options

Change stream key:

```bash
docker run ... ghcr.io/catchx17/rtmp-recorder:latest --stream-key bike
```

Then push to:

```text
rtmp://SERVER_IP:1935/live/bike
```

Change segment length to 5 minutes:

```bash
docker run ... ghcr.io/catchx17/rtmp-recorder:latest --segment-time 300
```

Record a single file:

```bash
docker run ... ghcr.io/catchx17/rtmp-recorder:latest --segment-time 0
```

Change filename prefix:

```bash
docker run ... ghcr.io/catchx17/rtmp-recorder:latest --prefix camera
```

## Network

Open inbound TCP `1935` on the server security group/firewall.

RTMP uses TCP, not UDP.

If `1935` is blocked by your cloud provider or network, map a different public port:

```bash
docker run -d \
  --name rtmp-recorder \
  -p 8080:1935 \
  -v "$PWD/recordings:/app/recordings" \
  -v "$PWD/logs:/app/logs" \
  ghcr.io/catchx17/rtmp-recorder:latest
```

Then push to:

```text
rtmp://SERVER_IP:8080/live/stream
```
