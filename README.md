# 京能全站总功率预测服务

完整的输入、数据处理、模型推理和输出流程见：

```text
全站总功率预测模型_输入到输出完整流程.md
```

本目录是生产运行包。外部系统每 15 分钟提交最近 1 天真实数据，服务内部持久化并拼接历史数据，再调用本次离线训练并导出的 7 天模型预测未来 1 天。

```text
外部每次提交最近1天96点
→ 与本地真实历史缓存按时间戳合并、去重、更新
→ 保留最近连续7天672点作为模型输入（最多额外保留96点用于状态特征平滑）
→ 完整离线TrendDetail模型链预测未来1天96点
→ 接口返回JSON
```

输入数据处理：十个场站的全部功率测点先求和形成一个 `total_power`，再进行全站总功率预测。缺失或非数值测点沿用上一时刻真实值；数值 `0` 保留为有效值；不插值、不做低负荷修复、不改写异常值、不生成虚拟数据。温湿度按同样的上一真实值规则补齐，深圳钰湖华氏温度点在模型输入前转换为摄氏度。

## 启动

```bash
py -3.11 app/api.py --host 0.0.0.0 --port 8000
```

GPU 环境：

```bash
py -3.11 app/api.py --host 0.0.0.0 --port 8000 --device cuda
```

## 接口

### 提交最近一天真实数据

```text
POST /api/power/forecast
Content-Type: application/json
```

每次请求必须包含最近一天共 96 个连续时间点。服务会将请求与 `runtime/real_history_cache.csv` 合并，重复时间使用最新提交的非空真实值，不会重复累计。

缓存不足 672 个连续点时返回 HTTP `409`，并返回当前缓存进度。数据已经保存，后续请求会继续累计。服务最多保留 768 个连续点：最近 672 点送入模型，额外 96 点只用于状态特征平滑。缓存满足 672 点后，接口直接返回未来一天 96 个预测点。

缺失规则：

- 未传场站、未传测点、空值或非数值：使用缓存中该测点上一时刻真实值；
- 数值 `0`：有效实测值，不填充；
- 缓存首次初始化时首点缺失：可使用本批次该测点最早真实值初始化；
- 某测点在可用历史中完全没有真实值：返回错误，不生成随机值或固定值。

### 获取最近一次预测结果

```text
GET /api/power/forecast/latest
```

尚未完成过一次真实预测时返回 `404`。

## 线上模型

| 项目 | 内容 |
|---|---|
| 模型 | `StationAttentionFusion_TrendDetail_deployable_v2` |
| 外部单次输入 | 最近1天，96点 |
| 服务内部缓存 | 最多768点，其中最近672点为模型输入，额外96点用于状态特征平滑 |
| 模型实际输入 | 过去7天，672点 |
| 模型输出 | 未来1天，96点 |
| 测试集RMSE | 285.49 MW |
| 测试集MAPE | 6.9694% |
| 波动差分强度 | `diff_std_ratio=0.7950` |
| 数据划分 | 0.6 / 0.2 / 0.2 |

线上执行与离线一致的完整链路：`NHITS + PatchTST` 低频融合、`StationAttentionHF` 站点细节预测、MAE/HF 二次融合，以及最终 TrendDetail 趋势细节叠加。本次所有模型均使用同一份真实历史训练数据、同一时间切分和同一数据处理规则重新训练，并保存为可加载产物。最终 TrendDetail 使用 `mae_smooth_w12 + 0.95 * hf_highpass_w24`。

活动模型由 `models/active_model.json` 指定。模型版本目录包含全部模型文件、融合参数、指标和 SHA-256 哈希。后续换模型时新增版本目录并切换该配置，接口代码不需要修改。

## 输出示意

```json
{
  "code": 200,
  "msg": "success",
  "model": "StationAttentionFusion_TrendDetail_deployable_v2",
  "testRMSE": 285.4938,
  "historyCache": {
    "ready": true,
    "continuousPoints": 672,
    "requiredPoints": 672
  },
  "data": [
    {
      "predictedTime": "未来时间",
      "timeSeries": 1,
      "predictedPower": "全站总功率预测值",
      "accuracy": "基于真实测试集逐预测步历史误差的估算值"
    }
  ]
}
```

## 生产原则

- 不使用示例输入、随机数据或正弦模拟预测；
- 不把预测结果 CSV 当作模型；
- 真实缓存文件由接口请求产生，Docker 部署时应把 `runtime/` 挂载到持久化目录；
- 上线前运行 `py -3.11 check_config.py` 检查活动模型、全部组件哈希和接口配置。

## 更换模型

```text
1. 将新的完整模型版本放入 models/versions/<版本名>/
2. 在版本目录中提供 manifest.json 和全部权重
3. 将 models/active_model.json 的 model_dir 改为新版本目录
4. 运行 check_config.py
5. 重启服务
```

接口层始终调用活动模型，不应在 `app/api.py` 中写死具体模型名称。
