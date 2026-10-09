# 按键反馈协议

实机后端对 `hardware.feedback_url + /buttons/{feedback_key}` 发起只读 GET。返回原始 JSON：

```json
{
  "button_id": "button_01",
  "event_count": 7,
  "state": true,
  "timestamp": 1791500000.123,
  "source": "console-testbench-v1",
  "epoch": "producer-boot-unique-id"
}
```

- `button_id` 等于对应按键配置中的 `feedback_key`。
- `source` 等于现场固定的 `hardware.feedback_source`。
- `event_count` 为生产者确认的物理按下事件累计数，同一 epoch 单调递增，按下/弹起不能各计一次。电气边沿应先消抖。
- `state` 是功能实际状态，例如 A/C 当前开/关；瞬时按键或不可观测时使用 `null`，此时不可使用 `ensure_state`。
- `timestamp` 为**源测量**的 Unix 秒数；源可以持续采样并更新时间，不得由 HTTP 桥刷新旧数据时间。
- `epoch` 在采集进程重启、计数清零或设备替换时改变。累计数不能通过机器人下发目标或“手指已到达”来增加。

运行时要求事件前后 source/epoch 相同、计数正好新增一次，且数据新鲜。两个事件、计数回退、来源变更、超时、数据过期均不能判成功。现场应隔离其他人同时操作同一按键；否则单靠事件计数不能区分事件由谁触发。

推荐优先使用试验台电气输入或已授权的车辆状态接口。若用指示灯视觉，需自行完成灯区标定、亮灭阈值/迟滞、反光抑制、消抖和时间戳采集；没有这些数据，仓库不会猜测照片中的灯态。功能状态只在变化时有边沿的按钮，重复 `press_once` 可能无法产生可归因事件；用 `ensure_state` 或独立按键事件通道。

已提供可运行的文件到 HTTP 适配器：

```bash
python -m g1_tabletop feedback-bridge --source-file /absolute/path/console-feedback.json --port 19003
```

文件格式是 `{ "button_01": {上述完整记录}, "button_02": {...} }`。现场采集进程写入同目录临时文件，再原子替换源文件。服务没有“按下成功”写接口；不存在目标返回 404，记录损坏返回 503，原有时间戳原样返回。不要把静态示例 JSON 当成实机反馈。
