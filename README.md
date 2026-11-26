# IPv4 组播硬件表演练台（Flask + SQLite + Docker Compose）

交换机硬件组播表快满了。这是一个供值班员**本地演练“试装新 IPv4 组播配置”**的网页：

1. 提交**监听表**：组地址、接收端口、指定源 `(S,G)` 或任意源 `(*,G)`；
2. 提交**回放流量**：源、组、帧数；
3. 填两项上限：**硬件表项条数**、**回放 CPU 帧总数**；
4. 后端自动求一份正确且同时守住两项上限的硬件配置并“模拟发布”；
5. 无解或模拟发布失败时**报错并继续使用 SQLite 中已发布的整份配置**；
6. 网页可查看已装规则、跑整轮回放、并对**任意源和组**试发一帧，显示实际出端口、
   硬件还是 CPU 转发、以及回放的 **CPU 帧总数**。

> 关键硬件约束：一条表项只能指定 **0～2 个出端口**（0 个即硬件丢弃），否则动作只能是
> “交 CPU”。因此需要发往 **3 个端口**的组无法被单条硬件表项正确编码，必须走 CPU。

---

## 1. 快速开始

### 方式 A：Docker Compose（推荐）

```bash
docker compose up --build
# 主入口 http://localhost:8000（可用环境变量 WEB_PORT 改宿主机端口）
# 若还想用传统 Flask 的宿主机 5000 端口，取消 compose.yaml 中对应映射的注释。
```

- SQLite 数据保存在命名卷 `drill-data` 的 `/data/drill.db`，重启不丢已发布版本；
- 自带 `HEALTHCHECK`（`GET /healthz`）；
- 可选环境变量：`WEB_PORT`、`FLASK_HOST_PORT`、`PUBLISH_DELAY`（默认 0.6s，模拟换版窗口）、
  `PUBLISH_FAIL_RATE`（默认 0）。

### 方式 B：本地 Python（3.10+）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python wsgi.py                 # http://127.0.0.1:8000 （线程模式）
# 或：gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 wsgi:app
```

首次启动若 SQLite 中没有已发布配置，会自动播种一份示例 v1（其中含需要发往
`1,2,3` 三个端口的组 `239.10.2.2`）。

### 跑测试与冒烟实测

```bash
python -m unittest discover -s tests -v   # 36 个测试
python scripts/smoke.py http://127.0.0.1:8000   # 零依赖端到端冒烟
```

---

## 2. 值班员操作路径（对应验收动作）

1. 打开 <http://localhost:8000/>，表单已预填“三端口组”示例；也可点顶部示例按钮：
   **三端口组 / 混合 S,G/\*,G / 无解场景 / 下发失败**。
2. 点“求解配置，守得住两项上限即发布”：
   - 成功 → 跳到新版本详情，SQLite 新增一条 `published`；
   - 无解（422）或模拟发布失败（502）→ 红色报错并明确提示
     “继续使用 SQLite 中已发布的配置”，顶部活动版本不变。
3. 打开 **已装规则**：查看 `(*,G)` / `(S,G)` 匹配方式与出端口或 `cpu` 动作，
   以及求解器的分组决策。
4. 打开 **回放 / 试发**：
   - “开始整轮回放”：每条流显示命中的硬件匹配、转发方式、实际端口、监听要求端口、
     是否完全一致；顶部卡片给出**回放 CPU 帧总数**与上限对比；
   - “试发一帧”：源、组可任意填写（包括监听表和回放里都不存在的地址），
     返回实际出端口、硬件/CPU 路径与一致性结论。
5. **版本记录**：`published / no_solution / publish_failed / invalid_rules`
   全部留痕；只有最新的 `published` 是活动配置。

### 验收：试跑需发往三个端口的组播帧

全新库的活动 v1 中，监听表有 `(*,239.10.2.2) -> {1,2,3}`，且没有对应硬件表项。
对一个**任意的、未列出的源**试发：

```bash
curl -s -X POST localhost:8000/api/send -H 'Content-Type: application/json' \
  -d '{"source":"198.51.100.66","group":"239.10.2.2"}'
