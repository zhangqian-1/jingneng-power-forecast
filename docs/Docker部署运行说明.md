# Docker 部署运行说明

## 1. 生产运行内容

生产服务运行只依赖：

    app/
    models/
    Dockerfile
    requirements.txt

tests、原始 CSV、测试 JSON、GitHub Actions 文件和绘图文件不属于服务运行依赖。它们被 .dockerignore 排除，不会进入镜像。

## 2. 环境要求

- Docker Engine 或 Docker Desktop
- 能够安装 requirements.txt 中的 Python 依赖
- 至少 8 GB 可用内存，实际资源以目标服务器压力测试为准
- 服务器 CPU 架构必须与构建镜像一致
- 目标服务器若为 ARM64/aarch64，必须在目标 ARM 环境实际构建或验证 PyTorch、NeuralForecast 和模型推理

当前 Dockerfile 使用 Python 3.11 slim 基础镜像。不要只依据 x86 构建成功就认定 ARM 服务器一定可运行。

## 3. 构建镜像

在生产部署包根目录执行：

    docker build -t jingneng-power-forecast:latest .

查看镜像：

    docker images jingneng-power-forecast

如果服务器不能直接访问镜像仓库，可以在构建机导出并在服务器导入：

    docker save jingneng-power-forecast:latest -o jingneng-power-forecast.tar
    docker load -i jingneng-power-forecast.tar

## 4. 启动容器

先创建服务器上的持久化目录：

    mkdir -p /data/jingneng/runtime

启动服务：

    docker run -d --name jingneng-power-forecast -p 8000:8000 -v /data/jingneng/runtime:/app/runtime --restart unless-stopped jingneng-power-forecast:latest

其中 /data/jingneng/runtime 必须是持久化目录。服务会在其中产生：

    real_history_cache.csv
    latest_forecast.json

如果不挂载该目录，容器删除或重建后历史缓存会丢失，需要重新累计 672 个点。

## 5. 启动检查

查看容器状态：

    docker ps --filter name=jingneng-power-forecast

查看启动日志：

    docker logs jingneng-power-forecast

正常启动日志应包含：

    Power forecast API: http://0.0.0.0:8000
    POST /api/power/forecast
    GET  /api/power/forecast/latest

查看最近结果：

    curl "http://服务器IP:8000/api/power/forecast/latest"

首次预测完成前返回 HTTP 404 属于正常状态，表示还没有最新预测结果。

## 6. 停止和重启

    docker restart jingneng-power-forecast
    docker logs --tail 100 jingneng-power-forecast

停止并删除容器不会删除宿主机挂载的 runtime 数据：

    docker stop jingneng-power-forecast
    docker rm jingneng-power-forecast

## 7. 网络和安全边界

容器监听 8000 端口。正式环境建议由网关负责：

- HTTPS 终止；
- 访问控制或 Token 鉴权；
- 请求超时和重试；
- 日志收集；
- 对外暴露范围控制。

当前应用本身不提供鉴权和专用健康检查接口，部署平台需要明确承担这些职责。
