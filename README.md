# 京能七站总功率预测服务

接收七个场站的真实功率、温度和湿度，返回未来 24 小时的 **96 点总功率预测**，间隔 15 分钟，单位 MW。当前模型版本：`trend_detail_7station_2025_v1`。

本仓库为完整源码工程，包含已训练模型、接口、测试和部署配置，可交付至公司 GitLab 维护。源码构建与镜像部署均无需重新训练模型。

## 交付内容

| 交付物 | 用途 |
|---|---|
| 源码工程 / 源码 ZIP | 导入功率预测项目的 GitLab 仓库，保留代码、模型、文档、样例及测试；不含原仓库 `.git` 历史、运行缓存或本机 `.env` |
| `offline-image-amd64-…` / `offline-image-arm64-…` 镜像包 | 按服务器架构选择，解压后按 [部署说明附录 A.2](docs/Docker部署运行说明.md) 导入启动；含 `image.tar.gz`，不用于替代 GitLab 源码工程 |
| 镜像包内 `release.json`、`SHA256SUMS` | 记录本次源码提交、镜像版本、架构、测试指标及文件校验值 |

镜像文件通过公司制品库、镜像仓库或发布附件交付，不提交普通 Git 源码历史。GitLab 自动构建需按公司 Runner 和镜像仓库配置，目前工程未配置 `.gitlab-ci.yml`。

## 代码与数据

以下文件随源码交接。`app/models/` 是模型实现代码，根目录的 `models/` 是已训练权重及配置，二者都需要保留。

| 文件或目录 | 用途 |
|---|---|
| `app/api.py` | HTTP 接口、错误响应、最近预测结果保存 |
| `app/input_adapter.py` | 七站测点解析、时间检查、缺失处理及输入特征构造 |
| `app/history_cache.py` | CSV 历史缓存、重复时间点合并、连续历史检查 |
| `app/predict.py`、`app/models/` | 模型加载、预测及 TrendDetail 融合计算 |
| `models/active_model.json`、`models/versions/` | 当前模型配置和七站全年版训练权重 |
| `check_config.py` | 必要文件、模型加载、权重哈希及模拟逻辑标记检查 |
| `tests/` | 接口、预处理、交付包和滚动准确率测试；`real_data_raw/` 存放七站真实测试源数据 |
| `docs/`、`examples/` | 交接文档及完整请求、响应 JSON 样例 |
| `Dockerfile`、`requirements.txt` | 容器构建及 Python 运行依赖 |
| `compose.yaml`、`.env.example` | 服务启动、端口、持久化目录和镜像地址配置 |
| `.github/workflows/build-image.yml` | GitHub 构建、容器测试、评分及镜像导出流程，不是 GitLab 流水线 |

运行时生成的 `runtime/` 保存历史缓存和最近结果，不随源码或镜像交付。当前缓存面向单个服务实例，不支持多个实例共用目录。

## 配套文档

| 要做什么 | 对应文件 |
|---|---|
| 封装、启动、检查服务能否预测 | [Docker 部署运行说明](docs/Docker部署运行说明.md) |
| 接口地址、请求头、输入输出、错误处理 | [接口交接说明](docs/接口交接说明.md) |
| 每个场站要传哪些字段、单位及缺失处理 | [测点需求清单](docs/测点需求清单.md) |

## 检查与测试

以下命令在**源码根目录**执行，使用 Python 3.11 的独立虚拟环境；不会训练模型。只部署已封装镜像时不需要执行本节。

```bash
python -m pip install -r requirements.txt
python check_config.py
python -m unittest discover -s tests -p "test_*.py" -v
python -m unittest discover -s tests/migration_2025 -p "test_*.py" -v
```

真实数据滚动预测测试使用独立的 `18000` 端口和测试缓存。`tests/results/handover` 首次须不存在，复测请换新目录，不要连接生产接口或复用生产缓存。先在一个终端启动测试服务：

```bash
python app/api.py --host 127.0.0.1 --port 18000 --device cpu --history-cache tests/results/handover/history.csv --latest-json tests/results/handover/latest.json
```

看到服务启动提示后，在另一个终端、同一源码目录和 Python 环境执行：

```bash
python -m pip install matplotlib
python tests/run_rolling_accuracy_test.py --base-url http://127.0.0.1:18000 --output-dir tests/results/handover/score
```

