# 本次离线验证记录

验证日期：2026-10-09。开发分支：`codex/g1-console-pick-runtime`，起点 `7077b3e`。没有连接机器人、相机或车辆，没有发送任何实机控制命令。

| 验证项 | 结果 |
| --- | --- |
| Windows / Python 3.13.5：新增 G1 集成测试 | **63 passed，2 skipped**；跳过 Linux Node 安装和整个原生运动学模块（内含 3 项） |
| Ubuntu 22.04 WSL / Python 3.11.13：新增 G1 集成测试 | **67 passed，无跳过** |
| Linux 新增包语句覆盖率 | **81%**（2173 语句）；runtime 89%、kinematics 90%、store 94% |
| Linux 现有 Core 全量回归 | **473 passed，13 subtests passed**；1 条第三方 Pydantic warning |
| Windows 现有 Core 全量回归 | **446 passed，27 failed，13 subtests passed** |
| Windows 修改前 Core 基线 | 414 passed，58 failed；最终失败集合没有新增，减少 31 项失败 |
| Ruff / 格式 / git diff 空白检查 | 通过 |
| 模拟按键→抓取→放置演示 | 通过，无模型服务与硬件依赖 |
| Skill 激活→AgentTask 绑定→Query→Action→结果→finalize | 使用真实 Core 类和本地 Gateway 通过 |
| 三路 WebSocket 图像/状态 | 真实本地 uvicorn 与 Core ObservationCollector 通过 |
| Skill / Node / 静态索引 | Core 归档检查、安装、SHA-256 锁、重复构建字节一致性通过 |
| Linux Node 安装后的入口 | 实际执行打包入口 `demo` 通过 |
| Python wheel | 成功构建 |

Windows 残余 Core 失败涉及 POSIX executable/chmod 和路径假设、符号链接权限、打开中的 SQLite 文件不能 unlink，以及当前平台/provider 环境相关测试。没有为压低失败数而改动无关 Core 功能；Linux 同一源码全量通过。此次仅修改 Core 的 Skill 字节读取与 frontmatter 换行处理，修复 CRLF 导致的激活摘要与绑定摘要不一致，并补了回归测试。

原生运动学使用 **Pinocchio 3.9.0、coal 3.0.2、eigenpy 3.12.0、NumPy 2.3.5**。测试在合成 29 关节 URDF 上检查：映射、FK/IK、另一只手保持不动、重力/负载挂载与释放、多次抓放不累积失效碰撞体、双手负载索引、环境碰撞以及指尖接触区域例外。它验证实际原生 API 和本地算法，不验证用户 G1 的模型或增益。

关键故障验证包括：重复 caller 幂等、场景/观测过期、反馈过期/来源 epoch 改变、无触发反馈、取消、手指保持、看门狗保持位置锁定、扭矩/关节范围/IMU 漂移、未知结果保留控制权、抓取/放置占用、重启不重放、数据库写失败、独占锁、维修记录、相机标定与合成投影。

覆盖率并非完整性证明。SDK 设备初始化、RealSense 真实视频流、DDS 网络时序与消息丢失、手指力学、车内空间、接触力、真实闭环成功率仍未实机验证；没有运行物理仿真器，没有完成 aarch64 或 Python 3.12 的本地验收。CI 提供 Linux Python 3.11/3.12 作业，但本次没有推送或声称远端 CI 已运行。具体接机输入见 [COMMISSIONING.md](COMMISSIONING.md)。

本地完整报告位于忽略的 `dist/`：`g1-windows-tests.xml`、`g1-linux-tests.xml`、`g1-linux-coverage.xml`、`core-final.xml`、`core-linux.xml` 及对应日志。交付构建位于 `dist/g1-tabletop-delivery/`，含 Skill、Linux x86_64 Node、wheel、`index.yaml`。这些产物和虚拟环境未加入 Git；从源码按 README 可重建。

复现 Linux 测试时设置 `LITELLM_LOCAL_MODEL_COST_MAP=True`，使用 LiteLLM 自带价格表，避免无关网络下载影响测试收集。WSL 中依赖下载受环境网络影响，本次通过 Windows 下载 Linux wheel，再在隔离 Linux 环境离线安装；未修改系统 Python。
