# 现场配置与接机顺序

完整 Linux 安装、驱动部署、启动停机和恢复命令见 [部署与接机手册](DEPLOYMENT_LINUX.zh-CN.md)；现场数据与验收结果请填写 [记录模板](SITE_ACCEPTANCE.zh-CN.md)。本页保留坐标约定与标定摘要。

代码可离线验证；下列测量必须在硬件、面板安装和作业布局确定后完成。`config/mock.json` 只用于模拟，不能修改 `mode` 后直接用于实机。

## 坐标、单位和手型

全系统长度 m、角度 rad，`Pose.quaternion_xyzw` 为单位四元数，变换 `A_to_B` 表示 B 坐标在 A 中的位姿：`T_base_target = T_base_camera @ T_camera_marker @ T_marker_target`。G1 base 为固定基座 URDF 根节点；不是世界地图。IMU 单独使用 Unitree 原生 **wxyz** 顺序。

`hardware.reference_body_q` 顺序是 `drivers/unitree.py` 的前 15 个关节（左右腿各 6、腰 3），增益/限位的 14 个值为左臂 7 后右臂 7。BrainCo q 的六个通道遵循现场 Revo2/服务定义，范围 [0,1]；先逐通道确认方向再设置 `point/open/pinch`。不能从 mock 数值推断真实手型。

每个手型的 `tolerance` 是六通道位置误差上限，应结合手指重复定位误差和按钮可用面积标定；关节读数接近目标不自动等于指尖达到毫米级精度。URDF 必须恰有 29 个单自由度关节；手指/支架采用固定连杆或保守球包络，不能直接输入另含可动手指关节或 floating base 的模型。

按键 `pose_in_panel` 为指尖接触时所需 TCP 位姿，TCP +Z 与 `press_axis_panel` 同向；`half_extent_m` 为按钮实际可用区域的半宽/半高，扣除相邻按键、圆角和边缘。`travel_m` 为从接触面沿法向的受限压入量。只有指尖球与目标面板在该区域和行程内能获得接触碰撞例外，手掌/相机/其他部位不豁免。

对象 `marker_to_target` 定义期望抓持中心/TCP 姿态；`approach_axis` 当前在 **base 坐标系**解释（不是物体局部轴），抬升固定沿 base +Z。物体验证用同一目标中心，负载球以 grasp TCP 为中心，质量/惯量也是近似；不适合偏心长杆或大物体。放置区 `marker_to_target` 同样给出放置后的目标中心/TCP 位姿。

## 标记和相机

```bash
python -m g1_tabletop marker --id 0 --pixels 600 --output dist/panel-marker.png
python -m g1_tabletop marker --id 1 --pixels 600 --output dist/object-marker.png
python -m g1_tabletop marker --id 2 --pixels 600 --output dist/placement-marker.png
```

使用 DICT_4X4_50，ID 不得重复。打印后实际测量黑色正方形边长填 `size_m`，不要把白边计入；输出像素数不代表物理尺寸。面板标记和按钮位置刚性关联；不能把标记贴在会动的按键上。物体与放置区分别有自己的标记。

三相机以 serial 选择。头部先观察面板/物体，靠近后使用对应腕 D405 修正，修正幅度超限或标记丢失即拒绝动作。D405 近距离视野、指尖遮挡与标记布局需在预接触位置实测。深度已采集/对齐，本版定位主链采用已知尺寸标记 PnP，不把单个反光深度像素当按钮位置。

离线标定命令：

```bash
python -m g1_tabletop calibrate --mount wrist --input left-pairs.json --output left-wrist-camera.json
python -m g1_tabletop calibrate --mount base --input head-pairs.json --output base-head-camera.json
```

输入统一形状：

```json
{"pairs": [{"base_to_mount": {"position": [0,0,0], "quaternion_xyzw": [0,0,0,1]}, "camera_to_target": {"position": [0,0,0], "quaternion_xyzw": [0,0,0,1]}}]}
```

上面只展示格式，不是可用测量数据。wrist 模式至少 8 对：`base_to_mount` 是采图时 base→wrist FK，`camera_to_target` 是同一静止标定板的相机观测，要求围绕至少两个轴有足够转动。base 模式至少 3 对：`base_to_mount` 字段在此模式表示**已测量的 base→标定板**，输出 base→camera。输出 Pose 填对应 `parent_to_camera`。命令只计算，不采图、不移动机器人。配对数据由现场同步采集与标定流程提供。

算法残差阈值只是筛除明显错误；应另用未参与拟合的数据测量 TCP 最终误差。小按键的误差预算可能需要比命令默认残差门限更严格。

## 分阶段验收

1. 确认 BrainCo 型号、固件与服务一致；独立验证物理急停、通信中断、进程崩溃和 `arm_sdk` 控制权交接后的行为。配置腿腰参考、IMU、基座安装和环境碰撞体。清除其他控制发布者。
2. 验证三路视频模式/曝光时间/序列号、头部外参、两侧手眼外参、point/grasp TCP、手指与相机包络。对 URDF/SRDF 计算 SHA-256，模型必须与当前 29-DOF 实机吻合。
3. 不接触物体做低速单臂姿态和路径检查；确认左右臂映射、重力项、增益和关节限制。测试计划被障碍阻挡时能够拒绝，实际基座姿态漂移时能够停止。
4. 在替代试验按键上先验证指尖尺寸、柔顺端头、接触行程与反馈消抖。没有测得容差前，不直接在狭窄中控按键上试默认参数。独立测量接触力；电机电流不能换算为可靠指尖力。
5. 对真实面板逐键配置，先一个键，记录按对键率、触发率、误触相邻键、失败回撤、状态延迟。断开反馈后必须得到失败/unknown，不能因动作走完而成功。确认 `ensure_state` 不重复切换已满足状态。
6. 小型刚性物体：预抓取、空抓、滑落、遮挡、抬升后持有确认、放置区验证。测试抓取中取消时手和臂都能停，重启后持物信息不丢失。
7. 分别验证左右臂，再验证串行跨臂任务及一手持物时另一手工作时的碰撞模型。需要同时双臂协作时另增协调控制，不绕过当前仲裁。

建议每个目标至少进行多组不同光照/轻微位置偏差下的重复试验，保存原始图像、源反馈、执行日志及配置摘要。成功率与接触力/最大偏差应根据你的验收指标设定；本仓库不预填“95%/99%”或“毫米级已达成”。

实机启用前将 `null` 字段填全，经独立检查后声明 `commissioned=true`，运行 `doctor`，再在现场有停机能力的条件下启动 real profile。无真机期间不要把该声明设为 true 来绕过配置检查。
