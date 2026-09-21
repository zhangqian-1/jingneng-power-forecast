# Docker 部署运行说明

## 1. 项目说明与运行环境

本项目提供七场站总功率预测服务，交付内容包括服务源码、已训练模型、Docker 构建与启动配置、接口说明及 JSON 样例。服务接收平台提供的真实历史测点数据，历史满足要求后返回未来 24 小时、共 96 点的七场站总功率预测结果。

生产运行不依赖测试集，不进行在线训练。本文面向部署及运维人员；测点、输入输出字段和调用样例见 [接口交接说明](接口交接说明.md) 与 [测点需求清单](测点需求清单.md)。

本版为平台接口适配版。训练CSV时区尚未确认，当前保留时间标签、不加减8小时。可先构建进行联调，但正式上线前须完成训练时间口径确认及UTC时间对齐验收。历史发布 `042dcd1` 的镜像不包含新接口，不能用它验收本文的新接口行为。

| 项目 | 要求 |
|---|---|
| 容器环境 | Docker Engine 已启动，Docker Compose 2.20 及以上版本；宿主机无需另装 Python |
| CPU 架构 | 镜像与服务器架构一致；Linux 用 `uname -m` 查看，`x86_64` 对应 `amd64`，`aarch64` 对应 `arm64` |
| 构建网络 | 可下载基础镜像和 Python 依赖；离线服务器按附录 A 导入镜像 |
| 存储与网络 | 提供可写的持久化目录，并配置平台访问地址及端口 |

以下为源码部署流程。已交付镜像的导入方式见附录 A。

源码工程交付至功率预测项目的 GitLab 仓库；镜像包作为独立部署制品交付。二者通过镜像包内 `release.json` 的 `commit` 对应，配置及文档使用同一提交版本。

## 2. 部署配置

从项目仓库取得完整源码，放入固定部署目录。以下命令均在包含 `Dockerfile`、`compose.yaml` 和 `.env.example` 的项目根目录执行。

首次部署时，从 `.env.example` 创建同目录的 `.env`，已有配置保留：

```bash
# Linux，仅首次执行，不覆盖已有配置
cp -n .env.example .env
```

Windows 可通过文件管理器复制，文件名为 `.env`。在 `.env` 中填写与第 3.1 节构建命令一致的镜像标签：

```dotenv
POWER_FORECAST_IMAGE=jingneng-power-forecast:7station-platform-v1
```

| 配置项 | 填写内容 |
|---|---|
| `POWER_FORECAST_IMAGE` | 必填，部署镜像标签或公司镜像仓库地址 |
| `POWER_FORECAST_BIND` | 默认 `127.0.0.1`，只允许服务器本机或同机网关访问 |
| `POWER_FORECAST_PORT` | 默认 `8000`；被占用时换一个空闲端口 |
| `POWER_FORECAST_RUNTIME_DIR` | 默认 `./runtime`，相对于 `compose.yaml` 所在目录；可设置为专用、可写的持久化目录 |

跨机器直连时，部署方需设置内网监听地址或 `0.0.0.0`，并配置防火墙白名单。服务无内置鉴权和 HTTPS，应通过受控内网或网关访问，不直接暴露公网。`.env` 仅保存在部署机，不提交源码仓库、不装入镜像。

## 3. 构建与启动

### 3.1 构建镜像

在与目标服务器相同架构的机器上执行：

```bash
docker build -t jingneng-power-forecast:7station-platform-v1 .
```

构建使用已训练模型，无需重新训练。构建机与部署服务器不同时，按附录 A 导出并传输镜像；跨架构构建需配置 Buildx 并单独验证。

### 3.2 启动服务

在部署服务器上、`compose.yaml` 与 `.env` 所在目录执行，前一条命令成功后执行下一条：

```bash
docker compose config --quiet
docker compose up -d --pull never --wait --wait-timeout 300
docker compose ps
docker compose logs --tail 100 forecast
```

`config` 校验配置，`up` 启动本地已有镜像。使用公司镜像仓库时，应先登录仓库、配置镜像地址并执行 `docker compose pull`；凭据由部署方管理，不写入源码或文档。

## 4. 接口检查与数据接入

在服务器本机检查接口：

```bash
curl "http://127.0.0.1:8000/api/v1/fluxcast/compute/latest"
```

Windows PowerShell 使用 `curl.exe`；地址及端口按部署配置调整。平台跨机器调用使用部署方提供的网关或内网地址。

平台通过 `POST /api/v1/fluxcast/compute` 提交 `point_table + frames` JSON，每次包含七站35个测点、最近一天的96个连续15分钟历史点，通常每15分钟滚动提交。服务不主动抓取平台数据；请求格式、测点及缺失处理以 [接口交接说明](接口交接说明.md) 为准。

首次运行需按时间顺序补传真实历史，或持续累计，直至满足 **672个连续且温湿度可构造的历史点**。历史或天气未就绪返回HTTP 200和空 `result_point`，通过 `reason`、`message` 说明原因，本次数据仍缓存，应继续补传而非清空缓存。

| 检查结果 | 含义 |
|---|---|
| 容器为 `healthy` | 服务可响应，不代表历史数据已经够用 |
| 首次 GET `latest` 返回 `404` | 尚无成功预测，属于正常初始状态 |
| POST 返回 `200`，`result_point` 为空 | 历史或天气未就绪，查看 `reason`、`continuous_points`、`missing_weather_points` |
| POST 返回 `200`，`result_point` 为96点 | 预测成功；`value` 为七站总功率，单位MW |
| 容器为 `unhealthy` 或接口返回其他错误 | 查看服务日志及接口文档中的错误说明 |

