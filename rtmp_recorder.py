#!/usr/bin/env python3
"""Record RTMP push streams with ffmpeg."""

from __future__ import annotations

import argparse
import datetime as dt
import http.server
import ipaddress
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path


SHOULD_STOP = False
NGINX_PROCESS: subprocess.Popen[bytes] | None = None
DEFAULT_TRIGGER_CONF = "/tmp/rtmp-recorder-notify.conf"


def log(message: str, *, stderr: bool = False) -> None:
    stream = sys.stderr if stderr else sys.stdout
    print(f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", file=stream, flush=True)


def configure_output() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except AttributeError:
        pass


def request_stop(signum: int, _frame: object) -> None:
    global SHOULD_STOP
    SHOULD_STOP = True
    log(f"Received signal {signum}; stopping recorder and finalizing the current file.")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def nginx_conf_token(value: object) -> str:
    token = str(value)
    if not token or re.search(r"[\s;{}]", token):
        raise ValueError(f"nginx config value is not safe to write unquoted: {token!r}")
    return token


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def display_exit_code(exit_code: int) -> int:
    if exit_code > 2**31 - 1:
        return exit_code - 2**32
    return exit_code


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    hostname = socket.gethostname()

    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                addresses.add(ip)
    except OSError:
        pass

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        if not ip.startswith("127."):
            addresses.add(ip)
    except OSError:
        pass
    finally:
        try:
            probe.close()
        except Exception:
            pass

    if os.name == "nt":
        try:
            ipconfig = subprocess.run(
                ["ipconfig"],
                check=False,
                capture_output=True,
                text=True,
                encoding="gbk",
                errors="ignore",
            )
            for line in ipconfig.stdout.splitlines():
                if "IPv4" not in line:
                    continue
                for match in re.findall(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", line):
                    addresses.add(match)
        except OSError:
            pass

    usable = []
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            continue
        if raw.startswith("198.18.") or raw.startswith("198.19."):
            continue
        if ip.is_private:
            usable.append(raw)

    def sort_key(raw: str) -> tuple[int, str]:
        if raw.startswith("192.168."):
            return (0, raw)
        if raw.startswith("10."):
            return (1, raw)
        return (2, raw)

    return sorted(set(usable), key=sort_key)


def rtmp_public_url(host: str, port: int, app: str, stream_key: str) -> str:
    return f"rtmp://{host}:{port}/{app.strip('/')}/{stream_key.strip('/')}"


def rtmp_pull_url(args: argparse.Namespace) -> str:
    return f"rtmp://{args.pull_host}:{args.pull_port}/{args.app.strip('/')}/{args.stream_key.strip('/')}"


def session_args(base_args: argparse.Namespace, app: str, stream_key: str) -> argparse.Namespace:
    args = argparse.Namespace(**vars(base_args))
    args.app = app
    args.stream_key = stream_key
    return args


def build_segment_output(out_dir: Path, prefix: str, ext: str) -> str:
    return str(out_dir / f"{prefix}_%Y%m%d_%H%M%S.{ext}")


def build_single_output(out_dir: Path, prefix: str, ext: str, name_timestamp: str) -> str:
    return str(out_dir / f"{prefix}_{name_timestamp}.{ext}")


def ffmpeg_command(args: argparse.Namespace, output_path: str) -> list[str]:
    input_url = rtmp_pull_url(args)
    command = [
        args.ffmpeg,
        "-hide_banner",
        "-loglevel",
        args.loglevel,
    ]

    if args.read_timeout:
        command.extend(["-rw_timeout", str(args.read_timeout * 1_000_000)])
    command.extend(["-i", input_url, "-map", "0", "-c", "copy"])

    if args.duration:
        command.extend(["-t", str(args.duration)])

    mp4_movflags = "+frag_keyframe+empty_moov+default_base_moof"
    if args.segment_time:
        command.extend(
            [
                "-f",
                "segment",
                "-segment_time",
                str(args.segment_time),
                "-reset_timestamps",
                "1",
                "-strftime",
                "1",
            ]
        )
        if args.ext == "mp4":
            command.extend(["-segment_format_options", f"movflags={mp4_movflags}", "-flush_packets", "1"])
        command.append(output_path)
    else:
        if args.ext == "mp4":
            command.extend(["-movflags", mp4_movflags, "-flush_packets", "1"])
        command.extend(["-y", output_path])

    return command


def stop_ffmpeg(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

    try:
        if process.stdin is not None:
            process.stdin.write(b"q")
            process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=15)
            return
    except Exception:
        pass

    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        process.wait(timeout=15)
        return
    except Exception:
        pass

    try:
        process.terminate()
        process.wait(timeout=10)
        return
    except Exception:
        pass

    process.kill()


def write_nginx_trigger_config(args: argparse.Namespace) -> None:
    config_path = Path(args.nginx_trigger_conf)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    publish_url = f"http://{args.notify_host}:{args.notify_port}/on_publish"
    publish_done_url = f"http://{args.notify_host}:{args.notify_port}/on_publish_done"
    lines = [
        "notify_method get;",
        "on_publish " + nginx_conf_token(publish_url) + ";",
        "on_publish_done " + nginx_conf_token(publish_done_url) + ";",
    ]
    config_path.write_text(
        "# Generated by rtmp_recorder.py. Do not edit inside the running container.\n"
        + "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )


def start_nginx(args: argparse.Namespace) -> subprocess.Popen[bytes] | None:
    if not args.start_nginx:
        return None

    if shutil.which(args.nginx) is None and not Path(args.nginx).exists():
        log("nginx was not found. Install nginx with the RTMP module or disable --start-nginx.", stderr=True)
        raise SystemExit(2)

    command = [args.nginx, "-g", "daemon off;"]
    if args.nginx_conf:
        command.extend(["-c", args.nginx_conf])

    log("Starting nginx-rtmp server.")
    log("Command: " + " ".join(f'"{part}"' if " " in part else part for part in command))
    process = subprocess.Popen(command)
    time.sleep(1)
    if process.poll() is not None:
        log(f"nginx exited early with code {display_exit_code(process.returncode)}.", stderr=True)
        raise SystemExit(process.returncode or 1)
    log(f"nginx started with PID {process.pid}.")
    return process


def stop_nginx(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    log("Stopping nginx.")
    try:
        process.terminate()
        process.wait(timeout=10)
        return
    except Exception:
        pass
    process.kill()


def print_push_addresses(args: argparse.Namespace) -> None:
    ips = [args.public_host] if args.public_host else local_ipv4_addresses()
    log("Use one of these RTMP addresses in your camera, phone app, OBS, or encoder:")
    if ips:
        for ip in ips:
            full = rtmp_public_url(ip, args.port, args.app, args.stream_key)
            server = f"rtmp://{ip}:{args.port}/{args.app.strip('/')}"
            log(f"  Full URL: {full}")
            log(f"  Split fields: server={server}, stream-key={args.stream_key}")
    else:
        log("  No local IPv4 address was detected. Set --public-host or PUBLIC_HOST.")
        log(f"  Format: rtmp://HOST:{args.port}/{args.app.strip('/')}/{args.stream_key.strip('/')}")
    log("The pushing device must be able to reach this host and TCP port.")


class RecorderSession:
    def __init__(
        self,
        key: tuple[str, str],
        process: subprocess.Popen[bytes],
        log_file: Path,
        ffmpeg_log: object,
    ) -> None:
        self.key = key
        self.process = process
        self.log_file = log_file
        self.ffmpeg_log = ffmpeg_log
        self.stopping = False

    def close_log(self) -> None:
        try:
            self.ffmpeg_log.close()
        except Exception:
            pass


class RecorderManager:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.Lock()
        self.sessions: dict[tuple[str, str], RecorderSession] = {}

    def start(self, app: str, stream_key: str) -> None:
        key = (app.strip("/"), stream_key.strip("/"))
        if not key[0] or not key[1]:
            raise ValueError("missing RTMP app or stream name")

        with self.lock:
            existing = self.sessions.get(key)
        if existing and existing.process.poll() is None:
            log(f"Replacing active recorder for app={key[0]}, stream-key={key[1]}.")
            self.stop(key)

        args = session_args(self.args, key[0], key[1])
        output_dir = Path(args.output).resolve()
        log_dir = Path(args.log_dir).resolve()
        recording_timestamp = timestamp()
        output_path = (
            build_segment_output(output_dir, args.prefix, args.ext)
            if args.segment_time
            else build_single_output(output_dir, args.prefix, args.ext, recording_timestamp)
        )
        log_file = log_dir / f"{args.prefix}_rtmp_{recording_timestamp}.log"
        command = ffmpeg_command(args, output_path)

        log(f"Publisher callback: app={key[0]}, stream-key={key[1]}; starting recorder.")
        log("Command: " + " ".join(f'"{part}"' if " " in part else part for part in command))
        log(f"ffmpeg log file: {log_file}")

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        ffmpeg_log = log_file.open("ab")
        ffmpeg_log.write(("\n\n=== start " + dt.datetime.now().isoformat(timespec="seconds") + " ===\n").encode())
        ffmpeg_log.flush()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=ffmpeg_log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except Exception:
            ffmpeg_log.close()
            raise

        session = RecorderSession(key, process, log_file, ffmpeg_log)
        with self.lock:
            self.sessions[key] = session

        threading.Thread(target=self._watch, args=(session,), daemon=True).start()

    def stop(self, key: tuple[str, str]) -> None:
        with self.lock:
            session = self.sessions.get(key)
        if not session or session.process.poll() is not None or session.stopping:
            return

        session.stopping = True
        log(f"Publisher finished: app={key[0]}, stream-key={key[1]}; stopping recorder.")
        threading.Thread(target=self._stop_process, args=(session,), daemon=True).start()

    def stop_all(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            if session.process.poll() is None:
                session.stopping = True
                stop_ffmpeg(session.process)
        for session in sessions:
            session.close_log()

    def _stop_process(self, session: RecorderSession) -> None:
        stop_ffmpeg(session.process)

    def _watch(self, session: RecorderSession) -> None:
        exit_code = session.process.wait()
        session.close_log()
        with self.lock:
            if self.sessions.get(session.key) is session:
                del self.sessions[session.key]
        if exit_code == 0:
            log(f"Recorder finished: app={session.key[0]}, stream-key={session.key[1]}.")
        else:
            log(
                "Recorder exited with code "
                f"{display_exit_code(exit_code)}: app={session.key[0]}, stream-key={session.key[1]}.",
                stderr=True,
            )


class NotifyHandler(http.server.BaseHTTPRequestHandler):
    server: "NotifyServer"

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _handle(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            post_params = urllib.parse.parse_qs(body, keep_blank_values=True)
            params.update(post_params)

        app = params.get("app", [""])[0]
        stream_key = params.get("name", [""])[0]
        try:
            if parsed.path == "/on_publish":
                self.server.manager.start(app, stream_key)
                if self.server.manager.args.notify_start_delay:
                    time.sleep(self.server.manager.args.notify_start_delay)
            elif parsed.path == "/on_publish_done":
                key = (app.strip("/"), stream_key.strip("/"))
                self.server.manager.stop(key)
            else:
                self._respond(404, "not found\n")
                return
        except Exception as exc:
            log(f"Notify callback failed: {exc}", stderr=True)
            self._respond(500, "error\n")
            return

        self._respond(200, "ok\n")

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class NotifyServer(http.server.ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], manager: RecorderManager) -> None:
        super().__init__(server_address, NotifyHandler)
        self.manager = manager


def start_notify_server(args: argparse.Namespace, manager: RecorderManager) -> NotifyServer:
    server = NotifyServer((args.notify_host, args.notify_port), manager)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Started RTMP notify callback server on {args.notify_host}:{args.notify_port}.")
    return server


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record an RTMP push stream. In Docker, nginx-rtmp receives the stream and ffmpeg records it."
    )
    parser.add_argument(
        "--public-host",
        default=os.environ.get("PUBLIC_HOST", ""),
        help="host/IP shown to publishers; set this to your server public IP in Docker",
    )
    parser.add_argument("--port", type=positive_int, default=1935, help="RTMP port, default: 1935")
    parser.add_argument("--pull-host", default="127.0.0.1", help="RTMP source host for ffmpeg, default: 127.0.0.1")
    parser.add_argument("--pull-port", type=positive_int, default=1935, help="RTMP source port for ffmpeg, default: 1935")
    parser.add_argument("--app", default="live", help="RTMP app path, default: live")
    parser.add_argument("--stream-key", default="stream", help="stream key, default: stream")
    parser.add_argument("-o", "--output", default="recordings", help="output directory, default: recordings")
    parser.add_argument("--prefix", default="recording", help="recording filename prefix, default: recording")
    parser.add_argument("--ext", default="mp4", choices=["mp4", "mkv", "mov"], help="output container format")
    parser.add_argument(
        "--segment-time",
        type=non_negative_int,
        default=600,
        help="segment length in seconds, default: 600; set to 0 for a single file",
    )
    parser.add_argument("--duration", type=positive_int, help="total recording seconds after stream starts")
    parser.add_argument(
        "--read-timeout",
        type=non_negative_int,
        default=5,
        help="ffmpeg RTMP read timeout in seconds; 0 disables",
    )
    parser.add_argument("--log-dir", default="logs", help="ffmpeg log directory, default: logs")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable path")
    parser.add_argument("--start-nginx", action="store_true", help="start nginx-rtmp before recording")
    parser.add_argument("--nginx", default="nginx", help="nginx executable path")
    parser.add_argument("--nginx-conf", default="", help="nginx config path")
    parser.add_argument(
        "--nginx-trigger-conf",
        default=os.environ.get("NGINX_TRIGGER_CONF", DEFAULT_TRIGGER_CONF),
        help=f"nginx include file written for trigger mode, default: {DEFAULT_TRIGGER_CONF}",
    )
    parser.add_argument(
        "--notify-host",
        default=os.environ.get("RTMP_NOTIFY_HOST", "127.0.0.1"),
        help="host for nginx-rtmp publish callbacks, default: 127.0.0.1",
    )
    parser.add_argument(
        "--notify-port",
        type=positive_int,
        default=int(os.environ.get("RTMP_NOTIFY_PORT", "8080")),
        help="port for nginx-rtmp publish callbacks, default: 8080",
    )
    parser.add_argument(
        "--notify-start-delay",
        type=non_negative_int,
        default=int(os.environ.get("RTMP_NOTIFY_START_DELAY", "1")),
        help="seconds to hold the publish callback after starting ffmpeg, default: 1",
    )
    parser.add_argument(
        "--loglevel",
        default="warning",
        choices=["quiet", "panic", "fatal", "error", "warning", "info", "verbose", "debug"],
        help="ffmpeg log level",
    )
    args = parser.parse_args(argv)

    if args.segment_time == 0:
        args.segment_time = None
    return args


def main(argv: list[str]) -> int:
    global NGINX_PROCESS
    configure_output()
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    args = parse_args(argv)
    if not args.start_nginx:
        log("nginx trigger recording requires --start-nginx.", stderr=True)
        return 2

    if shutil.which(args.ffmpeg) is None and not Path(args.ffmpeg).exists():
        log("ffmpeg was not found. Install ffmpeg or set --ffmpeg.", stderr=True)
        return 2

    output_dir = Path(args.output).resolve()
    log_dir = Path(args.log_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    args.output = str(output_dir)
    args.log_dir = str(log_dir)

    try:
        write_nginx_trigger_config(args)
    except ValueError as exc:
        log(str(exc), stderr=True)
        return 2

    manager = RecorderManager(args)
    try:
        notify_server = start_notify_server(args, manager)
    except OSError as exc:
        log(f"Could not start RTMP notify callback server: {exc}", stderr=True)
        return 2

    NGINX_PROCESS = start_nginx(args)

    print_push_addresses(args)
    log(f"Output directory: {output_dir}")
    log(f"ffmpeg log directory: {log_dir}")
    log("Record mode: trigger; nginx on_publish starts ffmpeg when a publisher connects.")
    log("Press Ctrl+C to stop nginx and the recorder service.")
    try:
        while not SHOULD_STOP:
            if NGINX_PROCESS is not None and NGINX_PROCESS.poll() is not None:
                log(f"nginx exited with code {display_exit_code(NGINX_PROCESS.returncode)}.", stderr=True)
                return NGINX_PROCESS.returncode or 1
            time.sleep(1)
        return 0
    finally:
        stop_nginx(NGINX_PROCESS)
        manager.stop_all()
        notify_server.shutdown()
        notify_server.server_close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
