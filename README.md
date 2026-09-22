# 汉字笔顺轨迹校验 API

基于 **FastAPI + SQLite** 的汉字书写校验服务：接收目标汉字的参考笔画序列与用户书写轨迹，
按**笔画方向、起止点、书写顺序**做归一化匹配，支持**可配置的分叉/转折笔画容差**，
返回**首个不合格笔画、偏差原因与整体得分**。同一提交幂等。

此外提供**候选字识别**（`POST /v1/recognize`）：输入未标注字符的笔画轨迹，
在全部已注册参考字中按置信度返回 top-k 候选，通过全局一对一最优分配容忍书写次序差异，
并把缺笔/多笔作为显式惩罚纳入总分。

**练习质量趋势**（`GET /v1/analytics/practice`）：按字符、时间闭区间与通过状态筛选
已保存的校验提交，在单次一致性快照中返回提交数、通过率、平均分、P50/P90 分数、
首个失败笔画分布与按 UTC 日期分桶的趋势。

## 快速启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

启动后访问交互式文档（Swagger UI）：**http://localhost:8000/docs**

### 本地开发

```bash
pip install -r requirements.txt
DATABASE_PATH=./data.db uvicorn app.main:app --port 8000
```

运行测试：

```bash
pip install pytest httpx
pytest tests/ -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/v1/config` | 查看服务端默认容差 |
| `PUT` | `/v1/references/{character}` | 注册/更新某汉字的参考笔画序列 |
| `GET` | `/v1/references/{character}` | 查询参考笔画 |
| `POST` | `/v1/recognize` | 候选字识别：未标注轨迹 → top-k 候选 |
| `POST` | `/v1/validate` | 校验一次书写提交（幂等） |
| `GET` | `/v1/submissions/{submission_id}` | 按幂等键查询历史结果 |
| `GET` | `/v1/analytics/practice` | 练习质量趋势：通过率/分位数/失败笔画分布/按日趋势 |

### 示例：候选字识别

```bash
# 先注册若干参考字（沿用 PUT /v1/references/{character}）
curl -X PUT http://localhost:8000/v1/references/十 \
  -H 'Content-Type: application/json' \
  -d '{"character":"十","strokes":[[[100,200],[300,200]],[[200,100],[200,300]]]}'

# 提交一段未标注字符的轨迹（即使笔顺与参考不同也能识别）
curl -X POST http://localhost:8000/v1/recognize \
  -H 'Content-Type: application/json' \
  -d '{
        "strokes": [[[201,99],[199,301]], [[102,198],[298,203]]],
        "top_k": 3
      }'
```

请求字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `strokes` | 是 | 用户书写轨迹（每笔 ≥2 个点）；**不要求按参考笔顺书写** |
| `top_k` | 是 | 返回候选数上限，整数且 `1 ≤ top_k ≤ 100`；非法值返回 `422` |
| `canvas_size` | 否 | `[w, h]`，缺省时各参考字按自身外接框分别归一化 |

响应：没有任何已注册参考字时 `candidates` 为空列表；`total_references`
报告参与比对的参考字总数，候选数不超过 `top_k`。

```json
{
  "top_k": 3,
  "total_references": 4,
  "candidates": [
    {
      "character": "十",
      "score": 100.0,
      "reference_stroke_count": 2,
      "user_stroke_count": 2,
      "matched_stroke_count": 2,
      "correspondences": [
        {"user_stroke_index": 0, "reference_stroke_index": 1, "cost": 0.0,
         "metrics": {"start_deviation": 0.0, "end_deviation": 0.0,
                      "direction_deviation_deg": 0.0, "shape_deviation": 0.0,
                      "is_forked": false}},
        {"user_stroke_index": 1, "reference_stroke_index": 0, "cost": 0.0,
         "metrics": { "...": "..." }}
      ],
      "penalties": {
        "missing_count": 0,
        "extra_count": 0,
        "missing_stroke_indices": [],
        "extra_stroke_indices": [],
        "missing_penalty_per_stroke": 1.0,
        "extra_penalty_per_stroke": 1.0,
        "matched_cost_total": 0.0,
        "missing_penalty_total": 0.0,
        "extra_penalty_total": 0.0,
        "total_cost": 0.0
      }
    }
  ]
}
```

