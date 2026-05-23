# RTMP Push Recorder

通用 RTMP 推流录制工具。适用于运动相机、手机直播 App、OBS、编码器等可以向 RTMP 地址推流的设备或软件。

工作方式：

1. 电脑先启动本工具，开启一个 RTMP 接收地址。
2. 推流设备或软件填写电脑显示的 RTMP 地址。
3. 推流开始后，电脑自动保存为视频文件。

默认不重新编码，默认每 10 分钟切成一个 MP4 文件。

## Docker 运行

先构建镜像：

```powershell
cd C:\Users\masterke\Desktop\rtmp-recorder
docker build -t rtmp-push-recorder .
```

运行容器：

```powershell
docker run --rm -it `
  -p 1935:1935 `
  -e PUBLIC_HOST=192.168.110.83 `
  -v "${PWD}\recordings:/app/recordings" `
  -v "${PWD}\logs:/app/logs" `
  rtmp-push-recorder
```

把 `PUBLIC_HOST` 改成这台电脑在局域网里的 IPv4 地址。当前常见值是：

```text
192.168.110.83
```

容器启动后会显示类似：

```text
完整 RTMP 地址：rtmp://192.168.110.83:1935/live/stream
如果分开填写：服务器 rtmp://192.168.110.83:1935/live，推流码 stream
```

推流端填写方式：

- 只有一个地址输入框：填完整地址 `rtmp://电脑IP:1935/live/stream`
- 分成服务器和推流码：服务器填 `rtmp://电脑IP:1935/live`，推流码填 `stream`

推流设备和电脑必须网络互通。Windows 防火墙弹窗时，请允许专用网络访问。

停止录制：在容器窗口里按 `Ctrl+C`。

## 常用参数

Docker 参数写在镜像名后面。

换推流码：

```powershell
docker run --rm -it -p 1935:1935 -e PUBLIC_HOST=192.168.110.83 `
  -v "${PWD}\recordings:/app/recordings" `
  -v "${PWD}\logs:/app/logs" `
  rtmp-push-recorder --stream-key bike
```

按 5 分钟切片：

```powershell
docker run --rm -it -p 1935:1935 -e PUBLIC_HOST=192.168.110.83 `
  -v "${PWD}\recordings:/app/recordings" `
  -v "${PWD}\logs:/app/logs" `
  rtmp-push-recorder --segment-time 300
```

录成单个文件：

```powershell
docker run --rm -it -p 1935:1935 -e PUBLIC_HOST=192.168.110.83 `
  -v "${PWD}\recordings:/app/recordings" `
  -v "${PWD}\logs:/app/logs" `
  rtmp-push-recorder --segment-time 0
```

指定输出文件名前缀：

```powershell
docker run --rm -it -p 1935:1935 -e PUBLIC_HOST=192.168.110.83 `
  -v "${PWD}\recordings:/app/recordings" `
  -v "${PWD}\logs:/app/logs" `
  rtmp-push-recorder --prefix camera_ride
```

## 输出目录

录制文件保存在宿主机：

```text
C:\Users\masterke\Desktop\rtmp-recorder\recordings
```

日志保存在宿主机：

```text
C:\Users\masterke\Desktop\rtmp-recorder\logs
```

## DJI Action 4 示例

如果用 DJI Action 4 / DJI Mimo，先启动 Docker 容器，然后把程序显示的 RTMP 地址填到 DJI Mimo 的 RTMP 直播设置里。

默认地址格式：

```text
rtmp://电脑IP:1935/live/stream
```

如果 DJI Mimo 分开填写，服务器是：

```text
rtmp://电脑IP:1935/live
```

推流码是：

```text
stream
```
