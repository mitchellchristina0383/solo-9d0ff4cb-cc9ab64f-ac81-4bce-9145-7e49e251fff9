"""OOM 归属核对应用：Flask 应用工厂。"""
import os

from flask import Flask

from .core import recompute_all
from .db import close_db, connect, init_schema


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "oom-duty-dev-key"),
        DATABASE=os.environ.get(
            "OOM_DB", os.path.join(app.instance_path, "oom.db")
        ),
    )
    if config:
        app.config.from_mapping(config)

    db_path = app.config["DATABASE"]
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    # 启动时建表并重核一次：保证重启后结论与确认状态和数据一致。
    conn = connect(db_path)
    try:
        init_schema(conn)
        recompute_all(conn)
        conn.commit()
    finally:
        conn.close()

    from .views import bp

    app.register_blueprint(bp)
    app.teardown_appcontext(close_db)
    return app