- `score`：置信度 `0..100`，越高越相似；候选按 `score` 降序，**同分按字符稳定排序**。
- `correspondences`：最优一对一分配下的笔画对应关系；`reference_stroke_index` 为
  `null` 表示该用户笔是**多笔**（无对应参考笔）。
- `penalties`：缺笔/多笔惩罚明细——缺笔下标、多笔下标、单笔惩罚值与各项合计，
  `total_cost = matched_cost_total + missing_penalty_total + extra_penalty_total`。

缺笔/多笔示例（只写一横去匹配“十”）：`matched_stroke_count=1`、
`missing_count=1`、`missing_stroke_indices=[1]`、`score=50.0`。

### 示例：校验“十”字

```bash
# 1. 注册参考笔画（横、竖，画布坐标）
curl -X PUT http://localhost:8000/v1/references/十 \
  -H 'Content-Type: application/json' \
  -d '{"character":"十","strokes":[[[100,200],[300,200]],[[200,100],[200,300]]]}'

# 2. 提交用户轨迹（reference_id 引用已注册参考；也可内联 reference_strokes）
curl -X POST http://localhost:8000/v1/validate \
  -H 'Content-Type: application/json' \
  -d '{
        "submission_id": "attempt-0001",
        "character": "十",
        "reference_id": "十",
        "strokes": [[[102,198],[298,203]],[[201,99],[199,301]]]
      }'
```

响应（节选）：

```json
{
  "submission_id": "attempt-0001",
  "passed": true,
  "score": 97.7,
  "first_failed_stroke": null,
  "deviation": null,
  "strokes": [
    {"index": 0, "passed": true, "score": 96.8, "reasons": [],
     "metrics": {"start_deviation": 0.0129, "end_deviation": 0.0164,
                  "direction_deviation_deg": 1.46, "shape_deviation": 0.0079,
                  "is_forked": false}}
  ],
  "idempotent_replay": false
}
```

不合格时 `deviation` 给出首个失败笔画的原因码与实测值：

```json
{
  "passed": false,
  "first_failed_stroke": 0,
  "deviation": {
    "code": "START_POINT_DEVIATION",
    "stroke_index": 0,
    "message": "第 1 笔起点偏差过大（0.273，上限 0.150）",
    "metrics": { ... }
  }
}
```

### 偏差原因码

| code | 含义 |
| --- | --- |
| `STROKE_COUNT_MISMATCH` | 笔画数量不符（缺笔/多笔） |
| `WRONG_ORDER` | 笔顺错误（该笔与另一笔参考形状相符） |
| `START_POINT_DEVIATION` | 起点偏差超限 |
| `END_POINT_DEVIATION` | 终点偏差超限 |
| `DIRECTION_DEVIATION` | 运笔方向偏差超限 |
| `SHAPE_DEVIATION` | 笔画形状偏差超限 |

### 示例：练习质量趋势

```bash
curl "http://localhost:8000/v1/analytics/practice?character=十&start=2026-09-20T00:00:00Z&end=2026-09-23T00:00:00Z"

# 只看未通过的提交
curl "http://localhost:8000/v1/analytics/practice?character=十&start=2026-09-20T00:00:00Z&end=2026-09-23T00:00:00Z&passed=false"
```

查询参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `character` | 是 | 目标汉字（单字） |
| `start` / `end` | 是 | ISO-8601 时间，**闭区间**；带时区偏移时换算为 UTC，不带偏移按 UTC 处理；`start > end` 返回 `422` |
| `passed` | 否 | `true`/`false` 只统计通过/未通过的提交；缺省不筛选 |

