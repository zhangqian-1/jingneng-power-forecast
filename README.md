# 京能七站总功率预测服务

接收七个场站的真实功率、温度和湿度，返回未来 24 小时的 **96 点总功率预测**，间隔 15 分钟，单位 MW。当前模型版本：`trend_detail_7station_2025_v1`。

## 代码与数据

以下是源码工程中的文件。离线镜像交付包只包含镜像、启动配置和对接资料，不包含全部源码目录。

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

## 交接时看这三份说明

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

从 GitHub 成功运行的 Actions 页面下载 `offline-image-对应架构-...` 附件，才是含镜像本体的交付包；仓库首页 `Code -> Download ZIP` 只是源码。服务器安装并启动 Docker 引擎、安装 Compose 2.20+，解压对应架构的交付包后在该目录执行：

```bash
docker load -i image.tar.gz
docker compose up -d --pull never --wait --wait-timeout 300
docker compose ps
docker compose logs --tail 100 forecast
```

交付包已附带填写好的 `.env`，导入镜像后无需连接 GitHub。使用 [compose.yaml](compose.yaml) 自行配置时，参考 [.env.example](.env.example) 填写镜像地址、端口和缓存目录；不要用空模板覆盖交付配置。

只有需要**从源码重新构建**时，才在源码根目录执行以下命令（使用构建机默认架构）；构建后把 `.env` 中的 `POWER_FORECAST_IMAGE` 设置为 `jingneng-power-forecast:local`，再执行上面的 Compose 命令，无需 `docker load`：

```bash
docker build -t jingneng-power-forecast:local .
```

模型、运行依赖已在镜像里，不需重新训练。启动成功后仍需平台传入真实历史，不会自动加载训练 CSV。架构选择、访问控制及 GitLab 制品交接见 [Docker 部署运行说明](docs/Docker部署运行说明.md)。

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
curl -X POST "http://服务器IP:8000/api/power/forecast" -H "Content-Type: application/json" --data-binary @examples/input_example.json
```

这里 `@examples/input_example.json` 表示由调用方读取文件内容并作为 Body 发送，服务器不需要这个文件。空缓存只发送一天样例不会直接预测成功；冷启动流程见接口说明，自动验收方式见部署说明。Windows PowerShell 请使用 `curl.exe`。

## 使用前须知

- 每个时间点是七站完整快照：19 个功率、8 个温度、8 个湿度字段。返回的是七站总功率，不是分别返回七站预测。
- 功率缺失按 0，温湿度仅沿用过去真实值。若温湿度长期缺测，七天记录也可能不足以预测。
- `/app/runtime` 必须持久化；缓存最多保留 768 个时间点（8 天）及状态上下文，不混用旧十站缓存。
- `accuracy` 是模型离线测试的固定历史参考，不是本次预测的实时准确率。
- 服务没有内置鉴权及 HTTPS，访问控制由部署平台或网关负责。

2026-09-12，代码提交 `3cbe226fb967` 的 AMD64、ARM64 镜像均已在 GitHub 完成构建、Compose 启动、重建容器缓存恢复及 73 天真实测试集验收（7008 点），MAPE 均为 **10.8778%**。镜像已发布，构建记录见部署说明；目标服务器尚未部署验收，不能据此认定已正式上线。

`docs/`、`examples/` 用于交接；`tests/` 用于接口测试和准确率复测，均不进入生产镜像。模型、运行代码及容器构建文件需要保留。