脚本按天提交真实历史，评分范围为 2025-10-20 至 12-31，共 73 天、7008 点。结果在 `tests/results/handover/score/summary.json`，包含 MAPE、MAE、RMSE 和 R²；结束后在服务终端按 Ctrl+C 停止测试服务。

## 部署与启动

在与目标服务器相同架构的构建机上，安装并启动 Docker Engine、安装 Compose 2.20+。在本仓库根目录构建镜像；此步骤打包现有模型，不训练模型：

```bash
docker build -t jingneng-power-forecast:7station-2025-v1 .
```

首次将 [.env.example](.env.example) 复制为 `.env`（已有配置不要覆盖），填写本次构建的镜像标签：

```dotenv
POWER_FORECAST_IMAGE=jingneng-power-forecast:7station-2025-v1
```

在 [compose.yaml](compose.yaml) 所在目录启动并查看状态：

```bash
docker compose config --quiet
docker compose up -d --pull never --wait --wait-timeout 300
docker compose ps
docker compose logs --tail 100 forecast
```

源码构建需要取得基础镜像和 Python 依赖；服务器不能联网时，可由公司的构建机完成。若另外收到已测试镜像，可直接导入或从公司镜像仓库拉取，跳过构建，详见 [Docker 部署运行说明](docs/Docker部署运行说明.md)。启动后仍需平台传入真实历史，不会自动加载训练 CSV。

## 数据怎么进，结果怎么出

1. 按部署说明启动容器。平台从自己的采集系统取数，不需要把训练 CSV 放到生产服务器。
2. 平台按测点清单组织 JSON，每次提交完整七站最近一天的 96 个实测点。通常每 15 分钟调用一次，历史回放也可按天顺序提交。
3. 向 `POST /api/power/forecast` 发送 JSON，请求头为 `Content-Type: application/json`，数据放在请求正文 Body 中。
4. 首次启用时，先按时间顺序补传历史。至少需要 672 个连续且温湿度可构造的点；不足返回 `409`，数据仍会缓存。满足条件后返回 `200` 和未来 96 点。
5. 从响应的 `data[].predictedTime` 和 `data[].predictedPower` 读取预测时间和功率。`GET /api/power/forecast/latest` 可取最近一次成功结果，不触发新预测。

## 完整 JSON 样例

- [输入样例](examples/input_example.json)：2025-10-19 的 96 个真实历史点。
- [冷启动输入样例](examples/input_not_ready_409.json)：2025-10-06 的 96 个真实历史点，与缓存不足输出配对。
- [成功输出样例](examples/output_example.json)：历史缓存已预热后，模型预测的 2025-10-20 的 96 点。
- [缓存不足输出样例](examples/output_not_ready_409.json)：首次提交 2025-10-06 数据时的 `409` 响应。

在本包根目录调用已启动的服务。服务器本机用 `127.0.0.1`；跨机器调用使用部署方提供的网关或内网地址。Compose 默认只监听本机，不能直接从其他电脑访问“服务器IP:8000”。

```bash
curl -X POST "http://127.0.0.1:8000/api/power/forecast" -H "Content-Type: application/json" --data-binary @examples/input_example.json
```

这里 `@examples/input_example.json` 表示由调用方读取文件内容并作为 Body 发送，服务器不需要这个文件。空缓存只发送一天样例不会直接预测成功；冷启动流程见接口说明，自动验收方式见部署说明。Windows PowerShell 请使用 `curl.exe`。

## 使用前须知

- 每个时间点是七站完整快照：19 个功率、8 个温度、8 个湿度字段。返回的是七站总功率，不是分别返回七站预测。
- 功率缺失按 0，温湿度仅沿用过去真实值。若温湿度长期缺测，七天记录也可能不足以预测。
- `/app/runtime` 必须持久化；缓存最多保留 768 个时间点（8 天）及状态上下文，不混用旧十站缓存。
- `accuracy` 是模型离线测试的固定历史参考，不是本次预测的实时准确率。
- 服务没有内置鉴权及 HTTPS，访问控制由部署平台或网关负责。

版本及测试记录以对应镜像包内的 `release.json` 为准，包含源码提交、CPU 架构、73 天 / 7008 点实测指标及镜像导出后重新导入验证状态。源码、文档和镜像须对应同一个提交版本；容器测试不替代目标服务器上的平台接入验收。

`docs/`、`examples/` 用于交接；`tests/` 用于接口测试和准确率复测，均不进入生产镜像。模型、运行代码及容器构建文件需要保留。
