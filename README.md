# AIS NMEA 0183 报文解码 API

基于 **Python 3.11+ / FastAPI / Pydantic v2** 的 AIS（ITU-R M.1371）解码服务。
`POST /decode` 接收同一批次、带 `source_id` 的原始 `!AIVDM` / `!AIVDO` 语句，
完成校验、乱序分片重组、歧义枚举与六位 payload 解码。
`POST /ingest` 在此基础上携带本批 UTC 观测时间 `observed_at`，把成功解码的消息
按 `(source_id, MMSI)` 持久化为可查询的船舶快照（SQLite），并提供 `GET /vessels` 查询。

## 功能

- **逐句校验**（校验失败不影响同批其他语句，逐条返回错误）：
  - 帧结构（`!` 前缀、`*HH` 校验和字段）
  - **XOR 校验和**（`!` 与 `*` 之间所有字符异或）
  - **分片总数 / 分片序号**（1–9，序号 ≤ 总数）
  - **fill bits**（0–5；非末分片必须为 0，保证六位边界对齐）
  - 频道（`A`/`B`，兼容 `1`/`2` 自动归一化）、六位字符集合法性
- **乱序归组重组**：
  - 带非空 `sequence_id` 的多片语句按 `(source_id, channel, sequence_id, 分片总数)` 归组，按分片序号重组；
  - `sequence_id` 为空的孤儿分片按 `(source_id, channel, 分片总数)` 入池，**枚举不复用语句的完整组合**：
    唯一 → 解码；多解 → **HTTP 409** 并在 `candidates` 中并列全部候选；缺片 → 返回 `missing_fragments` 缺失序号；
  - 同批完全相同（同 `source_id` 同原文）的重发语句自动去重。
- **六位 payload 解码**：
  - 消息 **1/2/3**（A 类船位报告）：MMSI、经纬度、航速（SOG）、航向（COG/船首向）、转向率、航行状态、时间戳等；
  - 消息 **5**（静态与航次数据）：船名、呼号、IMO、船型、尺度、ETA、吃水、目的地；
  - 标准“不可用”哨兵值一律输出为 `null`（见下表）；
  - **位长校验**：消息 1/2/3 必须恰好 168 bit、消息 5 必须恰好 424 bit，不符即拒绝；
    不支持的消息类型返回 `unsupported message type` 错误。
- **船舶快照（`/ingest` + `/vessels`）**：
  - 按 `(source_id, MMSI)` 分别保存**最新船位消息 1/2/3** 与**最新静态航次消息 5** 两条独立时间线；
  - 每类消息按批次 `observed_at` 比较：**旧批不得回滚新状态**；**同时间先保存者优先**；
    **同批多条同类消息按原语句下标取首条**；
  - 整条消息原样保存：新船位坐标为 `null` 时保留 `null`，**不借用历史坐标**；
  - 错误、缺片与歧义候选**不写入**快照；歧义批仍返回 409，但其中明确解码的消息**仍更新**快照；
  - 本地 **SQLite** 持久化，Compose 重启后仍可查询。
- 交互式 API 文档：`/docs`（Swagger UI）、`/redoc`。

## 快速开始

### Docker（一条命令）

```bash
docker compose up --build
```

服务监听 **http://localhost:8000**（`docker-compose.yml` 将宿主机 8000 映射到容器 8000）。
健康检查：`GET /health`。

快照数据库通过环境变量 `AIS_DB_PATH` 定位；Compose 将其设为 `/data/ais.db` 并挂载
命名卷 `ais-data`，因此 `docker compose restart`（乃至 `down` + `up`）后快照仍可查询。
本地直接运行时默认写入当前目录的 `ais_snapshots.db`。