预测从输入末点后15分钟开始。GET `latest`仅查询最近一次成功结果，不触发新预测；后续空结果不会覆盖它，应核对 `result_point[].timestamp` 确认结果时效。接口不输出历史参考 `accuracy`。本版仅保留平台接口及配套样例。

## 5. 缓存与日常维护

宿主机持久化目录挂载到容器 `/app/runtime`，保存最多 768 个时间点（8 天）、历史上下文和最近结果。重建容器时保留该目录；每个实例使用独立目录，不复用旧十站或其他不兼容模型的缓存。

新平台接口默认缓存为 `history_7station_2025_v1_platform_unconfirmed_v1.csv`，最近结果为 `latest_forecast_platform.json`。不再提供旧接口，也不要将旧版本缓存直接改名复用。首次联调建议新建专用runtime目录；未来时区确认后如需转换，应另行验证和迁移缓存。

```bash
# 停止并移除容器，保留宿主机 runtime 数据
docker compose down
# 使用当前配置重新启动
docker compose up -d --pull never --wait --wait-timeout 300
```

`unless-stopped` 自动恢复异常退出或随引擎重启的容器，手动停止的容器需重新启动。健康检查使用 `latest` 接口，无单独的 `/health` 路由；仅健康检查失败不会自动重启。

升级前停止服务并备份 runtime，保留旧镜像版本信息；导入或拉取新镜像、更新 `.env` 后执行 `up`。`docker compose restart` 不应用新镜像配置。

## 附录 A. 离线镜像部署

### A.1 导出自建镜像

在第 3.1 节的构建机执行：

```bash
docker save -o jingneng-power-forecast.tar jingneng-power-forecast:7station-platform-v1
```

将镜像、`compose.yaml`、`.env.example` 和交接文档传至服务器固定目录，在该目录导入：

```bash
docker load -i jingneng-power-forecast.tar
```

按第 2 节创建 `.env`、填写相同镜像标签，再按第 3.2 节启动。服务器无需再次构建。

### A.2 导入配套镜像包

历史镜像下载：[GitHub Release v7station-2025-042dcd1](https://github.com/zhangqian-1/jingneng-power-forecast/releases/tag/v7station-2025-042dcd1)。这些附件只包含旧接口，不包含本次平台适配。本版须重新构建并发布新的AMD64或ARM64镜像；源码使用对应同一构建提交的版本。私有仓库下载需要有访问权限的GitHub账号；公司接收后转存至指定制品位置。

将匹配服务器架构的完整镜像包解压至固定目录。包内包含 `image.tar.gz`、`compose.yaml`、`.env`、`release.json` 和 `SHA256SUMS`。在解压目录校验，成功后导入：

```bash
sha256sum -c SHA256SUMS
docker load -i image.tar.gz
```

上述校验命令适用于 Linux；Windows PowerShell 使用 `Get-FileHash -Algorithm SHA256 image.tar.gz` 与 `release.json` 中的镜像文件校验值比对。校验失败时停止部署并重新核对文件。

保留包内 `.env` 的镜像标签，按第 2 节配置端口、访问地址和持久化目录，再按第 3.2 节启动。镜像不含测试历史或预热缓存，首次数据接入按第 4 节执行。

## 附录 B. 验证记录与验收

本次封装记录保存在镜像包内的 `release.json`，交接时核对：

| 字段 | 核对内容 |
|---|---|
| `commit`、`model` | 对应源码提交与当前模型版本 |
| `platform`、`image`、`image_id` | 服务器架构、镜像标签与镜像 ID |
| `metrics` | 73 天 / 7008 点真实测试集的 MAPE、MAE、RMSE 等实测指标 |
| `compose_smoke_test`、`container_recreation_test` | 接口预测、容器重建后缓存恢复，均应为 `passed` |
| `offline_image` | 镜像文件 SHA256、大小；`export_import_test` 和 `import_prediction_test` 均应为 `passed` |
| `platform_contract`、`training_timezone`、`utc_production_acceptance` | 新构建会标记接口版本、训练时区未确认及UTC验收待完成；不能因镜像测试通过就忽略该限制 |

文件完整性按 `SHA256SUMS` 校验。镜像内仅包含运行代码、依赖及当前七站模型；文档、样例位于交付包外层，不参与运行。

历史Release绑定源码提交 `042dcd1ccd1dd75f22cf9849a1cfffc14a93802e`，本地接口修改不会改变已发布附件。新交付必须使用新标签及对应源码提交，不覆盖旧版校验文件。Release附件不受Actions制品14天保留期限制。

后续构建：GitHub 推送 `main` 默认构建 AMD64；ARM64 通过 Actions 手动运行 `Build And Test Docker Image`，选择 `architecture=arm64`。工作流全部成功后，从该次运行取得对应的 `offline-image-…` 及 `container-checks-…` 制品，再发布为新版本。GitHub 验证流程不在 GitLab 自动执行。

复测步骤见 [README](../README.md)，使用独立端口和空缓存实例，历史测试集及样例数据不发送到生产服务。`tests/`、`examples/`、`docs/` 属于交接资料，不进入运行镜像。

本地实际验证记录见 [平台适配验证记录](平台适配验证记录.md)。GitHub流程已增加新接口预热、重建和镜像导入测试，以及平台接口完整测试集评分；本地修改流程文件不代表GitHub已执行。

上线验收由部署方与平台对接方在目标服务器完成：先确认训练CSV时区、UTC时间对齐，再检查真实数据调用、200空结果原因、96点预测、重建容器后缓存保留、访问控制、资源占用及响应时间。历史测试指标不作为实时预测精度保证，重新构建的镜像需复测。
