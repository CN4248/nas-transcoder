# TEST_REPORT — 验证记录（NAS Transcode Studio）

> 每次验证都必须追加一条记录，使用 `project_test`。记录 = 时间 + 步骤 + 方法/命令 + 结果 + 证据。
> 没有证据的"通过"视为无效记录。

## 验证规范

- 单元测试、集成测试、回测、数据校验分别记录，不要合并成一条含糊的结论。
- 证据写：命令、输出摘要、指标数字（收益、夏普、最大回撤、命中率等）。
- 失败也要记录，并写清失败现象与下一步；失败记录不得删除。
- 一个步骤完成前，其最近一条记录必须是"通过"。

<!-- project-flow:begin tests -->
_由 project-flow 维护 · 更新于 2026-10-30 11:54_

| 编号 | 时间 | 步骤 | 验证内容 | 结果 |
| --- | --- | --- | --- | --- |
| T1 | 2026-10-26 11:25 | S1 | 安全基础层、命名冲突与 SQLite 持久化 | 通过 |
| T2 | 2026-10-26 11:26 | S2 | 五类真实转码、AVIF/RAR、源文件不变 | 通过 |
| T3 | 2026-10-26 11:26 | S3 | API、批量撤销、暂停/继续/终止、重启持久化 | 通过 |
| T4 | 2026-10-26 11:26 | S4 | 1440×900 真实浏览器 UI 全流程 | 通过 |
| T5 | 2026-10-26 11:26 | S5 | Compose 约束与干净 Alpine 3.20 rootfs 等价验证 | 通过 |
| T6 | 2026-10-26 11:26 | S6 | 本地与 Alpine 全量回归 29/29 | 通过 |
| T7 | 2026-10-26 16:27 | S5 | 构建提速：移除 Dockerfile frontend 镜像拉取并默认国内 apk 源 | 通过 |
| T8 | 2026-10-26 16:45 | S3 | 一键清理已完成记录与批量重试失败任务 | 通过 |
| T9 | 2026-10-30 09:15 | S6 | 重启后引擎探测失败恢复、自动重试与手动重新探测 | 通过 |
| T10 | 2026-10-30 09:32 | S6 | GitHub 发布包：数据清洗、MIT 许可证与解压后回归 | 通过 |
| T11 | 2026-10-30 11:54 | S6 | 文件浏览器与导出目录选择器支持直接输入路径跳转 | 通过 |
<!-- project-flow:end tests -->

## 明细

### T1 · 安全基础层、命名冲突与 SQLite 持久化 — 通过（2026-10-26 11:25）

- 步骤：S1
- 方法/命令：`python3 -m unittest tests.test_core -v`
- 证据：10 tests OK in 0.44s：/etc 穿越、源符号链接逃逸、设备文件、/output 外写入、output 符号链接逃逸均被拒绝；movie.tar.gz 冲突序列为 movie.tar.gz / movie (1).tar.gz / movie (2).tar.gz；源指纹在字节变化后变化；SQLite 重开后设置/job/event/action 全部保留。
- 备注：路径白名单与原子发布是不变量，后续任何改动不得弱化这些断言。

### T2 · 五类真实转码、AVIF/RAR、源文件不变 — 通过（2026-10-26 11:26）

- 步骤：S2
- 方法/命令：`chroot /tmp/alpine-clean /bin/sh -c 'cd /app && python3 -m unittest tests.test_transcode -v'`
- 证据：Alpine 3.20 + ffmpeg 6.1.1 + ImageMagick 7.1.1 + 7-Zip 23.01 + bsdtar 3.8.3：6 tests OK（5.18~5.4s）。真实生成并验证 M4A/MP4/WebP/7Z/TAR.GZ/VTT/AVIF/GIF/MP3，RAR5→ZIP 与 AVIF→JPG 通过；ffprobe 读取输出流，7z t / tarfile / zipfile 校验归档；源目录 tree_snapshot（size/mtime_ns/SHA-256）前后完全一致。
- 备注：RAR 仅输入；Alpine ImageMagick 无 AVIF 解码，AVIF 输入走 ffmpeg 图像模式，已验证。

### T3 · API、批量撤销、暂停/继续/终止、重启持久化 — 通过（2026-10-26 11:26）

- 步骤：S3
- 方法/命令：`python3 -m unittest tests.test_api tests.test_queue tests.test_persistence -v`
- 证据：13 tests OK：文件浏览/筛选/输出建目录与撤销；设置修改+撤销；批量建单+整批撤销；非法 /etc、源内输出、源符号链接、下载源文件全部拒绝；队列全局暂停时进度快速冻结（终态差 ≤1.5%），继续后完成；全局终止后 2 个任务均为 canceled 且 queue_paused=true；取消运行任务后进程终止、临时目录消失；重启后 queued 任务、并发=4、命名模板、日志仍存在，stale tmp 被清理，完成后再次重启输出仍可读。
- 备注：刷新页面依赖 /api/state 轮询；容器重启时运行中任务会按设计标记失败，排队任务保留可重试。