响应（示例）：

```json
{
  "filter": {
    "character": "十",
    "start": "2026-09-20T00:00:00Z",
    "end": "2026-09-23T00:00:00Z",
    "passed": null
  },
  "generated_at": "2026-09-22T08:30:00+00:00",
  "submission_count": 5,
  "passed_count": 2,
  "pass_rate": 0.4,
  "average_score": 60.0,
  "p50_score": 60.0,
  "p90_score": 92.0,
  "first_failed_strokes": [
    {"stroke_index": 0, "count": 1},
    {"stroke_index": 1, "count": 2}
  ],
  "daily_trend": [
    {"date": "2026-09-20", "submission_count": 2, "passed_count": 2,
     "pass_rate": 1.0, "average_score": 90.0, "p50_score": 90.0, "p90_score": 98.0},
    {"date": "2026-09-21", "submission_count": 2, "passed_count": 0,
     "pass_rate": 0.0, "average_score": 50.0, "p50_score": 50.0, "p90_score": 58.0},
    {"date": "2026-09-22", "submission_count": 1, "passed_count": 0,
     "pass_rate": 0.0, "average_score": 20.0, "p50_score": 20.0, "p90_score": 20.0}
  ]
}
```

- 统计基于**单次加锁读取的一致性快照**，`filter` 回显实际生效的筛选条件，
  `generated_at` 为响应生成的 UTC 时间；
- `p50_score` / `p90_score` 采用**显式线性插值**：升序序列 `x_0..x_{n-1}`，
  位置 `h = q·(n−1)`，取 `x_⌊h⌋ + (h−⌊h⌋)·(x_⌈h⌉ − x_⌊h⌋)`；
- `first_failed_strokes` 只统计未通过提交，按笔画下标升序；
- `daily_trend` 按 **UTC 日期**分桶、日期升序，仅包含有提交的日期，
  每桶复用同一套汇总指标；
- 无匹配提交时各数值为 `0`，`first_failed_strokes` 与 `daily_trend` 为空数组。

## 匹配算法

1. **归一化**：以参考字外接框（扩成正方形 + 5% 边距）做仿射变换映射到 `0..1` 区间，
   用户轨迹套用**同一变换**，保留位置与尺寸的绝对偏差；请求携带 `canvas_size` 时改按画布归一化。
2. **重采样**：每笔按弧长等距重采样为 32 个点（`RESAMPLE_POINTS` 可调）。
3. **逐笔比对**（按书写顺序一一对应）：
   - 起点/终点：归一化欧氏距离 ≤ `start_end`；
   - 方向：起止位移向量夹角 ≤ `direction_deg`；分叉笔画按拐点分段，取各段最大偏差；
   - 形状：重采样点平均点对点距离 ≤ `shape`。
4. **错序检测**：某笔不合格但与**另一笔**参考完全匹配时，判定为 `WRONG_ORDER`。
5. **计分**：单笔按四项指标在 2 倍容差内线性衰减加权（起点 20% / 终点 20% / 方向 30% / 形状 30%），
   整体分 = 单笔均分 × 笔画覆盖率（缺笔/多笔均扣分）。

### 候选字识别算法（`/v1/recognize`）

识别不预设目标字、也不假设笔顺，对每个已注册参考字独立打分：

1. **归一化与重采样**：与校验一致的外接框/画布归一化与 32 点弧长重采样。
2. **代价矩阵**：第 *i* 用户笔与第 *j* 参考笔的单笔代价
   `cost = 1 - 单笔得分/100`（复用上面的四指标加权与分叉笔画容差），范围 `0..1`。
3. **全局一对一最优分配**：在用户笔与参考笔之间求最小权完美匹配
   （O(n³) 匈牙利算法），因此**书写次序与参考不同也能正确配对**——
   这是全局最优，按书写次序贪心配对会失败的场景也能处理。
