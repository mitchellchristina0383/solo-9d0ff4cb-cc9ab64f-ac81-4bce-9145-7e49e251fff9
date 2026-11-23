"""页面与表单路由。"""
import sqlite3

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from . import core
from .db import get_db

bp = Blueprint("main", __name__)


def _flash_recompute(stats):
    msgs = []
    if stats["changed"]:
        msgs.append("归属结论已重核（更新 {} 条）".format(len(stats["changed"])))
    for event_no, reason in stats["revoked"]:
        msgs.append("事件 {} 的原确认已撤销：{}".format(event_no, reason))
    return msgs


@bp.route("/")
def index():
    db = get_db()
    events = db.execute(
        """SELECT e.event_no, e.boot_id, e.pid, e.cgroup, e.time_ms, e.scope,
                  a.status, a.run_no AS attr_run_no, a.match_count, a.fingerprint
           FROM events e LEFT JOIN attributions a USING (event_no)
           ORDER BY e.event_no"""
    ).fetchall()
    active, revoked = {}, {}
    for c in db.execute("SELECT * FROM confirmations ORDER BY id").fetchall():
        if c["state"] == "active":
            active[c["event_no"]] = c
        else:
            revoked[c["event_no"]] = c  # 取最近一次被撤销的确认
    return render_template("index.html", events=events,
                           active=active, revoked=revoked)


@bp.route("/import", methods=["GET", "POST"])
def import_csv():
    if request.method == "POST":
        kind = request.form.get("kind")
        f = request.files.get("file")
        if f is None or not f.filename:
            flash("请选择要导入的 CSV 文件", "error")
            return redirect(url_for(".import_csv"))
        try:
            text = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            flash("文件编码需为 UTF-8", "error")
            return redirect(url_for(".import_csv"))
        db = get_db()
        try:
            if kind == "runs":
                ins, upd = core.upsert_runs(db, core.parse_runs_csv(text))
                what = "运行记录：新增 {} 条，更正 {} 条".format(ins, upd)
            elif kind == "events":
                ins, upd = core.upsert_events(db, core.parse_events_csv(text))
                what = "OOM 摘要：新增 {} 条，更正 {} 条".format(ins, upd)
            else:
                flash("未知导入类型", "error")
                return redirect(url_for(".import_csv"))
            stats = core.recompute_all(db)
            db.commit()
        except core.DataError as e:
            db.rollback()
            flash("导入失败，未写入任何数据：{}".format(e), "error")
            return redirect(url_for(".import_csv"))
        for m in _flash_recompute(stats):
            flash(m, "error")
        flash("导入完成（{}）".format(what), "ok")
        return redirect(url_for(".index"))
    return render_template("import.html")


@bp.route("/confirm", methods=["POST"])
def confirm():
    try:
        event_no = int(request.form.get("event_no", ""))
    except ValueError:
        flash("确认请求不合法", "error")
        return redirect(url_for(".index"))
    fp = request.form.get("fingerprint", "")
    db = get_db()
    try:
        result = core.confirm_event(db, event_no, fp)
        db.commit()
    except core.ConfirmRejected as e:
        db.rollback()
        flash("确认被拒绝：{}".format(e), "error")
        return redirect(url_for(".index"))
    except sqlite3.IntegrityError:
        # 并发下另一请求已确认（唯一索引兜底）
        db.rollback()
        flash("事件 {} 已被确认".format(event_no), "ok")
        return redirect(url_for(".index"))
    if result == "already":
        flash("事件 {} 已是确认状态".format(event_no), "ok")
    else:
        flash("事件 {} 已确认，可派查".format(event_no), "ok")
    return redirect(url_for(".index"))


@bp.route("/runs")
def runs():
    rows = get_db().execute(
        "SELECT * FROM runs ORDER BY run_no").fetchall()
    return render_template("runs.html", runs=rows)


@bp.route("/runs/<int:run_no>/edit", methods=["GET", "POST"])
def edit_run(run_no):
    db = get_db()
    run = db.execute(
        "SELECT * FROM runs WHERE run_no = ?", (run_no,)).fetchone()
    if request.method == "POST":
        try:
            row = core.run_from_strings(
                dict(request.form, run_no=run_no))
        except core.DataError as e:
            flash(str(e), "error")
            return render_template("run_edit.html", run_no=run_no,
                                   is_new=run is None, form=request.form)
        core.upsert_runs(db, [row])
        stats = core.recompute_all(db)
        db.commit()
        for m in _flash_recompute(stats):
            flash(m, "error")
        flash("运行记录 {} 已保存".format(run_no), "ok")
        return redirect(url_for(".runs"))
    form = dict(run) if run else {}
    return render_template("run_edit.html", run_no=run_no,
                           is_new=run is None, form=form)


@bp.route("/events")
def events():
    rows = get_db().execute(
        "SELECT * FROM events ORDER BY event_no").fetchall()
    return render_template("events.html", events=rows)


@bp.route("/events/<int:event_no>/edit", methods=["GET", "POST"])
def edit_event(event_no):
    db = get_db()
    ev = db.execute(
        "SELECT * FROM events WHERE event_no = ?", (event_no,)).fetchone()
    if request.method == "POST":
        try:
            row = core.event_from_strings(
                dict(request.form, event_no=event_no))
        except core.DataError as e:
            flash(str(e), "error")
            return render_template("event_edit.html", event_no=event_no,
                                   is_new=ev is None, form=request.form)
        core.upsert_events(db, [row])
        stats = core.recompute_all(db)
        db.commit()
        for m in _flash_recompute(stats):
            flash(m, "error")
        flash("事件 {} 已保存".format(event_no), "ok")
        return redirect(url_for(".events"))
    form = dict(ev) if ev else {}
    return render_template("event_edit.html", event_no=event_no,
                           is_new=ev is None, form=form)
