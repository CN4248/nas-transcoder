# AGENTS — 开发约束（NAS Transcode Studio）

> 本文件约束**所有**参与本项目的对话与子代理。开工前必读；与用户任务冲突时以用户明确要求为准。

## 1. 环境与技术栈

- 目标机：绿联 DXP4800 Plus（x86_64）UGOS Pro，Docker 24+ / Compose v2。
- 镜像：`alpine:3.20` + `python3` + `ffmpeg` + `imagemagick` + `7zip` + `libarchive-tools`（bsdtar，负责 RAR4/RAR5 解压），运行用户默认 root（确保能读 NAS 全部文件）。
- 后端：**仅 Python 3 标准库**（`http.server`、`sqlite3`、`subprocess`、`threading`），禁止 pip 依赖。
- 前端：原生 HTML/CSS/ES2020，无 CDN、无构建步骤、无框架。
- 存储：SQLite `/data/app.db`；临时文件 `/output/.transcoder-tmp`；回收站 `/output/.transcoder-trash`。
- 测试：`python3 -m unittest discover -s tests -t . -v`（31 项）；容器冒烟 `sh tests/container_smoke.sh`。

## 2. 目录与文件约定

- 源码：`app/`（`server.py` 入口；`config.py` / `db.py` / `security.py` / `capabilities.py` / `engine.py` / `jobs.py`）。
- 前端：`app/static/index.html`、`app.js`、`style.css`。
- 测试：`tests/`（`test_*.py`、`helpers.py`、`fixtures/`、`container_smoke.sh`）。
- 部署：`Dockerfile`、`docker-compose.yml`、`.dockerignore`、`.env.example`。
- 文档：根目录 `STRATEGY.md` / `AGENTS.md` / `TEST_REPORT.md` / `HANDOFF.md` / `README.md`，不得移动。
- 截图：`docs/screenshots/`；测试临时目录一律在 `/tmp`，不入库。

## 3. 代码质量门禁

每次改动后至少运行：

```bash
python3 -m py_compile app/*.py
node --check app/static/app.js
python3 -m unittest discover -s tests -t . -v
docker compose config --quiet
```

改动 Dockerfile 后必须在有 Docker 能力的环境执行 `docker build` 并记录耗时/体积；本开发沙箱内嵌套 Docker 被内核禁止 `unshare`，只能用 Alpine 3.20 rootfs chroot 做等价依赖/运行验证，并在 TEST_REPORT 标注该限制。改动 UI 后必须在 1440×900 截图检查（无横向滚动、日志面板可见、弹窗按钮可点）。

## 禁止事项（安全红线，不可协商）

- **绝不写 `/host`**：任何写 API 必须先过 `safe_output_path()`；转码只以只读打开源。
- **绝不覆盖已有文件**：不得使用 `open(dest,"w")`、`shutil.copy*` 到目标、`os.replace` 到已存在路径；发布必须是 `link()`/独占创建，冲突自动加序号。
- **绝不删除源文件**：删除/撤销仅允许操作 `/output` 内本应用生成的文件；删除任务记录不得删除源。
- 禁止 `shell=True`、`os.system`、字符串拼接命令；所有子进程参数必须是 list。
- 禁止在容器挂载 `docker.sock`、`/dev`、`--privileged`；compose 必须保留 `cap_drop: ALL`、`no-new-privileges`、`read_only: true`、`/host` 只读。
- 归档必须做路径穿越/符号链接/炸弹校验；禁止直接 `tarfile.extractall` 到不受控路径。
- 禁止把源路径、输出路径直接拼进 JavaScript `innerHTML`（用 `textContent`）。
- 禁止在 API 中回显堆栈或环境变量秘密；错误给用户可读信息，详细信息写日志。

## 5. 验证要求

- 每次验证调用 `project_test`，证据必须含命令与输出摘要（测试数量、通过数、耗时、关键指标）。
- pass 步骤完成前，其最近一条验证必须是 pass。
- 真实转码测试必须覆盖音频/视频/图片/压缩包，且断言：
  - 源文件 SHA-256、mtime_ns、目录文件列表不变；
  - 输出可被对应工具读取；
  - 目标已存在时原内容不变；
  - 撤销后输出进入 trash，源仍不变。
- 容器测试必须验证 `/host` 只读（`touch /host/...` 失败）及 Web 健康接口。
- 不允许为了变绿删除/弱化测试。

## 6. 文档维护义务

- 功能、API、默认值改动同步 README 与 STRATEGY.md。
- TEST_REPORT.md 只追加，不删历史。
- 交接流程遵循 project-flow：`project_check(fix=true)` → `project_handoff`。

## 交接协议

- 触发：上下文 high/critical、用户要求停止/交接、或步骤全部完成。
- 顺序：`project_check(fix=false)` → 修正过时文档 → `project_check(fix=true)` → `project_handoff`。
- 交接文档必须包含：一句话现状、完成项、关键决策、验证证据、阻塞、下一步与验收标准、常用命令。
- 交接后不得继续修改代码，除非用户明确要求续接。