4. **缺笔/多笔显式惩罚**：把矩阵用虚拟槽扩成边长
   `max(用户笔数, 参考笔数)` 的方阵：参考笔未被对应用户笔匹配即**缺笔**，
   用户笔未被参考笔匹配即**多笔**，每条虚拟边按固定代价（默认 `1.0`，
   `RECOG_MISSING_PENALTY` / `RECOG_EXTRA_PENALTY` 可调）计入总代价。
5. **置信度**：`score = 100 × max(0, 1 − total_cost / max(两笔数))`，
   缺/多笔越多、配对形状越差，分数越低。
6. **排序**：所有参考字按分数降序取 `top_k`；**相同分数按字符升序稳定排序**，
   每个候选都返回笔画对应关系（含各项指标）与缺笔/多笔惩罚明细。

### 分叉/转折笔画容差

折、钩类复合笔画（如“┐”）在拐点处书写误差天然更大。引擎先在参考笔画上检测拐点
（转角 ≥ `corner_angle_deg`），对含拐点的笔画自动放宽：

- 方向容差 `+ fork_extra_direction_deg`（默认 +20°）
- 形状容差 `+ fork_extra_shape`（默认 +0.08）

两项均可在请求体 `tolerances` 中逐次覆盖，实现“可配置的分叉笔画误差”：

```json
{
  "submission_id": "attempt-0002",
  "character": "十",
  "reference_strokes": [[[100,150],[300,150],[300,300]]],
  "strokes": [[[100,150],[300,260],[300,300]]],
  "tolerances": {"fork_extra_direction_deg": 0, "fork_extra_shape": 0}
}
```

## 幂等语义

- `submission_id` 是幂等键：**相同键 + 相同负载** → 返回首个提交的结果，
  响应中 `idempotent_replay: true`，不产生新记录；
- **相同键 + 不同负载** → `409 Conflict`；
- 负载哈希为除 `submission_id` 外全部字段的规范化 JSON 的 SHA-256；
- 历史结果可通过 `GET /v1/submissions/{submission_id}` 查询。

## 配置

服务端默认值通过环境变量设置（见 `docker-compose.yml`），请求体 `tolerances` 可逐字段覆盖：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/app.db` | SQLite 文件路径 |
| `TOL_START_END` | `0.15` | 起止点偏差上限（归一化距离） |
| `TOL_DIRECTION_DEG` | `35` | 方向偏差上限（度） |
| `TOL_SHAPE` | `0.18` | 形状平均偏差上限 |
| `TOL_FORK_EXTRA_DEG` | `20` | 分叉笔画额外方向容差（度） |
| `TOL_FORK_EXTRA_SHAPE` | `0.08` | 分叉笔画额外形状容差 |
| `CORNER_ANGLE_DEG` | `50` | 拐点判定最小转角（度） |
| `RESAMPLE_POINTS` | `32` | 每笔重采样点数 |
| `RECOG_MISSING_PENALTY` | `1.0` | 识别时缺一笔的代价（0..1） |
| `RECOG_EXTRA_PENALTY` | `1.0` | 识别时多一笔的代价（0..1） |

## 项目结构

```
app/
├── main.py        # FastAPI 应用与路由（识别 + 幂等校验 + 练习趋势）
├── models.py      # Pydantic 请求/响应模型
├── recognition.py # 候选字识别引擎（代价矩阵 + 匈牙利全局一对一分配 + 缺/多笔惩罚）
├── validation.py  # 笔顺匹配引擎（方向/起止点/形状/错序/计分）
├── analytics.py   # 练习质量趋势统计（线性插值分位数 + UTC 日期分桶）
├── geometry.py    # 归一化、重采样、拐点检测
├── db.py          # SQLite 持久化（参考字 + 幂等提交 + 一致性快照查询）
└── config.py      # 环境变量配置
tests/test_api.py  # 39 个端到端用例
Dockerfile / docker-compose.yml
```