```

实测返回（节选自真实运行）：

```json
{"frame": {
  "source": "198.51.100.66", "group": "239.10.2.2",
  "via": "cpu", "cpu_reason": "miss",
  "hw_match": null, "listening_match": "*,G",
  "actual_ports": ["1", "2", "3"],
  "required_ports": ["1", "2", "3"],
  "correct": true},
 "version": 1, "ok": true}
```

该版本整轮回放的真实汇总：

```text
total_frames=1000  hardware_frames=700  cpu_frames=300  all_correct=True
 10.10.0.11 -> 239.10.1.1 n=120 via=hardware hw=S,G actual=['3','4']   ok
 10.10.0.77 -> 239.10.1.1 n=80  via=hardware hw=*,G actual=['1','2']   ok
 10.10.0.99 -> 239.10.1.1 n=500 via=hardware hw=*,G actual=['1','2']   ok
 10.10.0.21 -> 239.10.2.2 n=300 via=cpu      actual=['1','2','3']      ok
```

`239.10.2.2` 的 300 帧是唯一的 CPU 流量，恰好等于 CPU 上限 300。

---

## 3. 转发与正确性模型

匹配优先级在硬件表和监听表中一致：**精确 `(S,G)` 优先于 `(*,G)`**。

对帧 `(S,G)`：

1. 命中硬件 `(S,G)` → 执行其动作；否则命中硬件 `(*,G)` → 执行其动作；
2. 硬件动作是端口集 `P`（`0 ≤ |P| ≤ 2`）→ 实际端口即 `P`，标记**硬件转发**；
3. 硬件动作是 `cpu`，或硬件未命中 → **交 CPU**（分别记录 `hw_action` / `miss`），
   CPU 查监听表（同样 `(S,G)` 优先）得到端口集合并转发；监听表无项时为空集（丢弃）；
4. 正确性判据是**集合完全相等**：`actual_ports == required_ports`（与顺序无关）。

回放中同一 `(S,G)` 流内每一帧决策相同，因此
`cpu_frames = Σ 帧数 × (该流是否走 CPU)`，即按帧数加权，而不是 punt 流的条数。

### “回放未列出的源也照此处理”

- 试发表单/`POST /api/send` 允许任意合法 IPv4 源与组；
- 求解器保证配置对**所有 IPv4 源**正确，而不只是回放里出现的源。原因是转发行为只会对
  “精确监听源 / 精确硬件源”发生分化，其余所有源共享同一条通用路径。
  `app/dataplane.py:audit_all_sources` 对每个组枚举这些精确源，再加一个保证未被使用的
  “通用源”探针；单测 `test_audit_*` 与每次成功求解后的内置断言都会执行该审计。

---

## 4. 求解器：怎样算“有配法”

代码：`app/solver.py`。目标：在 `表项数 ≤ H`、`回放 CPU 帧 ≤ C` 下找到正确配置；
因为“硬件表快满了”，在可行解中**优先最少表项，再最小化 CPU 帧**。

对每个组 `G`，记 `D` 为 `(*,G)` 监听端口集（无则 `∅`），`R(S)` 为源 `S` 实际要求的端口集。
所有有意义的正确配置都被两类策略覆盖：

- **S（只装 `(S,G)`）**：任何 `|R(S)| ≤ 2` 的源可用 1 条 `(S,G)` 端口表项 offload，
  其余源未命中交 CPU（天然正确）。每条表项成本都是 1，所以装 `k` 条时最优是选
  回放权重最大的 `k` 个源，得到每 `k` 一个候选。
- **H（`(*,G)` 聚合）**：仅当 `|D| ≤ 2` 时可装 `(*,G)→D`；所有满足 `R(S) ≠ D` 的
  精确监听源**必须**补更精确的 `(S,G)` 表项，否则会被聚合项错误转发：
  `|R(S)| ≤ 2` 装端口项，`|R(S)| > 2`（如三端口源）只能装 `cpu` 项。
  该策略的表项数与 CPU 数都是定值。

每组候选先按 `(entries, cpu)` 做 Pareto 剪枝，再用一个以“已用硬件表项数”为键的 DP
跨组组合；超过 `H` 或中途 CPU 已超过 `C` 的状态直接剪除。DP 非空即“有配法”，
取满足 CPU 上限的最小表项数。无解时给出可操作诊断，例如：

```text
不存在同时满足两项上限的配置： 即使硬件表项不限量，最少仍有 5000 帧必须由 CPU 转发
（接收端口超过 2 个的组无法编码进单条硬件表项），高于 CPU 上限 100。
```

混合示例（`H=4, C=700`）求出的 4 条规则，完整演示了两级匹配与重端口例外：

```text
*,G  *           239.1.1.1  ports ['1','2']     # 聚合，默认源硬件转发
S,G  10.1.1.1    239.1.1.1  ports ['3','4']     # 更精确，优先
S,G  10.1.1.2    239.1.1.1  cpu []              # 要求 3 端口，只能 punt
*,G  *           239.1.1.2  ports ['5','6']
```

实测回放：`total=15700 hardware=15000 cpu=700 all_correct=True`。

---

## 5. 发布、回滚与“换版期间仍可回放”

- SQLite 只有 `revisions` 与 `replay_runs` 两张表；活动配置 =
  `status='published'` 中 `id` 最大的一行，整份配置以 JSON 存于一行。
- 提交流程（`app/services.py:DrillService.submit_drill`）：
  校验 → 求解（或校验 API 传入的手工规则）→ 调 `Publisher.push()` 模拟下发 →
  成功才写新的 `published` 行。
- `simulate_fail=true`（页面复选框 / JSON 字段，或 `PUBLISH_FAIL_RATE>0`）时，
  推送抛 `PublishError`，后端写一条 `publish_failed` 审计行，**不产生新的 published**，
  活动版本原样保留；无解写 `no_solution`，同样不动活动版本。
- 发布由进程内 `publish_lock` 串行化（容器固定 `gunicorn -w 1 --threads 8`）。
- **回放不拿发布锁**：`run_replay()` 在开始时只读一次最新 published 行，之后整轮
  （所有流）都用这份内存中的完整快照计算，并把所用 `revision_id/version` 写入
  `replay_runs`。因此换版进行中回放仍可服务，且不会混用新旧两份规则。

实测时序（发布人为加 1.2s 延迟，在 t+0.5s 发起回放）：

```text
mid-switch replay at t+0.5s: -> rev=#4 v2 cpu_frames=700   # 仍是换版前整份快照
publish done               : -> rev=#5 v3 entries=0 cpu=120
replay after switch        : -> rev=#5 v3 cpu_frames=120
```

另有确定性并发测试 `tests/test_concurrency.py`：用事件把发布卡在设备推送阶段，
断言期间 `/api/replay`、`/api/send` 都在 2s 内返回且仍用修订 #1，放行后 v2 才生效。

---

## 6. HTTP / JSON API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查，返回活动修订 id |
| GET | `/` `/rules` `/replay` `/revisions` `/revisions/<id>` | 服务端渲染页面 |
| POST | `/drills` `/replay/run` `/send` | 表单提交（PRG/直接渲染错误） |
| GET | `/api/config?revision_id=` | 活动（或指定已发布）整份快照 |
| POST | `/api/drills` | 求解并发布；body 可带 `"simulate_fail": true`，也可带手工 `"rules"` |
| POST | `/api/replay` | `{}` 用快照自带流量；也可 `{"flows":[...]}` 临时回放 |
| POST | `/api/send` | `{"source","group","revision_id"?}` 试发一帧 |
| POST | `/api/evaluate` | 无状态评估任意 listening/rules/flows（含全源审计） |
| GET | `/api/revisions` `/api/examples` | 审计列表 / 内置示例 |

状态码约定：成功 `200/201`；输入错误 `400 invalid_input`；
无解 `409 no_solution`；模拟发布失败 `502 publish_failed`；手工规则非法 `422 invalid_rules`。
错误响应都带 `active_revision_id`，便于调用方确认兜底版本。

---

## 7. 目录结构

```text
workspace/
├── app/
│   ├── __init__.py        # Flask 工厂、SQLite/服务装配、首启播种
│   ├── netutils.py        # IPv4/组播/端口解析与结构化校验
│   ├── model.py           # payload 规范化、逐行错误收集
│   ├── dataplane.py       # 两级匹配、单帧转发、回放统计、全源审计
│   ├── solver.py          # 分组策略 + 硬件预算 DP
│   ├── storage.py         # SQLite（revisions / replay_runs）
│   ├── publisher.py       # 模拟设备下发（延迟 + 失败注入）
│   ├── services.py        # 求解/发布事务、快照回放、试发
│   ├── examples.py        # 四个演示场景 + 种子
│   ├── routes.py          # 页面与 JSON API
│   ├── templates/         # Jinja 页面
│   └── static/            # style.css / app.js / favicon
├── scripts/smoke.py       # 零依赖端到端冒烟
├── tests/                 # 36 个 unittest（数据面/求解器/API/Web/并发）
├── Dockerfile
├── compose.yaml
├── requirements.txt
└── wsgi.py
```

---

## 8. 实测与核对结论

- `python -m unittest discover -s tests`：**36/36 通过**，覆盖
  两级匹配优先级、未命中/CPU 动作、端口集合完全一致、错误规则识别、
  三/四端口组、DP 上限不可行、0 端口丢弃项、全源审计、
  API 全流程、Web 表单、失败回滚、换版并发快照。
- 用 gunicorn（与镜像同一启动表达式）实际起服务并跑 `scripts/smoke.py`：
  **14/14 PASS**；全新 SQLite 首启播种 `revision #1`。
