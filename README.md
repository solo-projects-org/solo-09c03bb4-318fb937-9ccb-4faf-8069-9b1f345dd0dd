# 发票冲红补样 API

数仓质检补样服务：发票抽查单已下发，对冲红（红冲作废）的票号按批退出样本/备选名单，
并从共享备选池中为各核查专题补样。FastAPI + SQLite，Docker Compose 一键启动，Swagger 可直接操作。

## 功能

- **按轮登记清册**：各核查专题的名额、已下发样本票号、备选票号；备选票可跨专题共享。
- **按批号提交冲红**：本批票号整批退出样本名单与备选名单；仍有效的已发样本照旧。
- **自动补样**：每张票最多占一个名额；用最大二部匹配保证**本批补上的名额总数最大**；
  回执给出各专题的替补票号与剩余缺额；备选不足时本批照样生效，缺额逐专题列在回执中。
- **长链可用**：上千个核查专题逐级换位（A 占的票要让给 B、B 再让给 C……）时，
  增广路用显式栈迭代求解，不会因递归过深报 500；同一份清册按相同顺序提交批次，
  逐专题替补票号与顺序和旧递归实现完全一致（本批用掉哪些票会决定下一批的可用备选）。
- **幂等**：同轮同批号重送同一组票号（与顺序无关）返回原回执（HTTP 200）；换一组票号报 409 冲突。
- **并发一致**：并发冲红在 `BEGIN IMMEDIATE` 写事务中串行生效，结果与按生效顺序（seq）逐批处理一致；
  任一步失败整批回滚。
- **持久化**：SQLite 落盘（Docker volume），重启后仍可查现行名单与回执。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs （Swagger UI）
```

本地运行（Python 3.11+）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # 运行只需 requirements.txt
DB_PATH=./data/app.db .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rounds` | 登记一轮清册（同内容重登幂等返回 200，不同内容 409） |
| GET | `/rounds/{round_id}` | 查现行名单：各专题名额/有效样本/可用备选/缺额 |
| POST | `/rounds/{round_id}/redflush-batches` | 按批号提交冲红票号，返回补样回执 |
| GET | `/rounds/{round_id}/redflush-batches` | 列出本轮全部回执（按生效顺序） |
| GET | `/rounds/{round_id}/redflush-batches/{batch_no}` | 查单个回执 |
| GET | `/health` | 健康检查 |

### 示例

```bash
# 登记清册：备选 C1~C4 跨专题共享
curl -X POST http://localhost:8000/rounds -H 'Content-Type: application/json' -d '{
  "round_id": "R2026-01",
  "topics": [
    {"topic_code": "价税异常",   "quota": 2, "samples": ["A1","A2"], "candidates": ["C1","C2","C3"]},
    {"topic_code": "进销项背离", "quota": 2, "samples": ["B1","B2"], "candidates": ["C2","C3","C4"]},
    {"topic_code": "顶额开具",   "quota": 1, "samples": ["D1"],      "candidates": ["C1","C4"]}
  ]}'

# 提交冲红批 P1
curl -X POST http://localhost:8000/rounds/R2026-01/redflush-batches \
  -H 'Content-Type: application/json' -d '{"batch_no": "P1", "invoice_nos": ["A1","B1"]}'

# 查现行名单与回执
curl http://localhost:8000/rounds/R2026-01
curl http://localhost:8000/rounds/R2026-01/redflush-batches/P1
```

回执示例（节选）：`topics[]` 含各专题 `vacancy_before`（补前缺额）、`replacements`（替补票号）、
`shortfall`（剩余缺额）；`removed` 记录本批退出名单的票号；`unknown_invoices` 为清册外票号。

## 关键设计

- **数据模型**（SQLite）：`rounds`（清册摘要做幂等）、`topics`（名额+登记顺序）、
  `samples`（active 标记现行有效）、`candidates`（available 标记可用，同一票号可出现在多个专题）、
  `batches`（批号主键 + 生效顺序 seq + 规范化票号集合 + 回执原文）。
  按票号、按专题建了覆盖索引，否则长链逐票扫描会把整批处理拖到分钟级。
- **补样最大化**：专题缺额为左部槽位、备选票为右部点的二部图，Kuhn 增广路求最大匹配；
  专题按登记顺序、备选按登记顺序处理，结果确定。增广路用显式栈迭代实现（`app/matching.py`），
  长链换位不会触发 Python 递归上限；迭代版与旧递归版的下标推进、seen 集合与沿途换位顺序一致，
  因此同清册同提交顺序下每批的替补票号与顺序不变。补上的票在所有专题的备选条目中同时失效，
  保证每张票只占一个名额。
- **并发与原子性**：每批处理（查重 → 整批退出 → 算缺额 → 匹配 → 写回执）包在一个
  `BEGIN IMMEDIATE` 事务里，SQLite 写锁把并发批串行化，等价于按生效顺序逐批处理；
  事务内任何异常整体回滚。
- **幂等**：批次的票号集合排序去重后作为幂等键；同键重送直接返回库存回执原文，异键 409。
- **登记校验**：专题代码唯一、名单内不重复、样本数不超名额、同一票号不得跨专题任样本、
  不得既为样本又为备选（备选跨专题共享是允许的）。

## 测试与试跑

```bash
.venv/bin/python -m pytest -q          # 功能 + 长链补满/缺额/后续批次 + 回滚 + 重启持久化 + 真并发一致性
.venv/bin/python scripts/demo.py http://127.0.0.1:8000   # 现场试跑：共享备选/并发冲红/幂等/冲突
```

长链用例：1500 个专题串成一条真正的备选链（Ti 备选 `[Ci, C(i+1)]`，最后一个专题只挂 C1，
补样时必须沿整条链逐级换位；同样的清册用旧递归实现在这里会 500），断言整批补满、
每张票只占一个名额；另一个用例追加一个无备选专题，验证备选不足时本批仍生效、
回执逐专题给出 `shortfall`；后续批次用例在 1200 专题的链上连跑 1+4 批，逐批把回执的
替补票号与顺序同旧递归算法在同样名单上的结果比对，且上一批补回的票会被下一批冲掉。
算法层另有随机用例（迭代版与递归版逐票一致、深链换位、与穷举最优解对照的最大名额）。

并发一致性测试方法：10 个不同批次经真实 HTTP 服务并发提交后，按跑出的生效顺序 seq
在全新数据库上串行重放，断言两者回执与最终名单完全一致；另有同批号同集合并发重送
（仅一次生效）与同批号异集合并发（仅一次生效、其余 409）的用例。