### 本地运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q          # 57 个单元/集成测试
```

## API

### `POST /decode`

请求体：

```json
{
  "sentences": [
    { "source_id": "ais-receiver-1", "sentence": "!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C" }
  ]
}
```

- `source_id`（必填）：接收机/数据源标识，是分片归组键的一部分；
- `sentence`（必填）：完整原始语句，含 `*HH` 校验和。

HTTP 状态码：

| 状态码 | 含义 |
| ------ | ---- |
| `200`  | 批次处理完成，无歧义分组（各结果状态见 `results[].status`） |
| `409`  | 至少一个无 `sequence_id` 分片池存在多个完整组合，候选见 `results[].candidates` |
| `422`  | 请求体不符合 schema（如空批次、缺字段） |

`results[].status` 取值：

| status | 含义 |
| ------ | ---- |
| `decoded`    | 成功解码，`decoded` 为消息体 |
| `incomplete` | 缺片，`missing_fragments` 列出缺失的分片序号 |
| `ambiguous`  | 多解，`candidates` 并列全部完整组合（此时整体响应为 409） |
| `error`      | 语句校验失败或解码失败，`error` 为机器可读错误码，`detail` 为说明 |

### `POST /ingest`

与 `/decode` 相同的语句列表，外加本批 UTC 观测时间 `observed_at`；完成同样的
校验/去重/重组/解码，并**把成功解码的消息写入持久快照**。请求体：

```json
{
  "observed_at": "2026-10-05T08:30:00Z",
  "sentences": [
    { "source_id": "ais-receiver-1", "sentence": "!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C" }
  ]
}
```

- `observed_at`（必填）：本批观测时间，ISO 8601；无时区按 UTC 处理，带偏移会换算为 UTC；
- `sentences`（必填）：同 `/decode`。

响应在 `/decode` 的 `summary` / `results` 之外增加 `observed_at` 与 `snapshots`
（本批涉及船舶的**合并快照**，即写入完成后的最新持久状态）。HTTP 状态码语义与
`/decode` 一致：`200` 正常，`409` 存在歧义分组（**明确解码的消息仍会更新快照**），
`422` 请求体不合法。

快照更新规则（按 `(source_id, MMSI)` 与消息类分别比较）：

| 规则 | 行为 |
| ---- | ---- |
| 新旧比较 | 仅当新消息的 `observed_at` **严格更新**才覆盖；旧批不得回滚新状态 |
| 同时间 | **先保存者优先**（后到的同时间批次不覆盖） |
| 同批多条同类 | 按原语句下标取**首条** |
| null 坐标 | 新船位坐标为 `null` 时保留 `null`，不借用历史坐标 |
| 不写入 | 错误、缺片、歧义候选不写入快照 |

### `GET /vessels`

按来源和 MMSI 查询已保存的合并快照；两个查询参数均可选（省略则列出全部）：

```
GET /vessels?source_id=ais-receiver-1&mmsi=366999999
```

返回 `VesselSnapshot` 数组，按 `(source_id, mmsi)` 排序。每个快照结构：

```json
{
  "source_id": "ais-receiver-1",
  "mmsi": 366999999,
  "position": {
    "observed_at": "2026-10-05T09:00:00Z",
    "data": { "message_type": 1, "mmsi": 366999999, "longitude": null, "latitude": 40.7484, "...": "..." }
  },
  "static": {
    "observed_at": "2026-10-05T08:30:00Z",
    "data": { "message_type": 5, "mmsi": 366999999, "name": "MV CLAUDE", "destination": "NEW YORK", "...": "..." }
  }
}
```

`position`（消息 1/2/3）与 `static`（消息 5）是两条独立时间线，各自携带写入时的
批次 `observed_at`；对应消息类尚未解码到时该侧为 `null`。

### 哨兵值 → `null`

| 字段 | 原始哨兵值 |
| ---- | ---------- |
| `rate_of_turn` | -128（0x80） |
| `speed_over_ground` | 1023 |
| `longitude` | 181°（0x6791AC0） |
| `latitude` | 91°（0x3412140） |
| `course_over_ground` | 3600 |
| `true_heading` | 511 |
| `timestamp` | 60 |
| `imo_number` / `ship_type` / `draught` | 0 |
| `eta_month` / `eta_day` | 0 |
| `eta_hour` / `eta_minute` | 24 / 60 |

## 示例

以下示例报文由 `scripts/generate_examples.py` 生成（可重复执行验证），响应为实际服务输出。

### 1. 单片解码（消息 1）

```bash
curl -s -X POST http://localhost:8000/decode \
  -H 'Content-Type: application/json' \
  -d '{
    "sentences": [
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C"}
    ]
  }'
