#!/usr/bin/env python3
"""自测：归属匹配（PID 复用、左闭右开、跨启动隔离）、确认、更正后重核与撤销、
旧页面确认拒绝、导入校验与重启持久化。

用法: python3 tests/selftest.py
"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import create_app  # noqa: E402

PASS = 0


def check(cond, label):
    global PASS
    assert cond, "FAIL: " + label
    PASS += 1
    print("  ok - " + label)


def csv_text(header, rows):
    lines = [",".join(header)]
    for r in rows:
        lines.append(",".join(str(x) for x in r))
    return "\n".join(lines) + "\n"


RUNS_HDR = ["run_no", "boot_id", "pid", "cgroup", "start_ms", "end_ms"]
EVENTS_HDR = ["event_no", "boot_id", "pid", "cgroup", "time_ms", "scope"]


def post_import(client, kind, text):
    data = {"kind": kind,
            "file": (io.BytesIO(text.encode("utf-8")), kind + ".csv")}
    return client.post("/import", data=data,
                       content_type="multipart/form-data",
                       follow_redirects=True)


def q(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def q1(db_path, sql, args=()):
    rows = q(db_path, sql, args)
    return rows[0] if rows else None


def attr(db_path, event_no):
    return q1(db_path, "SELECT * FROM attributions WHERE event_no=?",
              (event_no,))


def fp_of(db_path, event_no):
    return attr(db_path, event_no)["fingerprint"]


def active_conf(db_path, event_no):
    return q1(db_path, "SELECT * FROM confirmations "
                       "WHERE event_no=? AND state='active'", (event_no,))


def confirm(client, event_no, fp):
    return client.post("/confirm",
                       data={"event_no": event_no, "fingerprint": fp},
                       follow_redirects=True)


def main():
    tmp = tempfile.mkdtemp(prefix="oom-selftest-")
    db_path = os.path.join(tmp, "oom.db")
    try:
        app = create_app({"DATABASE": db_path, "SECRET_KEY": "test",
                          "TESTING": True})
        c = app.test_client()

        print("== 1. 导入与归属匹配（含 PID 复用、左闭右开） ==")
        runs = csv_text(RUNS_HDR, [
            (1, "B1", 100, "/a", 1000, 2000),
            (2, "B1", 100, "/a", 2000, 3000),   # PID 100 复用，紧接上一段
            (3, "B1", 100, "/b", 1000, 3000),   # 同 PID 不同 cgroup
            (4, "B1", 200, "/a", 5000, 6000),
            (5, "B1", 200, "/a", 5500, 6500),   # 与 4 重叠
            (6, "B2", 100, "/a", 1000, 2000),   # 另一次启动
        ])
        events = csv_text(EVENTS_HDR, [
            (101, "B1", 100, "/a", 1500, "memcg"),
            (102, "B1", 100, "/a", 2000, "host"),
            (103, "B1", 100, "/a", 999, "memcg"),
            (104, "B1", 100, "/b", 1500, "memcg"),
            (105, "B1", 200, "/a", 5700, "host"),
            (106, "B1", 100, "/a", 3000, "memcg"),
            (107, "B1", 200, "/a", 5200, "memcg"),
            (108, "B2", 100, "/a", 1500, "host"),
            (109, "B3", 100, "/a", 1500, "memcg"),
        ])
        r = post_import(c, "runs", runs)
        check(r.status_code == 200 and "新增 6 条".encode() in r.data,
              "运行记录导入成功")
        r = post_import(c, "events", events)
        check(r.status_code == 200 and "新增 9 条".encode() in r.data,
              "OOM 摘要导入成功")

        expect = {  # event_no: (status, run_no, match_count)
            101: ("unique", 1, 1),   # 落在 run1 时段内
            102: ("unique", 2, 1),   # t=2000：run1 右开排除，run2 左闭命中
            103: ("pending", None, 0),
            104: ("unique", 3, 1),   # 同 PID，cgroup 区分
            105: ("pending", None, 2),  # run4/run5 重叠
            106: ("pending", None, 0),  # t=3000 为 run2 右开端点
            107: ("unique", 4, 1),   # 只落在 run4
            108: ("unique", 6, 1),   # 启动 ID 隔离
            109: ("pending", None, 0),
        }
        for eno, (st, rn, mc) in expect.items():
            a = attr(db_path, eno)
            check(a["status"] == st and a["run_no"] == rn
                  and a["match_count"] == mc,
                  "事件 {} 归属 = {}/{}/{}".format(eno, st, rn, mc))

        r = c.get("/")
        check(r.status_code == 200, "首页可访问")
        check("待核".encode() in r.data, "待核事件已标注")

        print("== 2. 确认流程 ==")
        fp101 = fp_of(db_path, 101)
        r = confirm(c, 101, fp101)
        check("已确认".encode() in r.data, "唯一匹配事件确认成功")
        conf = active_conf(db_path, 101)
        check(conf and conf["run_no"] == 1 and conf["scope"] == "memcg",
              "确认记录含运行号与触发范围")
        r = confirm(c, 101, fp101)
        check("已是确认状态".encode() in r.data, "重复确认幂等")
        check(len(q(db_path, "SELECT * FROM confirmations "
                             "WHERE event_no=101 AND state='active'")) == 1,
              "同一事件仅一条有效确认")
        r = confirm(c, 103, "whatever")
        check("待核".encode() in r.data and active_conf(db_path, 103) is None,
              "待核事件确认被拒绝")
        r = confirm(c, 102, "bogus-fingerprint")
        check("已过期".encode() in r.data and active_conf(db_path, 102) is None,
              "指纹不符的确认被拒绝")

        print("== 3. 更正后重核：撤销、旧页面拒绝、其他事件照旧 ==")
        # 3a. 确认 107(run4) 后更正 run5 使其与 run4 重叠 -> 107 变待核
        fp107 = fp_of(db_path, 107)
        confirm(c, 107, fp107)
        check(active_conf(db_path, 107) is not None, "事件 107 已确认")
        r = c.post("/runs/5/edit",
                   data={"boot_id": "B1", "pid": 200, "cgroup": "/a",
                         "start_ms": 5000, "end_ms": 5600},
                   follow_redirects=True)
        check("已撤销".encode() in r.data, "更正运行记录后提示撤销")
        a = attr(db_path, 107)
        check(a["status"] == "pending" and a["match_count"] == 2,
              "事件 107 更正后变待核（2 候选）")
        check(active_conf(db_path, 107) is None, "事件 107 原确认已撤销")
        check(q1(db_path, "SELECT state FROM confirmations "
                          "WHERE event_no=107 ORDER BY id DESC")["state"]
              == "revoked", "撤销状态已落库")
        r = confirm(c, 107, fp107)
        check(active_conf(db_path, 107) is None, "旧页面确认 107 被拒绝")
        # 副作用：105@5700 只剩 run4 匹配 -> 待核变唯一
        a = attr(db_path, 105)
        check(a["status"] == "unique" and a["run_no"] == 4,
              "事件 105 因更正由待核变为唯一匹配 run4")
        check(active_conf(db_path, 101) is not None,
              "无关事件 101 的确认不受影响")

        # 3b. 归属从 run8 变为 run9（补录），旧指纹确认被拒
        c.post("/runs/8/edit",
               data={"boot_id": "B1", "pid": 300, "cgroup": "/a",
                     "start_ms": 100, "end_ms": 200}, follow_redirects=True)
        c.post("/events/110/edit",
               data={"boot_id": "B1", "pid": 300, "cgroup": "/a",
                     "time_ms": 150, "scope": "memcg"}, follow_redirects=True)
        fp110_old = fp_of(db_path, 110)
        check(attr(db_path, 110)["run_no"] == 8, "事件 110 初始归属 run8")
        confirm(c, 110, fp110_old)
        check(active_conf(db_path, 110)["run_no"] == 8, "事件 110 已确认 run8")
        c.post("/runs/8/edit",
               data={"boot_id": "B1", "pid": 300, "cgroup": "/a",
                     "start_ms": 100, "end_ms": 140}, follow_redirects=True)
        check(attr(db_path, 110)["status"] == "pending",
              "run8 时段缩短后事件 110 变待核")
        check(active_conf(db_path, 110) is None, "事件 110 确认已撤销")
        # 补录 run9 覆盖该时刻 -> 归属变为 run9
        r = post_import(c, "runs", csv_text(RUNS_HDR,
                                            [(9, "B1", 300, "/a", 140, 300)]))
        check("新增 1 条".encode() in r.data, "补录 run9 成功")
        a = attr(db_path, 110)
        check(a["status"] == "unique" and a["run_no"] == 9,
              "补录后事件 110 归属 run9")
        r = confirm(c, 110, fp110_old)
        check("已过期".encode() in r.data and active_conf(db_path, 110) is None,
              "旧页面（run8 指纹）确认被拒绝")
        r = confirm(c, 110, fp_of(db_path, 110))
        check(active_conf(db_path, 110)["run_no"] == 9,
              "刷新后按新归属 run9 确认成功")

        # 3c. 更正事件触发范围 -> 撤销原确认
        confirm(c, 104, fp_of(db_path, 104))
        check(active_conf(db_path, 104)["scope"] == "memcg",
              "事件 104 已按 memcg 确认")
        r = c.post("/events/104/edit",
                   data={"boot_id": "B1", "pid": 100, "cgroup": "/b",
                         "time_ms": 1500, "scope": "host"},
                   follow_redirects=True)
        check("已撤销".encode() in r.data, "更正触发范围后提示撤销")
        check(active_conf(db_path, 104) is None, "事件 104 原确认已撤销")
        confirm(c, 104, fp_of(db_path, 104))
        check(active_conf(db_path, 104)["scope"] == "host",
              "事件 104 按新触发范围 host 重新确认")
        check(active_conf(db_path, 101) is not None
              and active_conf(db_path, 110) is not None,
              "其他事件确认照旧")

        print("== 4. 导入校验与按编号更正（CSV） ==")
        n_events = q1(db_path, "SELECT COUNT(*) n FROM events")["n"]
        bad = csv_text(EVENTS_HDR, [(201, "B1", 1, "/a", 10, "bogus")])
        r = post_import(c, "events", bad)
        check("导入失败".encode() in r.data, "非法 scope 导入失败")
        check(q1(db_path, "SELECT COUNT(*) n FROM events")["n"] == n_events,
              "失败导入未写入任何数据（原子性）")
        # CSV 更正 run1 的结束时刻；101 归属不变 -> 确认保留
        r = post_import(c, "runs", csv_text(RUNS_HDR,
                                            [(1, "B1", 100, "/a", 1000, 1900)]))
        check("更正 1 条".encode() in r.data, "CSV 按编号更正 run1")
        check(q1(db_path, "SELECT end_ms FROM runs WHERE run_no=1")["end_ms"]
              == 1900, "run1 结束时刻已更正")
        check(active_conf(db_path, 101) is not None,
              "归属未变的事件确认保留")

        print("== 5. 重启持久化 ==")
        app2 = create_app({"DATABASE": db_path, "SECRET_KEY": "test",
                           "TESTING": True})
        c2 = app2.test_client()
        r = c2.get("/")
        check(r.status_code == 200, "重启后首页可访问")
        check(active_conf(db_path, 101)["run_no"] == 1,
              "重启后事件 101 确认仍在")
        check(active_conf(db_path, 110)["run_no"] == 9,
              "重启后事件 110 确认（run9）仍在")
        check(q1(db_path, "SELECT COUNT(*) n FROM confirmations "
                          "WHERE state='revoked'")["n"] == 3,
              "重启后 3 条撤销记录仍在")
        a = attr(db_path, 105)
        check(a["status"] == "unique" and a["run_no"] == 4,
              "重启后最新归属结论保留（105 -> run4）")
        a = attr(db_path, 107)
        check(a["status"] == "pending" and a["match_count"] == 2,
              "重启后待核状态保留（107）")

        print("\n全部 {} 项检查通过".format(PASS))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
