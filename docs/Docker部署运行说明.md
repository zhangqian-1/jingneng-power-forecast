# Docker 部署运行说明

## 1. 源码交接与环境

本项目以完整源码工程交到公司 GitLab。保留 `app/`、`models/`、`tests/`、`docs/`、`examples/`、`Dockerfile`、`requirements.txt`、[compose.yaml](../compose.yaml)、[.env.example](../.env.example) 和 `check_config.py`。模型已经训练好，接收方可以检查、构建和运行，不需要重新训练。

接收方安装并启动 Docker Engine，安装 Docker Compose 2.20+。只用容器部署无需在宿主机另装 Python；直接运行源码检查和测试时使用 Python 3.11，命令见 [README](../README.md)。源码构建需要下载基础镜像和 Python 依赖，不能联网的服务器应使用公司构建机生成的镜像或另行交付的已测试镜像。

镜像架构必须与服务器一致：`x86_64` 对应 `amd64`，`aarch64` 对应 `arm64`。Linux 执行 `uname -m` 查看。以下构建示例使用构建机默认架构，建议在与目标服务器相同架构的机器构建；跨架构构建需由部署方配置 Buildx 并复测。

## 2. 构建与配置

从公司 GitLab 取得完整项目，在源码根目录构建：

```bash
docker build -t jingneng-power-forecast:7station-2025-v1 .
```

将 `compose.yaml` 放在固定部署目录，不要使用临时目录。首次从 `.env.example` 创建同目录的 `.env`：

```bash
# Linux，仅首次执行，不覆盖已有配置
cp -n .env.example .env
```

Windows 可在文件管理器中复制并重命名；文件名必须是 `.env`，不是 `.env.txt`，不要覆盖已有配置。模板中的镜像地址留空，使用上面的构建命令后填写：

```dotenv
POWER_FORECAST_IMAGE=jingneng-power-forecast:7station-2025-v1
```

| 配置项 | 填写内容 |
|---|---|
| `POWER_FORECAST_IMAGE` | 必填，使用本地已构建/导入的标签，或公司镜像仓库地址；从仓库拉取时可用摘要固定版本 |
| `POWER_FORECAST_BIND` | 默认 `127.0.0.1`，只允许服务器本机或同机网关访问 |
| `POWER_FORECAST_PORT` | 默认 `8000`；被占用时换一个空闲端口 |
| `POWER_FORECAST_RUNTIME_DIR` | 默认 `compose.yaml` 同目录下的 `runtime`，可改为专用持久化目录；需可写，不混用旧十站缓存 |

需要其他机器直连时，由部署方配置内网监听地址或 `0.0.0.0`，并限制防火墙白名单。服务没有内置鉴权和 HTTPS，不要直接开放到公网。`.env` 是部署机配置，不提交源码仓库、不装入镜像。

## 3. 启动与检查

取得对应架构的镜像并完成上面的配置后，在 `compose.yaml` 所在目录执行，Linux 和 Windows 均可。这条命令启动已有镜像，不负责构建代码：

```bash
docker compose config --quiet
docker compose up -d --pull never --wait --wait-timeout 300
```

`--pull never` 使用本地已经构建或导入的镜像；缺少镜像时会直接报错。若使用公司镜像仓库，先按公司要求登录该仓库、填写 `.env` 的镜像地址，并执行 `docker compose pull`。账号和 Token 不写入源码、镜像或交接文档。

查看状态、日志和最近结果：

```bash
docker compose ps
docker compose logs --tail 100 forecast
curl "http://127.0.0.1:8000/api/power/forecast/latest"
```

Windows PowerShell 用 `curl.exe`，修改监听地址或端口后同步修改访问地址。`healthy` 表示服务接口可响应，不代表历史数据够了；首次无结果时 GET 返回 `404` 正常。Compose 检查的是现有 `latest` 接口，没有新增 `/health`；状态变为 `unhealthy` 时应查日志，它不会仅因健康检查失败而自动重启。

平台按 [接口交接说明](接口交接说明.md) POST 真实数据。缓存不足时返回 `409` 并继续保存，满足 672 个连续且温湿度可构造的点后预测未来 96 点。

`runtime` 保存历史、状态上下文和最近结果，重建容器不会自动删除它。`unless-stopped` 会重启异常退出的容器，也会随 Docker 引擎启动恢复未被手动停止的容器；已手动停止的需再次启动。一个缓存目录只给一个服务实例使用。

