#!/usr/bin/env python3
"""Record RTMP push streams with ffmpeg."""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


SHOULD_STOP = False
NGINX_PROCESS: subprocess.Popen[bytes] | None = None


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


def rtmp_listen_url(bind_host: str, port: int, app: str, stream_key: str) -> str:
    return f"rtmp://{bind_host}:{port}/{app.strip('/')}/{stream_key.strip('/')}"


def rtmp_public_url(host: str, port: int, app: str, stream_key: str) -> str:
    return f"rtmp://{host}:{port}/{app.strip('/')}/{stream_key.strip('/')}"


def rtmp_pull_url(args: argparse.Namespace) -> str:
    return f"rtmp://{args.pull_host}:{args.pull_port}/{args.app.strip('/')}/{args.stream_key.strip('/')}"


def build_segment_output(out_dir: Path, prefix: str, ext: str) -> str:
    return str(out_dir / f"{prefix}_%Y%m%d_%H%M%S.{ext}")


def build_single_output(out_dir: Path, prefix: str, ext: str) -> str:
    return str(out_dir / f"{prefix}_{timestamp()}.{ext}")


def ffmpeg_command(args: argparse.Namespace, output_path: str) -> list[str]:
    input_url = rtmp_pull_url(args)
    command = [
        args.ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        args.loglevel,
        "-i",
        input_url,
        "-map",
        "0",
        "-c",
        "copy",
    ]

    if args.duration:
        command.extend(["-t", str(args.duration)])

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
                output_path,
            ]
        )
    else:
        command.extend(["-y", output_path])

    return command


def stop_ffmpeg(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

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


def recent_output_file(out_dir: Path, prefix: str, ext: str, started_at: float) -> Path | None:
    candidates: list[Path] = []
    for candidate in out_dir.glob(f"{prefix}_*.{ext}"):
        try:
            if candidate.stat().st_mtime >= started_at - 1:
                candidates.append(candidate)
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def format_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


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


def run_once(args: argparse.Namespace, output_path: str, log_file: Path) -> int:
    command = ffmpeg_command(args, output_path)
    output_dir = Path(args.output).resolve()
    log("Starting stream recorder.")
    log("Command: " + " ".join(f'"{part}"' if " " in part else part for part in command))
    log(f"ffmpeg log file: {log_file}")
    log(f"Waiting for stream from nginx-rtmp: {rtmp_pull_url(args)}")

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with log_file.open("ab") as ffmpeg_log:
        ffmpeg_log.write(("\n\n=== start " + dt.datetime.now().isoformat(timespec="seconds") + " ===\n").encode())
        ffmpeg_log.flush()
        started_at = time.time()
        last_status_at = 0.0
        last_file: Path | None = None
        last_size: int | None = None
        last_growth_at = started_at
        process = subprocess.Popen(
            command,
            stdout=ffmpeg_log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        log(f"ffmpeg started with PID {process.pid}.")
        try:
            while process.poll() is None:
                now = time.time()
                if SHOULD_STOP:
                    stop_ffmpeg(process)
                    break

                current_file = recent_output_file(output_dir, args.prefix, args.ext, started_at)
                if current_file and current_file != last_file:
                    last_file = current_file
                    log(f"Recording file opened: {current_file}")

                if now - last_status_at >= args.status_interval:
                    elapsed = int(now - started_at)
                    if current_file and current_file.exists():
                        size = current_file.stat().st_size
                        if last_size is None:
                            delta = size
                        else:
                            delta = max(0, size - last_size)
                        if delta > 0:
                            last_growth_at = now
                        last_size = size
                        log(
                            "Status: recording, "
                            f"elapsed={elapsed}s, file={current_file.name}, "
                            f"size={format_size(size)}, delta={format_size(delta)}"
                        )
                    else:
                        log(f"Status: waiting for stream, elapsed={elapsed}s")
                    last_status_at = now

                if current_file and args.idle_timeout and now - last_growth_at > args.idle_timeout:
                    log(
                        f"No recording data for {args.idle_timeout}s; restarting ffmpeg "
                        "to wait for the next publisher."
                    )
                    stop_ffmpeg(process)
                    return 124

                if args.wait_timeout and now - started_at > args.wait_timeout and current_file is None:
                    log(f"No stream arrived within {args.wait_timeout}s; stopping this attempt.")
                    stop_ffmpeg(process)
                    return 124

                time.sleep(0.5)
        finally:
            if SHOULD_STOP:
                stop_ffmpeg(process)

        return process.wait()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record an RTMP push stream. In Docker, nginx-rtmp receives the stream and ffmpeg records it."
    )
    parser.add_argument("--bind", default="0.0.0.0", help="listen address, default: 0.0.0.0")
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
    parser.add_argument("--wait-timeout", type=non_negative_int, default=0, help="stream wait timeout; 0 waits forever")
    parser.add_argument("--max-retries", type=int, default=-1, help="max retries after failure; -1 retries forever")
    parser.add_argument("--reconnect-delay", type=positive_int, default=5, help="seconds before retry")
    parser.add_argument("--status-interval", type=positive_int, default=10, help="status log interval in seconds")
    parser.add_argument("--idle-timeout", type=positive_int, default=30, help="restart ffmpeg after this many seconds without recorded data")
    parser.add_argument("--log-dir", default="logs", help="ffmpeg log directory, default: logs")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable path")
    parser.add_argument("--start-nginx", action="store_true", help="start nginx-rtmp before recording")
    parser.add_argument("--nginx", default="nginx", help="nginx executable path")
    parser.add_argument("--nginx-conf", default="", help="nginx config path")
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
    if shutil.which(args.ffmpeg) is None and not Path(args.ffmpeg).exists():
        log("ffmpeg was not found. Install ffmpeg or set --ffmpeg.", stderr=True)
        return 2

    NGINX_PROCESS = start_nginx(args)

    output_dir = Path(args.output).resolve()
    log_dir = Path(args.log_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    args.output = str(output_dir)
    args.log_dir = str(log_dir)

    print_push_addresses(args)
    log(f"Output directory: {output_dir}")
    log(f"ffmpeg log directory: {log_dir}")
    log("Press Ctrl+C to stop and finalize the current recording.")

    retries = 0
    while not SHOULD_STOP:
        output_path = (
            build_segment_output(output_dir, args.prefix, args.ext)
            if args.segment_time
            else build_single_output(output_dir, args.prefix, args.ext)
        )
        log_file = log_dir / f"{args.prefix}_rtmp_{timestamp()}.log"
        exit_code = run_once(args, output_path, log_file)

        if SHOULD_STOP:
            log("Recorder stopped.")
            stop_nginx(NGINX_PROCESS)
            return 0

        if exit_code == 0:
            if args.duration:
                log("ffmpeg exited normally after the configured duration.")
                stop_nginx(NGINX_PROCESS)
                return 0
            retries = 0
            log(f"Stream ended; waiting for the next publisher in {args.reconnect_delay}s.")
            time.sleep(args.reconnect_delay)
            continue

        retries += 1
        if args.max_retries >= 0 and retries > args.max_retries:
            log(
                f"ffmpeg exited with code {display_exit_code(exit_code)}; max retries reached.",
                stderr=True,
            )
            stop_nginx(NGINX_PROCESS)
            return exit_code

        log(
            f"ffmpeg exited with code {display_exit_code(exit_code)}; "
            f"retrying in {args.reconnect_delay}s (attempt {retries})."
        )
        time.sleep(args.reconnect_delay)

    stop_nginx(NGINX_PROCESS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
