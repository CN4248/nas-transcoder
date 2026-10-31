# HANDOFF — 第 1 次交接（2026-10-26 11:28）

> 本文件是给**新对话**的启动包。读完后按第 9 节执行，不要凭猜测继续。

## 0. 一句话现状

交付完成：端口 5124、bridge、/:/host:ro 的单容器 NAS 转码工作台；只读源 + 原子新文件 + 持久队列/日志/设置/撤销 + 现代 WebUI。本地与干净 Alpine 3.20 rootfs 各 29 项测试全绿；唯一未在沙箱执行的是实际 docker build（嵌套 Docker 禁止 unshare），已用同版本 rootfs/同包集合/同启动命令做等价验证。

## 1. 项目与约束入口

- 项目根目录：`/workspace/docker/nas-transcoder`
- 规则与方向：`STRATEGY.md`
- 开发约束：`AGENTS.md`（**先读，优先级高于任何任务描述**）
- 验证记录：`TEST_REPORT.md`
- 项目状态文件：`.project-flow/state.json`

## 2. 当前阶段与进度

阶段：已交付
方向：A. 轻量单体：Alpine + Python 标准库 + ffmpeg/ImageMagick/7-Zip + 原生 WebUI
步骤：6/6 完成
验证记录：6 条
交接次数：1
步骤明细：
  - S1 安全基础层与数据模型 [已完成]
  - S2 格式能力探测与转码引擎 [已完成]
  - S3 持久任务队列与 REST API [已完成]
  - S4 现代 WebUI [已完成]
  - S5 Docker 构建与部署文档 [已完成]
  - S6 全链路回归与修复 [已完成]

## 3. 本次会话完成的工作

- 后端：Python 标准库 HTTP + SQLite，任务队列、暂停/继续/取消/终止、并发、重启恢复、SSE 轮询状态
- 安全：/host 只读 + realpath 白名单 + O_RDONLY + 源指纹 + 临时文件 + link 原子发布 + 冲突自动加序号，绝不覆盖
- 转码：动态探测 ffmpeg/ImageMagick/7-Zip/bsdtar；音频/视频/图片/压缩包/字幕；RAR4/RAR5 输入、AVIF 输入/输出、视频提取音频/生成 GIF
- WebUI：文件浏览/跨目录多选/批量配置/输出目录与新建/逐文件命名/队列/日志/设置/格式矩阵/撤销历史/深浅主题，1440×900 检查通过
- Docker：Alpine 3.20、ffmpeg/ImageMagick/7zip/libarchive-tools、healthcheck、read_only、cap_drop、5124 映射；compose config 通过
- 文档与证据：README、STRATEGY、AGENTS、TEST_REPORT、8 张最终截图、本地/Alpine 测试日志

## 4. 关键决策与理由

- 单容器 Alpine + Python 标准库 + 原生 WebUI，零 pip/npm 编译，构建快且离线可运行
- 源文件只读不是“约定”而是四层保护：ro 挂载、realpath 白名单、O_RDONLY、源指纹复核；所有写路径强制 /output
- 输出通过临时目录 + link() 原子发布，目标存在自动 (n)，绝不用 replace 覆盖已有文件
- RAR 仅作为输入（专有编码器不装入镜像），RAR4/RAR5 用 bsdtar 解压；UI 不显示 RAR 输出
- AVIF 输出在 ImageMagick 无编码器时走 ffmpeg；Alpine 的 AVIF 输入也走 ffmpeg 图像模式
- 队列/设置/日志/撤销全部落 SQLite；运行中断任务重启后标记失败可重试，排队任务保留
- 撤销只移动/恢复 /output 内应用生成的输出到 .transcoder-trash，源目录永不参与

## 5. 验证结果（最近）

