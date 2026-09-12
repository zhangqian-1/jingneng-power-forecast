# Docker 部署运行说明

## 1. 交给接收方什么

启动服务需要经过测试的镜像地址（或镜像 tar）、[compose.yaml](../compose.yaml) 和填写好的 `.env`。接收方需安装并启动 Docker Engine，安装 Docker Compose 2.20+；不用另装 Python，也不用训练。对接说明和 JSON 样例从同一提交的源码包一并交接，不要只发启动配置。

镜像架构必须与服务器一致：`x86_64` 对应 `amd64`，`aarch64` 对应 `arm64`。Linux 服务器通常执行 `uname -m` 查看。运行内存和耗时以目标服务器实测为准。

## 2. 首次配置

把 `compose.yaml` 放在固定部署目录，不要使用临时目录。使用 GitHub 成功构建产物中的 `delivery/release.env` 时，将它放在同目录并命名为 `.env`；也可以从 [.env.example](../.env.example) 创建 `.env`：

```bash
# Linux，仅首次执行，不覆盖已有配置
cp -n .env.example .env
```

Windows 可以在文件管理器中复制并重命名；文件名必须是 `.env`，不是 `.env.txt`，不要覆盖已有配置。模板中的镜像地址留空是正常的，必须填写后才能启动；`release.env` 只在 GitHub 构建、测试和发布成功后生成，目前尚未提供正式发布地址。

| 配置项 | 填写内容 |
|---|---|
| `POWER_FORECAST_IMAGE` | 必填，使用成功构建记录里的完整镜像地址；推荐线上使用记录中的摘要地址 |
| `POWER_FORECAST_BIND` | 默认 `127.0.0.1`，只允许服务器本机或同机网关访问 |
| `POWER_FORECAST_PORT` | 默认 `8000`；被占用时换一个空闲端口 |
| `POWER_FORECAST_RUNTIME_DIR` | 默认 `compose.yaml` 同目录下的 `runtime`，可改为专用持久化目录；需可写，不混用旧十站缓存 |

需要其他机器直连时，由部署方配置内网监听地址或 `0.0.0.0`，并限制防火墙白名单。服务没有内置鉴权和 HTTPS，不要直接开放到公网。`.env` 是本机配置，不提交 GitHub、不装入镜像。

## 3. 一条命令启动

取得对应架构的镜像并完成上面的配置后，在 `compose.yaml` 所在目录执行，Linux 和 Windows 均可。这条命令启动已有镜像，不负责构建代码：

```bash
docker compose up -d --wait --wait-timeout 300
```

首次会在本地缺少镜像时尝试拉取。如果镜像仓库私有，先用有读取权限的账号执行 `docker login ghcr.io`，按提示完成认证，不要把密码或 Token 写进配置文件。

查看状态、日志和最近结果：

```bash
docker compose ps
docker compose logs --tail 100 forecast
curl "http://127.0.0.1:8000/api/power/forecast/latest"
```

Windows PowerShell 用 `curl.exe`，修改监听地址或端口后同步修改访问地址。`healthy` 表示服务接口可响应，不代表历史数据够了；首次无结果时 GET 返回 `404` 正常。Compose 检查的是现有 `latest` 接口，没有新增 `/health`；状态变为 `unhealthy` 时应查日志，它不会仅因健康检查失败而自动重启。

平台按 [接口交接说明](接口交接说明.md) POST 真实数据。缓存不足时返回 `409` 并继续保存，满足 672 个连续且温湿度可构造的点后预测未来 96 点。

`runtime` 保存历史、状态上下文和最近结果，重建容器不会自动删除它。`unless-stopped` 会重启异常退出的容器，也会随 Docker 引擎启动恢复未被手动停止的容器；已手动停止的需再次启动。一个缓存目录只给一个服务实例使用。

## 4. 封装和测试在哪里做

**GitHub 构建与容器测试：** 使用原仓库，完整提交当前代码、七站模型以及新增配置后推送。普通推送构建 `amd64`；要交付 ARM，进入 Actions → Build And Test Docker Image → Run workflow，选择 `architecture=arm64`。ARM 构建需仓库支持 `ubuntu-24.04-arm` 执行器，排队或失败不代表已经通过。

工作流会自动执行：

1. 构建与目标架构一致的镜像，用本份 Compose 配置启动。
2. 用真实七站数据提交 2025-10-06 至 10-12 的七日请求，验证前六次 `409`、第七次 `200`，并核对模型名、96 点时间和功率数值。
3. 重建容器，检查最近结果、历史缓存和重复预测仍一致。
4. 使用另一个空缓存容器，回放 2025-10-20 至 12-31 的 73 天测试集，计算 MAPE；与当前离线参考相差超过 0.01 个百分点则停止发布。
5. 通过后发布带架构、提交号和运行编号的新镜像标签，不覆盖旧 `v2/latest`。

到这次运行的 Summary 看镜像地址、摘要和实测 MAPE。Artifacts 中的 `container-checks-...` 保存日志、`release.json`、`delivery/compose.yaml`、`delivery/release.env` 和评分结果，**不含镜像 tar，也不是完整的接口交接资料**。失败运行也可能有日志附件，只有日志不代表发布成功。附件保留 14 天，正式交接需另行保存。镜像本身不含测试 CSV、文档、样例和测试缓存。

**本地检查：** 配好 `.env` 后执行 `docker compose config --quiet`，不需要 Docker 引擎即可检查配置；它不能证明容器能运行。需要复测时，在有 Docker 的测试机使用独立目录、端口和项目名，运行 `tests/run_api_test.py`（七日接口）和 `tests/run_rolling_accuracy_test.py`（完整评分），不要向生产缓存回放历史测试数据。

**目标服务器检查：** 拉取对应架构的同一个镜像，按第 3 节启动，再完成接口调用、缓存持久化和访问控制检查。GitHub 通过不替代目标服务器验收。截至本次文档更新，新的 Compose 容器流程尚未在 GitHub 或目标服务器实际执行，不能标记为已上线。

## 5. 离线交付与维护

服务器不能访问镜像仓库时，在能取得该镜像的 Docker 机器上导出。以下 `完整镜像标签` 替换为成功构建记录里的实际值：

```bash
docker pull 完整镜像标签
docker save -o jingneng-power-forecast.tar 完整镜像标签
```

将 tar 传到服务器后执行 `docker load -i jingneng-power-forecast.tar`，并把 `.env` 中镜像填写为导入的同一标签，再执行一条命令启动。不要把镜像 tar 提交到代码仓库。

```bash
# 停止并移除容器，保留宿主机 runtime 数据
docker compose down
# 使用当前配置重新启动
docker compose up -d --wait --wait-timeout 300
```

升级时先备份 runtime、记录旧镜像地址，再更新 `.env` 中的镜像并启动。`docker compose restart` 只重启，不会应用新镜像配置；跨模型版本不要直接复用不兼容的历史缓存。