## 4. 检查、测试与验收

**源码检查和真实数据回放：** 按 [README](../README.md) 执行 `check_config.py`、单元测试及滚动预测测试。测试使用独立端口和空缓存，不要向生产服务回放历史测试数据。配好 `.env` 后可先运行 `docker compose config --quiet`，它只检查配置，不能证明容器可以运行。

**公司重新构建后的容器验收：** 使用独立的测试实例和缓存，检查以下项目，再进行正式部署：

1. 镜像架构与服务器一致，Compose 正常启动；GET `latest` 在首次无结果时返回 `404`。
2. 真实历史达到要求前返回 `409`，满足要求后返回 `200` 和未来 96 点；七日测试使用 `tests/run_api_test.py`。
3. 重建容器后保留历史和最近结果，重复提交最后一次请求仍可正常预测。
4. 用另一个空缓存测试实例运行 `tests/run_rolling_accuracy_test.py`，检查 73 天、7008 点评分；与当前离线参考相差超过 0.01 个百分点时，应排查后再发布。

**交付前已有验证记录（2026-09-12）：** 源码提交 `1ea3806a56188be69efd648b39a1b43e7d155d85` 已在 GitHub 完成两种架构的构建、上述测试，以及镜像导出后重新导入的预测验证。后续纯文档更新不改变这些已测试镜像的提交号。

| 服务器架构 | 成功构建记录 | 7008 点 MAPE |
|---|---|---|
| AMD64 / x86_64 | [构建 34687943415](https://github.com/zhangqian-1/jingneng-power-forecast/actions/runs/34687943415) | 10.8778242% |
| ARM64 / aarch64 | [构建 34688006356](https://github.com/zhangqian-1/jingneng-power-forecast/actions/runs/34688006356) | 10.8778241% |

这些链接是开发方验证记录，不是接收方部署的前提；对接时可附带导出的 `release.json` 和评分报告。GitHub 工作流保留在 `.github/workflows/build-image.yml`，上传公司 GitLab 后不会自动执行。本项目目前未配置 `.gitlab-ci.yml`，公司如需自动检查和构建，应按其 Runner、架构和镜像仓库配置流水线。

**目标服务器验收：** 在公司的实际环境核对接口调用、缓存持久化、访问控制、资源占用和响应时间。已有测试通过不代表该服务器已经上线；源码重建的镜像也需要在公司环境复测。

## 5. 可选镜像交付与维护

源码是 GitLab 项目交接的主体，已测试镜像可另作配套制品，交到公司指定的制品库、镜像仓库或发布附件。大镜像文件不要直接提交到普通 Git 源码历史，镜像包也不代替源码项目。

**已收到完整镜像包时：** 包内应有 `image.tar.gz`、`compose.yaml`、`.env`、`release.json` 和 `SHA256SUMS`。可跳过源码构建，解压后在 Linux 执行：

```bash
sha256sum -c SHA256SUMS
docker load -i image.tar.gz
docker compose up -d --pull never --wait --wait-timeout 300
```

保留包内 `.env` 的镜像标签，不改成仓库摘要地址；其他端口和目录按部署环境调整。导入镜像不需要外部镜像仓库，也不带测试历史，首次仍需平台补传真实数据。Windows PowerShell 可用 `Get-FileHash -Algorithm SHA256 image.tar.gz` 核对 `release.json` 中的镜像文件校验值。

如果接收方需要把镜像放进公司 Container Registry，可在导入后按公司提供的地址执行 `docker tag`、`docker push`，并同步修改服务器 `.env`；不需要重新训练或更改预测代码。

**自行导出本地构建的镜像：** 在第 2 节的构建机执行：

```bash
docker save -o jingneng-power-forecast.tar jingneng-power-forecast:7station-2025-v1
```

将 tar 连同 `compose.yaml`、配置模板和接口资料交给服务器部署方。服务器执行 `docker load -i jingneng-power-forecast.tar`，按第 2 节配置同一镜像标签，再按第 3 节启动。

```bash
# 停止并移除容器，保留宿主机 runtime 数据
docker compose down
# 使用当前配置重新启动
docker compose up -d --pull never --wait --wait-timeout 300
```

升级时先备份 runtime、记录旧镜像地址，再更新 `.env` 中的镜像并启动。`docker compose restart` 只重启，不会应用新镜像配置；跨模型版本不要直接复用不兼容的历史缓存。
