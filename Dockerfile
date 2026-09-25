# 使用Python 3.11作为基础镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app/ ./app/
COPY models/active_model.json ./models/active_model.json
COPY models/versions/single_step_7station_2025_v1/ ./models/versions/single_step_7station_2025_v1/

# 只保存由真实接口请求生成的最近一次结果
RUN mkdir -p /app/runtime
VOLUME ["/app/runtime"]

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# 暴露API端口（如果使用API服务）
EXPOSE 8000

# 默认启动命令：运行API服务
# 如果要运行预测脚本，使用: docker run <image> python app/predict.py
CMD ["python", "app/api.py", "--host", "0.0.0.0", "--port", "8000"]