```

响应 `200`（节选）：

```json
{
  "results": [
    {
      "source_id": "ais-receiver-1",
      "status": "decoded",
      "message_type": 1,
      "decoded": {
        "message_type": 1,
        "mmsi": 366999999,
        "navigation_status": 0,
        "speed_over_ground": 8.4,
        "longitude": -73.9835,
        "latitude": 40.7484,
        "course_over_ground": 121.5,
        "true_heading": 119,
        "timestamp": 34
      }
    }
  ]
}
```

### 2. 多片乱序重组（消息 5，分片 2 先于分片 1 到达）

```bash
curl -s -X POST http://localhost:8000/decode \
  -H 'Content-Type: application/json' \
  -d '{
    "sentences": [
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,2,2,4,A,NECQEp6ClRh000000000000,2*1F"},
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,2,1,4,A,55Mwqgh2:N2UL@?7;?@mJ0<h5D@D000000000016?0N:<6Lf,0*5E"}
    ]
  }'
```

响应 `200`（节选）——两片按 `sequence_id=4` 归组、按序号重组后解码：

```json
{
  "results": [
    {
      "status": "decoded",
      "sequence_id": "4",
      "total_fragments": 2,
      "received_fragments": [1, 2],
      "message_type": 5,
      "decoded": {
        "mmsi": 366999999,
        "imo_number": 9074729,
        "call_sign": "WDC1234",
        "name": "MV CLAUDE",
        "ship_type": 70,
        "draught": 8.5,
        "destination": "NEW YORK"
      }
    }
  ]
}
```

### 3. 歧义（无 sequence_id，两个分片 1 共享一个分片 2）→ 409

```bash
curl -s -X POST http://localhost:8000/decode \
  -H 'Content-Type: application/json' \
  -d '{
    "sentences": [
      {"source_id": "ais-receiver-2",
       "sentence": "!AIVDM,2,1,,B,55M2oP@2:N0l4447774pu9@PE8r1=@5800000016<PD886P`,0*40"},
      {"source_id": "ais-receiver-2",
       "sentence": "!AIVDM,2,1,,B,55M2oPP2:N0p888;;;9<uE@PE8r0=8u=<000001@;@I796i9,0*43"},
      {"source_id": "ais-receiver-2",
       "sentence": "!AIVDM,2,2,,B,0B0Slm3kP00000000000000,2*3F"}
    ]
  }'
```

响应 **`409`**（节选）——两个完整组合都成立，并列候选，不做猜测：

```json
{
  "results": [
    {
      "status": "ambiguous",
      "detail": "2 complete combinations are possible for fragment pool ...",
      "candidates": [
        {
          "sentence_indexes": [0, 2],
          "message_type": 5,
          "decoded": { "mmsi": 366000001, "name": "NORTHERN STAR", "destination": "BOSTON" }
        },
        {
          "sentence_indexes": [1, 2],
          "message_type": 5,
          "decoded": { "mmsi": 366000002, "name": "SOUTHERN CROSS", "destination": "MIAMI" }
        }
      ]
    }
  ]
}
```

### 4. 缺片（sequence_id=9 的分片 2 未到达）

```bash
curl -s -X POST http://localhost:8000/decode \
  -H 'Content-Type: application/json' \
  -d '{
    "sentences": [
      {"source_id": "ais-receiver-3",
       "sentence": "!AIVDM,2,1,9,A,55Mwqgh2:N2UL@?7;?@mJ0<h5D@D000000000016?0N:<6Lf,0*53"}
    ]
  }'
```

响应 `200`：

```json
{
  "results": [
    {
      "status": "incomplete",
      "sequence_id": "9",
      "total_fragments": 2,
      "received_fragments": [1],
      "missing_fragments": [2],
      "detail": "missing fragment(s) 2 of 2"
    }
  ]
}
```

### 5. 写入快照并查询（`/ingest` + `/vessels`）

```bash
curl -s -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "observed_at": "2026-10-05T08:30:00Z",
    "sentences": [
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,1,1,,A,15Mwqgh01DreE9HGD@44gkg40000,0*1C"},
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,2,2,4,A,NECQEp6ClRh000000000000,2*1F"},
      {"source_id": "ais-receiver-1",
       "sentence": "!AIVDM,2,1,4,A,55Mwqgh2:N2UL@?7;?@mJ0<h5D@D000000000016?0N:<6Lf,0*5E"}
    ]
  }'