### T4 · 1440×900 真实浏览器 UI 全流程 — 通过（2026-10-26 11:26）

- 步骤：S4
- 方法/命令：`python3 /tmp/cdp_final.py（Chromium headless CDP，1440×900）`
- 证据：innerWidth/innerHeight/scrollWidth/scrollHeight = 1440/900/1440/900（无横向溢出）；console errors=0；跨目录选中 4 个文件；目标默认为 m4a/mkv/vtt/7z；提交后 4 个任务全部 completed；Page.reload 后 4 张任务卡与 19 条日志仍在；设置并发 2→3 保存后撤销回 2；目录 exports 新建后撤销消失；格式矩阵/操作历史/浅色主题截图均无遮挡。截图：docs/screenshots/final-03/04/05/06/07/08/09/10。
- 备注：UI 使用轮询；刷新只重置当前浏览目录，不回退任务/日志/设置。

### T5 · Compose 约束与干净 Alpine 3.20 rootfs 等价验证 — 通过（2026-10-26 11:26）

- 步骤：S5
- 方法/命令：`docker compose config --quiet；干净 Alpine minirootfs 执行 Dockerfile 的 apk 包集合并运行 --check`
- 证据：compose rc=0：ports 5124:5124、network_mode=bridge、/:/host:ro、read_only=true、cap_drop ALL、/output 与 /data 可写挂载。干净 Alpine 3.20 rootfs 按 Dockerfile 包集合安装 python3/ffmpeg/imagemagick/7zip/libarchive-tools/ca-certificates/tzdata：138 秒完成，187 MiB，148 包；--check 探测 5/5 工具（ffmpeg 6.1.1、ffprobe 6.1.1、ImageMagick 7.1.1、7-Zip 23.01、bsdtar 3.8.3），RAR5 列表/AVIF 探测正常。镜像内 29 项测试全绿（见 T6）。
- 备注：实际 `docker build` 未能在本沙箱执行：嵌套 Docker 的 daemon 被内核禁止 unshare，镜像层注册报 `unshare: operation not permitted`；因此用同版本 Alpine rootfs + 完全相同的包集合和运行命令做等价验证。真机首次构建请执行 `docker compose up -d --build`，并保留 compose config 证据。

### T6 · 本地与 Alpine 全量回归 29/29 — 通过（2026-10-26 11:26）

- 步骤：S6
- 方法/命令：`python3 -m unittest discover -s tests -t . -v；chroot /tmp/alpine-clean ... python3 -m unittest discover -s tests -t . -v`
- 证据：本地：Ran 29 tests in 58.100s，OK（docs/test-logs/local-final.log，29 条 test_ ... ok）。干净 Alpine 3.20 rootfs：Ran 29 tests in 67.489s，OK（docs/test-logs/alpine-final.log，29 条 test_ ... ok）。覆盖安全路径、五类真实转码、RAR5、AVIF 输入/输出、原子不覆盖、归档穿越/符号链接、暂停/继续/取消/终止、撤销恢复、重启持久化、UI 1440×900。所有源目录快照断言一致。
- 备注：唯一环境限制是实际 docker build 需在 NAS/有 Docker 能力的主机执行；应用代码与镜像包集合已在同版本 Alpine rootfs 完整验证。

### T7 · 构建提速：移除 Dockerfile frontend 镜像拉取并默认国内 apk 源 — 通过（2026-10-26 16:27）

- 步骤：S5
- 方法/命令：`grep -n '^# syntax' Dockerfile; docker compose config`
- 证据：真实构建日志显示 `resolve image config for docker-image://docker.io/docker/dockerfile:1` 耗时 119.4s，占前 129.8s 的绝大部分。已删除 Dockerfile 首行 `# syntax=docker/dockerfile:1`，BuildKit 改用内置 frontend，不再拉取该镜像；Dockerfile 与 compose 的 APK_MIRROR 默认值均为 `https://mirrors.aliyun.com/alpine`，compose config rc=0 且显示该参数。
- 备注：未在本沙箱实际重建（嵌套 Docker unshare 限制）；用户 NAS 上重新 `docker compose up -d --build` 后应跳过 119s 的 frontend 解析。若基础 alpine 拉取仍慢，可设置 BASE_IMAGE。

### T8 · 一键清理已完成记录与批量重试失败任务 — 通过（2026-10-26 16:45）

- 步骤：S3
- 方法/命令：`python3 -m unittest tests.test_bulk -v；Chromium CDP 1440×900 点击验证`
- 证据：30 项全量测试通过（本地 61.929s、干净 Alpine 67.693s）。新增 test_bulk：2 个真实完成任务的输出文件在清理记录后仍存在；清理动作单条可撤销并恢复 2 条记录；1 个失败任务批量重试后 status=queued/progress=0/error=null。UI 实测：清理已完成与重试失败按钮在 1440×900 下可见（宽 95px/84px，scrollWidth=1440，无横向溢出），确认弹窗正常，点击清理后 completed 5→0、输出文件全部保留，撤销后恢复 5 条；点击重试后 failed 1→0、queued 0→1；console errors=0；截图 docs/screenshots/final-11-bulk-actions.png。
- 备注：清理只删任务记录、不删输出；重试只针对 failed，不包含 canceled。清空记录会写一条 clear_completed 撤销动作，可整批恢复记录。

