"""归属匹配、重核撤销、CSV 解析与确认逻辑（不依赖 Flask，便于自测）。"""
import csv
import hashlib
import io
from datetime import datetime, timezone

SCOPES = ("memcg", "host")  # memcg=触发组, host=整机

RUN_COLUMNS = ["run_no", "boot_id", "pid", "cgroup", "start_ms", "end_ms"]
EVENT_COLUMNS = ["event_no", "boot_id", "pid", "cgroup", "time_ms", "scope"]


class DataError(ValueError):
    """CSV 或表单数据不合法。"""


class ConfirmRejected(Exception):
    """确认请求被拒绝（待核或页面已过期）。"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fingerprint(event_no, run_no, scope):
    """确认指纹：归属（运行号）或触发范围任一变化都会使指纹失效。"""
    raw = "{}|{}|{}".format(event_no, run_no, scope)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 归属匹配

def find_matches(conn, ev):
    """按 启动ID + PID + 所在cgroup + 左闭右开时段 [start_ms, end_ms) 匹配。"""
    return conn.execute(
        """SELECT run_no FROM runs
           WHERE boot_id = ? AND pid = ? AND cgroup = ?
             AND start_ms <= ? AND end_ms > ?
           ORDER BY run_no""",
        (ev["boot_id"], ev["pid"], ev["cgroup"], ev["time_ms"], ev["time_ms"]),
    ).fetchall()


def compute_event(conn, ev):
    rows = find_matches(conn, ev)
    if len(rows) == 1:
        run_no = rows[0]["run_no"]
        return {
            "status": "unique",
            "run_no": run_no,
            "match_count": 1,
            "fingerprint": fingerprint(ev["event_no"], run_no, ev["scope"]),
        }
    return {"status": "pending", "run_no": None,
            "match_count": len(rows), "fingerprint": None}


def recompute_all(conn):
    """全量重核归属；归属或触发范围变化的事件，撤销其有效确认。

    返回 {"changed": [event_no...], "revoked": [(event_no, reason)...]}。
    """
    now = now_iso()
    changed, revoked = [], []
    events = conn.execute("SELECT * FROM events ORDER BY event_no").fetchall()
    for ev in events:
        cur = compute_event(conn, ev)
        old = conn.execute(
            "SELECT * FROM attributions WHERE event_no = ?",
            (ev["event_no"],),
        ).fetchone()
        if old is None or any(
            old[k] != cur[k]
            for k in ("status", "run_no", "match_count", "fingerprint")
        ):
            conn.execute(
                """INSERT INTO attributions
                       (event_no, status, run_no, match_count, fingerprint, computed_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(event_no) DO UPDATE SET
                       status = excluded.status,
                       run_no = excluded.run_no,
                       match_count = excluded.match_count,
                       fingerprint = excluded.fingerprint,
                       computed_at = excluded.computed_at""",
                (ev["event_no"], cur["status"], cur["run_no"],
                 cur["match_count"], cur["fingerprint"], now),
            )
            changed.append(ev["event_no"])
        for c in conn.execute(
            "SELECT * FROM confirmations WHERE event_no = ? AND state = 'active'",
            (ev["event_no"],),
        ).fetchall():
            if c["fingerprint"] != cur["fingerprint"]:
                if cur["status"] == "unique":
                    new_desc = "运行号 {} / 范围 {}".format(
                        cur["run_no"], ev["scope"])
                else:
                    new_desc = "待核（候选 {} 个）".format(cur["match_count"])
                reason = "数据更正后结论变化：原 运行号 {} / 范围 {} → 现 {}".format(
                    c["run_no"], c["scope"], new_desc)
                conn.execute(
                    """UPDATE confirmations
                       SET state = 'revoked', revoked_at = ?, revoke_reason = ?
                       WHERE id = ?""",
                    (now, reason, c["id"]),
                )
                revoked.append((ev["event_no"], reason))
    return {"changed": changed, "revoked": revoked}


# ---------------------------------------------------------------- 确认

def confirm_event(conn, event_no, fp):
    """确认派查。返回 'confirmed' 或 'already'；拒绝时抛 ConfirmRejected。"""
    ev = conn.execute(
        "SELECT * FROM events WHERE event_no = ?", (event_no,)).fetchone()
    if ev is None:
        raise ConfirmRejected("事件 {} 不存在".format(event_no))
    attr = conn.execute(
        "SELECT * FROM attributions WHERE event_no = ?", (event_no,)).fetchone()
    if attr is None or attr["status"] != "unique":
        raise ConfirmRejected(
            "事件 {} 当前为待核，无唯一匹配，不能确认".format(event_no))
    if not fp or attr["fingerprint"] != fp:
        raise ConfirmRejected(
            "页面数据已过期：事件 {} 的归属或触发范围已更正，"
            "请刷新后重新核对".format(event_no))
    existing = conn.execute(
        "SELECT id FROM confirmations WHERE event_no = ? AND state = 'active'",
        (event_no,)).fetchone()
    if existing:
        return "already"
    conn.execute(
        """INSERT INTO confirmations
               (event_no, run_no, scope, fingerprint, state, confirmed_at)
           VALUES (?, ?, ?, ?, 'active', ?)""",
        (event_no, attr["run_no"], ev["scope"], attr["fingerprint"], now_iso()),
    )
    return "confirmed"


# ---------------------------------------------------------------- 记录校验

def run_from_strings(d, ctx=""):
    try:
        run_no = int(str(d.get("run_no", "")).strip())
        pid = int(str(d.get("pid", "")).strip())
        start_ms = int(str(d.get("start_ms", "")).strip())
        end_ms = int(str(d.get("end_ms", "")).strip())
    except (ValueError, TypeError):
        raise DataError(ctx + "运行号/PID/起止时刻必须为整数")
    boot_id = str(d.get("boot_id", "")).strip()
    cgroup = str(d.get("cgroup", "")).strip()
    if not boot_id:
        raise DataError(ctx + "boot_id 不能为空")
    if not cgroup:
        raise DataError(ctx + "cgroup 不能为空")
    if run_no <= 0:
        raise DataError(ctx + "run_no 必须为正整数")
    if pid <= 0:
        raise DataError(ctx + "pid 必须为正整数")
    if start_ms < 0:
        raise DataError(ctx + "start_ms 不能为负")
    if end_ms <= start_ms:
        raise DataError(ctx + "end_ms 必须大于 start_ms（左闭右开区间）")
    return dict(run_no=run_no, boot_id=boot_id, pid=pid, cgroup=cgroup,
                start_ms=start_ms, end_ms=end_ms)


def event_from_strings(d, ctx=""):
    try:
        event_no = int(str(d.get("event_no", "")).strip())
        pid = int(str(d.get("pid", "")).strip())
        time_ms = int(str(d.get("time_ms", "")).strip())
    except (ValueError, TypeError):
        raise DataError(ctx + "事件号/PID/时刻必须为整数")
    boot_id = str(d.get("boot_id", "")).strip()
    cgroup = str(d.get("cgroup", "")).strip()
    scope = str(d.get("scope", "")).strip().lower()
    if not boot_id:
        raise DataError(ctx + "boot_id 不能为空")
    if not cgroup:
        raise DataError(ctx + "cgroup 不能为空")
    if event_no <= 0:
        raise DataError(ctx + "event_no 必须为正整数")
    if pid <= 0:
        raise DataError(ctx + "pid 必须为正整数")
    if time_ms < 0:
        raise DataError(ctx + "time_ms 不能为负")
    if scope not in SCOPES:
        raise DataError(ctx + "scope 必须为 memcg（触发组）或 host（整机）")
    return dict(event_no=event_no, boot_id=boot_id, pid=pid, cgroup=cgroup,
                time_ms=time_ms, scope=scope)


# ---------------------------------------------------------------- CSV 导入

def _parse_csv(text, columns, row_fn):
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise DataError("空文件")
    headers = [h.strip() for h in reader.fieldnames]
    missing = [c for c in columns if c not in headers]
    if missing:
        raise DataError("缺少列: " + ", ".join(missing))
    rows, errors, seen = [], [], set()
    for lineno, raw in enumerate(reader, start=2):
        try:
            row = row_fn({k: (v or "") for k, v in raw.items() if k},
                         ctx="第{}行: ".format(lineno))
            key = row.get("run_no", row.get("event_no"))
            if key in seen:
                raise DataError("第{}行: 编号 {} 在文件内重复".format(lineno, key))
            seen.add(key)
            rows.append(row)
        except DataError as e:
            errors.append(str(e))
    if errors:
        raise DataError("；".join(errors[:10]))
    if not rows:
        raise DataError("没有数据行")
    return rows


def parse_runs_csv(text):
    return _parse_csv(text, RUN_COLUMNS, run_from_strings)


def parse_events_csv(text):
    return _parse_csv(text, EVENT_COLUMNS, event_from_strings)


def upsert_runs(conn, rows):
    """按运行号 upsert：新编号=补录，已有编号=更正。返回 (新增数, 更正数)。"""
    now = now_iso()
    ins = upd = 0
    for r in rows:
        exists = conn.execute(
            "SELECT 1 FROM runs WHERE run_no = ?", (r["run_no"],)).fetchone()
        conn.execute(
            """INSERT INTO runs
                   (run_no, boot_id, pid, cgroup, start_ms, end_ms, updated_at)
               VALUES (:run_no, :boot_id, :pid, :cgroup, :start_ms, :end_ms, :now)
               ON CONFLICT(run_no) DO UPDATE SET
                   boot_id = excluded.boot_id, pid = excluded.pid,
                   cgroup = excluded.cgroup, start_ms = excluded.start_ms,
                   end_ms = excluded.end_ms, updated_at = excluded.updated_at""",
            dict(r, now=now),
        )
        if exists:
            upd += 1
        else:
            ins += 1
    return ins, upd


def upsert_events(conn, rows):
    now = now_iso()
    ins = upd = 0
    for r in rows:
        exists = conn.execute(
            "SELECT 1 FROM events WHERE event_no = ?",
            (r["event_no"],)).fetchone()
        conn.execute(
            """INSERT INTO events
                   (event_no, boot_id, pid, cgroup, time_ms, scope, updated_at)
               VALUES (:event_no, :boot_id, :pid, :cgroup, :time_ms, :scope, :now)
               ON CONFLICT(event_no) DO UPDATE SET
                   boot_id = excluded.boot_id, pid = excluded.pid,
                   cgroup = excluded.cgroup, time_ms = excluded.time_ms,
                   scope = excluded.scope, updated_at = excluded.updated_at""",
            dict(r, now=now),
        )
        if exists:
            upd += 1
        else:
            ins += 1
    return ins, upd