```

响应 `200`（节选）——`results` 与 `/decode` 相同，`snapshots` 给出本批涉及船舶的合并快照：

```json
{
  "observed_at": "2026-10-05T08:30:00Z",
  "summary": { "total_sentences": 3, "decoded": 2 },
  "snapshots": [
    {
      "source_id": "ais-receiver-1",
      "mmsi": 366999999,
      "position": {
        "observed_at": "2026-10-05T08:30:00Z",
        "data": { "message_type": 1, "longitude": -73.9835, "latitude": 40.7484, "speed_over_ground": 8.4 }
      },
      "static": {
        "observed_at": "2026-10-05T08:30:00Z",
        "data": { "message_type": 5, "name": "MV CLAUDE", "destination": "NEW YORK" }
      }
    }
  ]
}
```

随后任意时刻按来源和 MMSI 查询（重启后仍有效）：

```bash
curl -s 'http://localhost:8000/vessels?source_id=ais-receiver-1&mmsi=366999999'
```

```json
[
  {
    "source_id": "ais-receiver-1",
    "mmsi": 366999999,
    "position": { "observed_at": "2026-10-05T08:30:00Z", "data": { "longitude": -73.9835, "...": "..." } },
    "static":   { "observed_at": "2026-10-05T08:30:00Z", "data": { "name": "MV CLAUDE", "...": "..." } }
  }
]
```

时序行为示例：先以 `observed_at=2026-10-05T08:30:00Z` 写入船位后，再以
`observed_at=2026-10-05T08:00:00Z`（更早）提交同一船舶的另一船位——快照保持
`08:30` 的状态不变；而以 `observed_at=2026-10-05T09:00:00Z` 提交坐标为 `null`
的 `!AIVDO` 船位后，快照坐标即为 `null`（不回填历史坐标）。

## 设计说明

- **归组键**：多片归组严格限定在同一 `source_id`、同一频道内；不同接收机/频道的同名
  `sequence_id` 不会混淆。
- **歧义语义**：无 `sequence_id` 时，服务对分片池做笛卡尔枚举，每个候选组合中同一物理
  语句至多使用一次；组合唯一才解码，否则整体返回 409 并列出全部候选（候选数上限 100，
  超出时 `detail` 会注明截断）。带 `sequence_id` 的组内若同序号分片内容冲突，返回
  `conflicting_fragments` 错误而非猜测。
- **位长严格性**：六位 payload 去掉 fill bits 后，消息 1/2/3 必须 168 bit、消息 5 必须
  424 bit；任何更长/更短的报文都以 `decode_error` 拒绝，避免输出错位字段。
- **解码正确性**：解码器已与参考实现 `pyais` 交叉验证（800 条随机消息 1/5 逐字段一致）。
- **快照存储**：每 `(source_id, MMSI, 消息类)` 一行 SQLite 记录，`observed_at` 归一化为
  定宽 UTC ISO 8601 文本，覆盖条件为 SQL 级 `excluded.observed_at > 已存值` 的原子
  upsert——严格更新才覆盖，天然实现“旧批不回滚、同时间先保存者优先”；`/ingest` 按
  语句下标顺序应用本批解码结果，因此同批同类首条生效。快照保存整条解码消息，
  不做字段级合并（null 坐标原样保留）。

## 项目结构

```
app/
  main.py        # FastAPI 入口，POST /decode、POST /ingest、GET /vessels、GET /health
  models.py      # Pydantic 请求/响应模型（含快照模型）
  nmea.py        # NMEA 0183 语句解析与校验（XOR 校验和、分片、fill bits）
  ais.py         # 六位 armour、位提取、消息 1/2/3 与 5 解码
  assembler.py   # 归组、乱序重组、孤儿分片池歧义枚举
  storage.py     # SQLite 快照存储（按 source_id+MMSI+消息类的最新值 upsert）
scripts/
  generate_examples.py  # 重新生成 README/测试中的示例报文
tests/           # pytest 单元与 API 集成测试（含快照时序语义）
Dockerfile
docker-compose.yml      # 命名卷 ais-data 持久化 /data/ais.db
requirements.txt / requirements-dev.txt
```
