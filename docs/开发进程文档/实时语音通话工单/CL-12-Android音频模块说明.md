# CL-12 Android 音频模块说明

## 边界

- `app/modules/call-audio` 是 Expo SDK 54 本地 Android 模块，不引入新的第三方依赖
- 采集固定为 PCM16、16 kHz、单声道；播放固定为 PCM16、24 kHz、单声道，不做静默采样率回退
- Base64 只用于 Expo 原生桥接传递字节；`call.v1` WebSocket 仍使用现有二进制帧 codec
- 模块不发送 socket ACK，不决定通话生命周期；后台事件、音频错误、路由观察和停止回执交给上层 coordinator
- Expo Go、非 Android、模块缺失或实际格式初始化失败时能力为 unavailable

## 完成与停止语义

- final 入队、写入 `AudioTrack` 或服务端 final 帧均不产生完成回执
- 模块累计 `playbackHeadPosition` 的无符号 32 位差值；只有播放头消费到 final 对应的最后一帧才发送 `onPlaybackCompleted`
- 每个 `(response_id, stream_id)` 具有 generation；stop/tombstone 先推进 generation，再由播放线程 flush，flush 完成后才发送独立 `onPlaybackStopped`
- TS ledger 丢弃旧 generation、重复 completion 和 tombstone 后迟到 completion

## 缓冲与隐私

- 原生播放队列有界为 4 秒 PCM，满时返回稳定错误 `playback_buffer_full`
- TS 上行恢复缓冲最多保留 3 秒 PCM，保留原始 capture sequence；传输层接入后必须复用该缓冲，不能再创建第二份 3 秒队列
- 模块不持久化 PCM，不记录 PCM/Base64/逐字内容

## 仍需真机证据

- Android 原生编译与 New Architecture 装配
- AEC 在目标设备上的实际效果，而不只是 `AcousticEchoCanceler` 创建/启用结果
- 有线耳机、听筒、扬声器、蓝牙路由行为；当前只观察系统输出设备，不强制承诺路由
- 真实播放头、延迟和 stop 延迟；Jest/Kotlin helper 测试不能替代设备证据