### T9 · 重启后引擎探测失败恢复、自动重试与手动重新探测 — 通过（2026-10-30 09:15）

- 步骤：S6
- 方法/命令：`python3 -m unittest discover -s tests -v; python3 /tmp/cdp_settings.py; simulated ffmpeg wrapper test`
- 证据：1) 用包装脚本模拟 ffmpeg 冷启动：首次 -version 睡 9s（超过 8s 探测超时）后失败，第二次成功。Capabilities 自动重试后 elapsed=9.13s，ffmpeg.available=True、version=7.1.5，矩阵 mp3 目标包含 mp3/m4a/aac/flac。2) 缺失二进制测试：ffmpeg.available=False，error='rc=127: [Errno 2] No such file or directory'，ffprobe.error='ffmpeg 不可用，未执行 ffprobe 探测'，矩阵不谎报。3) `POST /api/capabilities/refresh` 在本地真实环境返回 5/5 工具可用；`test_01b_capabilities_refresh` 通过。4) 全量 `python3 -m unittest discover -s tests -v`：Ran 31 tests in 51.479s，OK。5) 设置页新增「重新探测引擎」按钮，1440×900 下 footer scrollWidth/clientWidth=858/858 无溢出，点击后引擎 5/5 就绪并出现成功 toast。6) `node --check app/static/app.js`、`python3 -m py_compile app/*.py tests/*.py`、`docker compose config --quiet` 均通过。
- 备注：根因确认：容器内 ffmpeg/ffprobe/magick 二进制和动态库实际正常，重启后应用启动阶段探测失败并缓存了“不可用”。本次改为探测失败自动重试 1 次（第二次 20s 超时）、错误详情写入 Tool.error 与容器日志，并新增设置页手动重新探测（有运行中任务时返回 409）。需要重建镜像后生效；重建前可先 docker compose restart 应急。

### T10 · GitHub 发布包：数据清洗、MIT 许可证与解压后回归 — 通过（2026-10-30 09:32）

- 步骤：S6
- 方法/命令：`python3 -m py_compile app/*.py tests/*.py; node --check app/static/app.js; docker compose config --quiet; python3 -m unittest discover -s tests -t .; unzip -q nas-transcoder.zip -d /tmp/nts-zip-verify && cd /tmp/nts-zip-verify/nas-transcoder && python3 -m unittest discover -s tests -t .`
- 证据：在干净 staging 目录解压包内全量测试：Ran 31 tests in 56.557s，OK；`py_compile`、`node --check`、`docker compose config --quiet` 均通过。发布包 `nas-transcoder.zip`：43 个文件、2,205,946 字节，顶层唯一目录 `nas-transcoder/`，包含 MIT `LICENSE`、README、Dockerfile、compose、.env.example、app、tests、docs/screenshots；`data/`、`output/`、`.env`、`__pycache__`、`.project-flow`、AGENTS/STRATEGY/TEST_REPORT/HANDOFF 均为 0 命中；`unzip -t` 通过；重命名后重新解压仍可编译/测配置。SHA-256 `4de2154ef9b75843b3f2aea266e9bba426ab383d55b6500b80906f8adf6b7226`。
- 备注：发布包位于 /workspace/release/nas-transcoder.zip；已同步更新 /workspace/release 清单与 SHA256SUMS.txt。旧 nas-transcoder.zip 备份在 /tmp/nas-transcoder-old.zip。发布包不包含本机的 data/app.db、output、测试日志和项目内部文档。

### T11 · 文件浏览器与导出目录选择器支持直接输入路径跳转 — 通过（2026-10-30 11:54）

- 步骤：S6
- 方法/命令：`node --check app/static/app.js && python3 -m py_compile app/*.py && docker compose config --quiet && python3 -m unittest discover -s tests -t . -v；Chromium CDP 实测路径输入`
- 证据：静态检查全部通过；unittest 31 项 66.970s，结果 OK。CDP 实测：源目录根输入 `/home/user` 后 pathInput 回显 `/home/user`，面包屑为 `NAS 根目录 / home / user`，文件列表显示 `nas-song.mp3`；输入 `/home/not-exist` 后提示「路径不存在」并自动恢复到 `/home/user`；导出目录选择器输入 `/tmp/nts-demo/out/jump-target` 后 pickerCurrent 同步为同一路径。截图 docs/screenshots/final-13-path-jump.png、final-14-path-jump-error.png、final-15-picker-path.png。
- 备注：源路径按 `/host` 挂载根做映射，NAS 绝对路径直接可用；导出路径按 `/output` 根映射；前端只做便利映射，最终仍由服务端 security 层校验真实路径。