- T6 2026-10-26 11:26 · S6 · 本地与 Alpine 全量回归 29/29 → 通过（证据：本地：Ran 29 tests in 58.100s，OK（docs/test-logs/local-final.log，29 条 test_ ... ok）。干净 Alpine 3.20 rootfs：Ran 29 tests in 67.489s，OK（docs/test-logs/alpine-final.log，29 条 test_ ... ok）。覆盖安全路径、五类真实转码、RAR5、AVIF 输入/输出、原子不覆盖、归档穿越/符号链接、暂停/继续/取消/终止、撤销恢复、重启持久化、UI 1440×900。所有源目录快照断言一致。）
- T5 2026-10-26 11:26 · S5 · Compose 约束与干净 Alpine 3.20 rootfs 等价验证 → 通过（证据：compose rc=0：ports 5124:5124、network_mode=bridge、/:/host:ro、read_only=true、cap_drop ALL、/output 与 /data 可写挂载。干净 Alpine 3.20 rootfs 按 Dockerfile 包集合安装 python3/ffmpeg/imagemagick/7zip/libarchive-tools/ca-certificates/tzdata：138 秒完成，187 MiB，148 包；--check 探测 5/5 工具（ffmpeg 6.1.1、ffprobe 6.1.1、ImageMagick 7.1.1、7-Zip 23.01、bsdtar 3.8.3），RAR5 列表/AVIF 探测正常。镜像内 29 项测试全绿（见 T6）。）
- T4 2026-10-26 11:26 · S4 · 1440×900 真实浏览器 UI 全流程 → 通过（证据：innerWidth/innerHeight/scrollWidth/scrollHeight = 1440/900/1440/900（无横向溢出）；console errors=0；跨目录选中 4 个文件；目标默认为 m4a/mkv/vtt/7z；提交后 4 个任务全部 completed；Page.reload 后 4 张任务卡与 19 条日志仍在；设置并发 2→3 保存后撤销回 2；目录 exports 新建后撤销消失；格式矩阵/操作历史/浅色主题截图均无遮挡。截图：docs/screenshots/final-03/04/05/06/07/08/09/10。）
- T3 2026-10-26 11:26 · S3 · API、批量撤销、暂停/继续/终止、重启持久化 → 通过（证据：13 tests OK：文件浏览/筛选/输出建目录与撤销；设置修改+撤销；批量建单+整批撤销；非法 /etc、源内输出、源符号链接、下载源文件全部拒绝；队列全局暂停时进度快速冻结（终态差 ≤1.5%），继续后完成；全局终止后 2 个任务均为 canceled 且 queue_paused=true；取消运行任务后进程终止、临时目录消失；重启后 queued 任务、并发=4、命名模板、日志仍存在，stale tmp 被清理，完成后再次重启输出仍可读。）
- T2 2026-10-26 11:26 · S2 · 五类真实转码、AVIF/RAR、源文件不变 → 通过（证据：Alpine 3.20 + ffmpeg 6.1.1 + ImageMagick 7.1.1 + 7-Zip 23.01 + bsdtar 3.8.3：6 tests OK（5.18~5.4s）。真实生成并验证 M4A/MP4/WebP/7Z/TAR.GZ/VTT/AVIF/GIF/MP3，RAR5→ZIP 与 AVIF→JPG 通过；ffprobe 读取输出流，7z t / tarfile / zipfile 校验归档；源目录 tree_snapshot（size/mtime_ns/SHA-256）前后完全一致。）
- T1 2026-10-26 11:25 · S1 · 安全基础层、命名冲突与 SQLite 持久化 → 通过（证据：10 tests OK in 0.44s：/etc 穿越、源符号链接逃逸、设备文件、/output 外写入、output 符号链接逃逸均被拒绝；movie.tar.gz 冲突序列为 movie.tar.gz / movie (1).tar.gz / movie (2).tar.gz；源指纹在字节变化后变化；SQLite 重开后设置/job/event/action 全部保留。）

## 6. 已知问题与阻塞

- 本开发沙箱无法执行实际 docker build / docker run：嵌套 Docker daemon 注册镜像层时被内核禁止 unshare（operation not permitted）。已用干净 Alpine 3.20 rootfs 安装完全相同的包集合并运行全量测试；真机首次构建仍需执行 docker compose up -d --build 并记录耗时/体积。
- WebUI 无登录，默认仅面向可信内网；公网使用需反向代理认证。

## 7. 下一步（按顺序执行）

- 在 NAS 上执行 docker compose up -d --build，记录构建耗时与 docker image ls 体积
- 打开 http://NAS_IP:5124，跑一次音频/视频/图片/压缩包真实转码，确认源文件 hash/mtime 不变
- 运行 sh tests/container_smoke.sh 验证 /host 只读与 /output、/data 可写
- 刷新页面并重启容器一次，确认任务/日志/设置持久化与中断任务恢复语义

## 8. 步骤总表

| 步骤 | 内容 | 状态 |
| --- | --- | --- |
| S1 | 安全基础层与数据模型 | 已完成 |
| S2 | 格式能力探测与转码引擎 | 已完成 |
| S3 | 持久任务队列与 REST API | 已完成 |
| S4 | 现代 WebUI | 已完成 |
| S5 | Docker 构建与部署文档 | 已完成 |
| S6 | 全链路回归与修复 | 已完成 |

## 9. 新对话启动步骤

1. 读 `AGENTS.md`、`STRATEGY.md`、本文件；有需要再读 `TEST_REPORT.md`。
2. 调 `project_resume`（root=`/workspace/docker/nas-transcoder`）获取状态与文档摘要。
3. 找到第一个 `待开始/进行中` 的步骤；调 `project_steps(action=start, stepId=...)`。
4. 只做该步骤范围内的事；每次验证后用 `project_test` 记录。
5. 完成后 `project_steps(action=complete)`；上下文到 high/critical 时立即交接。

## 10. 环境与常用命令

- docker compose config --quiet
- docker compose up -d --build
- docker compose logs -f --tail=200
- python3 -m unittest discover -s tests -t . -v
- sh tests/container_smoke.sh
- python3 -m py_compile app/*.py && node --check app/static/app.js

---
_生成于 2026-10-26 11:28 · 由 project-flow 生成，请勿手工删改小节结构_