- 已用真实 HTTP 调用核对：三端口任意源试发走 CPU 且端口恰为 `[1,2,3]`；
  种子回放 CPU 总数 300；无解 409 与发布失败 502 后活动版本均不变；
  换版窗口内回放继续使用旧整份快照。
- `compose.yaml` 已通过 YAML 解析与字段核对，healthcheck 命令在运行中的服务上实测 rc=0。
- `git status` 中初始提交为空归档，以上均为本次新增/改动，可用 `git diff`/`git status` 核对。

### 余项 / 边界（如实说明）

1. **本沙箱内没有 docker / podman CLI 与守护进程**，因此
   `docker compose up --build` 没能在这里真正执行；Dockerfile 基于官方
   `python:3.12-slim`、依赖仅 Flask/gunicorn，启动命令与 healthcheck 已用等价的
   本地 gunicorn 进程实测。镜像构建与 compose 拉起建议在有 Docker 的环境最终确认一次。
2. “发布到交换机”是**模拟动作**（延迟 + 显式/随机失败注入），没有对接真实
   gNMI/NETCONF/SDK；对接时只需替换 `app/publisher.py:Publisher.push`，
   并把“成功后再写 published”的事务边界后移到设备确认之后。
3. 端口以字符串标识（`1` 与 `Eth1` 视为不同端口），未建模 VLAN/隧道/成员口层级；
   未做鉴权与多值班员权限——定位是**本地演练页**，不要直接暴露到生产网络。
4. DP 规模按演练数据量设计（输入上限 500 行）；若未来单组精确源/流数量很大，
   可把“top-k + DP”进一步换成按组的背包或增量求解。
