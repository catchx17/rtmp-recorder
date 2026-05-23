#!/usr/bin/env python3
"""RTMP push-stream recorder wrapper around ffmpeg."""

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


def request_stop(signum: int, _frame: object) -> None:
    global SHOULD_STOP
    SHOULD_STOP = True
    print(f"\n收到停止信号 {signum}，正在让 ffmpeg 收尾当前文件...", flush=True)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是大于等于 0 的整数")
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


def build_segment_output(out_dir: Path, prefix: str, ext: str) -> str:
    return str(out_dir / f"{prefix}_%Y%m%d_%H%M%S.{ext}")


def build_single_output(out_dir: Path, prefix: str, ext: str) -> str:
    return str(out_dir / f"{prefix}_{timestamp()}.{ext}")


def ffmpeg_command(args: argparse.Namespace, output_path: str) -> list[str]:
    input_url = rtmp_listen_url(args.bind, args.port, args.app, args.stream_key)
    command = [
        args.ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        args.loglevel,
        "-listen",
        "1",
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


def output_started(out_dir: Path, prefix: str, ext: str, started_at: float) -> bool:
    for candidate in out_dir.glob(f"{prefix}_*.{ext}"):
        try:
            if candidate.stat().st_mtime >= started_at - 1:
                return True
        except OSError:
            continue
    return False


def print_push_addresses(args: argparse.Namespace) -> None:
    ips = [args.public_host] if args.public_host else local_ipv4_addresses()
    print("在推流设备或直播软件里填写：")
    if ips:
        for ip in ips:
            full = rtmp_public_url(ip, args.port, args.app, args.stream_key)
            server = f"rtmp://{ip}:{args.port}/{args.app.strip('/')}"
            print(f"  完整 RTMP 地址：{full}")
            print(f"  如果分开填写：服务器 {server}，推流码 {args.stream_key}")
    else:
        print("  没有自动识别到局域网 IP。请用 ipconfig 查看电脑 IPv4 地址。")
        print(
            "  格式："
            f"rtmp://电脑IPv4:{args.port}/{args.app.strip('/')}/{args.stream_key.strip('/')}"
        )
    print()
    print("推流设备和这台电脑必须网络互通。Windows 防火墙弹窗时请选择允许专用网络。")


def run_once(args: argparse.Namespace, output_path: str, log_file: Path) -> int:
    command = ffmpeg_command(args, output_path)
    print("启动 RTMP 接收录制：")
    print(" ".join(f'"{part}"' if " " in part else part for part in command))
    print(f"日志文件：{log_file}")
    print("等待推流连接...")

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with log_file.open("ab") as log:
        log.write(("\n\n=== start " + dt.datetime.now().isoformat(timespec="seconds") + " ===\n").encode())
        started_at = time.time()
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        try:
            while process.poll() is None:
                if SHOULD_STOP:
                    stop_ffmpeg(process)
                    break
                if (
                    args.wait_timeout
                    and time.time() - started_at > args.wait_timeout
                    and not output_started(Path(args.output).resolve(), args.prefix, args.ext, started_at)
                ):
                    print(f"{args.wait_timeout} 秒内没有收到推流，终止本次等待。")
                    stop_ffmpeg(process)
                    return 124
                time.sleep(0.5)
        finally:
            if SHOULD_STOP:
                stop_ffmpeg(process)

        return process.wait()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="开启本机 RTMP 接收地址，录制摄像机、手机、OBS 等设备或软件推过来的视频流。"
    )
    parser.add_argument("--bind", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument(
        "--public-host",
        default=os.environ.get("PUBLIC_HOST", ""),
        help="展示给推流端填写的主机名或 IP；Docker 运行时建议设置为电脑局域网 IP",
    )
    parser.add_argument("--port", type=positive_int, default=1935, help="RTMP 端口，默认 1935")
    parser.add_argument("--app", default="live", help="RTMP app 路径，默认 live")
    parser.add_argument("--stream-key", default="stream", help="推流码，默认 stream")
    parser.add_argument("-o", "--output", default="recordings", help="输出目录，默认 recordings")
    parser.add_argument("--prefix", default="recording", help="文件名前缀，默认 recording")
    parser.add_argument("--ext", default="mp4", choices=["mp4", "mkv", "mov"], help="输出封装格式")
    parser.add_argument(
        "--segment-time",
        type=non_negative_int,
        default=600,
        help="切片秒数，默认 600；设为 0 表示录成单个文件",
    )
    parser.add_argument("--duration", type=positive_int, help="收到推流后的总录制秒数；不填则一直录到 Ctrl+C")
    parser.add_argument("--wait-timeout", type=non_negative_int, default=0, help="等待推流秒数；0 表示一直等待")
    parser.add_argument("--max-retries", type=int, default=-1, help="异常退出最大重试次数；-1 表示无限重试")
    parser.add_argument("--reconnect-delay", type=positive_int, default=5, help="断开后重试等待秒数")
    parser.add_argument("--log-dir", default="logs", help="日志目录，默认 logs")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument(
        "--loglevel",
        default="warning",
        choices=["quiet", "panic", "fatal", "error", "warning", "info", "verbose", "debug"],
        help="ffmpeg 日志等级",
    )
    args = parser.parse_args(argv)

    if args.segment_time == 0:
        args.segment_time = None
    return args


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    args = parse_args(argv)
    if shutil.which(args.ffmpeg) is None and not Path(args.ffmpeg).exists():
        print("找不到 ffmpeg。请先安装 ffmpeg，或用 --ffmpeg 指定 ffmpeg.exe 路径。", file=sys.stderr)
        return 2

    output_dir = Path(args.output).resolve()
    log_dir = Path(args.log_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    args.output = str(output_dir)
    args.log_dir = str(log_dir)

    print_push_addresses(args)
    print(f"输出目录：{output_dir}")
    print("按 Ctrl+C 停止录制。")

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
            print("录制已停止。")
            return 0

        if exit_code == 0:
            print("ffmpeg 正常结束。")
            return 0

        retries += 1
        if args.max_retries >= 0 and retries > args.max_retries:
            print(
                f"ffmpeg 异常退出，退出码 {display_exit_code(exit_code)}；已达到最大重试次数。",
                file=sys.stderr,
            )
            return exit_code

        print(
            f"ffmpeg 异常退出，退出码 {display_exit_code(exit_code)}；{args.reconnect_delay} 秒后重新等待推流"
            f"（第 {retries} 次）。"
        )
        time.sleep(args.reconnect_delay)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
