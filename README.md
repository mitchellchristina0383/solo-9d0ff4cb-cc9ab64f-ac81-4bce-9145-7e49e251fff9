# OOM 归属核对台

Linux 服务被 OOM 杀死后，值班员需要判断是**服务自身 cgroup 超限**还是**整机内存不足**，
并定位到具体是哪一次服务运行。本工具用 Flask + SQLite 实现：

1. 分开导入一台主机的**运行记录**与**内核 OOM 摘要**两份 CSV；
2. 自动把每个 OOM 事件归属到唯一匹配的运行记录；
3. 值班员在页面上确认归属与触发范围后派查；
4. 补录或按编号更正数据后自动重核：归属或触发范围变化的事件，其原确认被撤销，
   旧页面提交的确认请求被拒绝，其他事件不受影响；
5. 全部结论与确认状态落 SQLite，重启后可继续查。

## 运行

### Docker Compose（推荐）

```bash
docker compose up -d --build
# 打开 http://localhost:8000
```

- 数据保存在命名卷 `oom-data`（容器内 `/data/oom.db`），重启、重建容器均不丢。
- 可用 `SECRET_KEY=xxx docker compose up -d` 覆盖会话密钥。

### 本地开发

```bash
pip install -r requirements.txt   # 仅运行只需 flask
python run.py                     # http://localhost:8000
```

数据库路径用环境变量 `OOM_DB` 指定，默认 `<instance>/oom.db`。

## CSV 格式

UTF-8 编码，首行为表头。时刻均为**该次启动后的毫秒**。

**运行记录** `run_no,boot_id,pid,cgroup,start_ms,end_ms`

- `run_no` 运行号（全局唯一）；`boot_id` 主机启动 ID；`pid` 主进程 PID；
- `cgroup` 服务所在 cgroup；`[start_ms, end_ms)` 为起止时刻，**左闭右开**。

**OOM 摘要** `event_no,boot_id,pid,cgroup,time_ms,scope`

- `event_no` 事件号（全局唯一）；`pid` 被杀 PID；`cgroup` 被杀进程所在 cgroup；
- `scope` 触发范围：`memcg`（触发组）或 `host`（整机）。

导入按编号 upsert：**新编号 = 补录，已有编号 = 更正**。任一行不合法则整文件不写入
（原子导入）。也可在「运行记录」「OOM 摘要」页面按编号单条更正/补录。
样例见 `sample_data/`。

## 归属规则

事件 E 匹配运行记录 R 当且仅当：

```
R.boot_id = E.boot_id  AND  R.pid = E.pid  AND  R.cgroup = E.cgroup
AND  R.start_ms <= E.time_ms < R.end_ms
```

- **恰好 1 条匹配** → 页面列出运行号与触发范围，值班员确认后派查；
- **0 条或多条** → 标为「待核」（并显示候选数），不能确认。

PID 复用由「启动 ID + 左闭右开时段」区分；同 PID 不同 cgroup 由 cgroup 字段区分；
跨重启由 boot_id 隔离。

## 确认、撤销与防旧页面

- 每个唯一匹配计算指纹 `sha256(event_no|run_no|scope)`，确认按钮携带该指纹提交。
- 任何导入/更正后**全量重核**所有事件：归属（运行号）或触发范围（scope）变化
  → 该事件的有效确认被撤销（保留历史与原因），页面提示；指纹不变则确认保留。
- 确认请求到达时重新比对当前指纹：不一致（即旧页面）→ 拒绝并提示刷新。
- 数据库部分唯一索引保证同一事件至多一条有效确认（并发兜底）。

## 实现结构

```
app/
  __init__.py   应用工厂；启动时建表并重核一次
  db.py         SQLite 连接（WAL、外键、busy timeout）与建表
  core.py       匹配、重核/撤销、确认、CSV 解析与 upsert（不依赖 Flask）
  views.py      页面与表单路由
  templates/    事件核对 / 导入 / 运行记录 / OOM 摘要 / 更正表单
run.py          本地入口（gunicorn 用 run:app）
tests/selftest.py  自测脚本
```

表：`runs`、`events`、`attributions`（重核结论）、`confirmations`（确认/撤销历史）。

## 实测结果

`python3 tests/selftest.py`（Flask test_client，临时库）：**51 项检查全部通过**，覆盖：

- 匹配：PID 复用相邻两段、左闭（t=start 命中）、右开（t=end 不命中）、
  同 PID 不同 cgroup 区分、跨 boot_id 隔离、0 匹配与 2 匹配均为待核；
- 确认：唯一匹配可确认、重复确认幂等、待核拒绝、伪造指纹拒绝；
- 更正后重核：运行记录更正使事件变待核 → 撤销；补录新记录使归属 run8→run9
  → 撤销，旧指纹确认被拒、新指纹确认成功；事件触发范围 memcg→host → 撤销；
  归属未变的事件确认保留；无关事件确认照旧；
- 导入：非法 scope 整文件拒绝且零写入；CSV 按编号更正生效；
- 重启：新建应用实例后确认状态、撤销记录、最新归属结论均在。

另用样例数据对真实 HTTP 服务做了冒烟：导入 → 确认事件 101 → 更正 run4 使事件 104
归属 run4 → 杀掉进程重启 → 确认状态与最新结论保留。

Docker 部分：本环境无 docker，`docker-compose.yml` 已做 YAML 结构校验，
Dockerfile 经静态核对（依赖仅 flask+gunicorn，`run:app` 为模块级应用对象），
**未实际构建镜像**，请在有 docker 的机器上执行 `docker compose up -d --build` 验证。

## 余项 / 限制

- 无登录鉴权，假定部署在内网；如需可加反向代理 Basic Auth。
- 编号（run_no / event_no）全局唯一，多主机导入需各自编号段（或后续加主机维度）。
- 每次变更全量重核，万级事件以内无压力；更大数据量可改为按 boot_id 增量重核。
- 不支持删除记录（只能更正覆盖）；误录的编号可更正为占位值。
- scope 仅接受 `memcg`/`host` 两个标记，其他内核来源的表述需先归一化。
- 生产用 gunicorn 多进程 + SQLite WAL 足够本场景；更高并发需换外部数据库。
