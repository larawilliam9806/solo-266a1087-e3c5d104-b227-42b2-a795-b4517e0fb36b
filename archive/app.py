"""Flask hand-in page for the film archive."""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .core import (
    DuplicateOrderError,
    SubmissionError,
    clean_identifier,
    get_order,
    list_orders,
    reel_state,
    submit_order,
)
from .csv_import import parse_edit_csv_checked
from .db import init_db


def _load_or_create_secret(database: str) -> str:
    """Return a stable secret so every worker validates the same sessions.

    Without an explicit SECRET_KEY we persist one next to the database.  A
    per-process random key would make CSRF tokens fail whenever a request
    lands on a different gunicorn worker.  Workers may race on first boot, so
    a reader retries until the creator has flushed a complete key.
    """
    env_secret = os.environ.get("SECRET_KEY")
    if env_secret:
        return env_secret
    path = Path(database).with_suffix(Path(database).suffix + ".secret")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(200):
            secret = path.read_text(encoding="utf-8").strip()
            if len(secret) >= 32:
                return secret
            time.sleep(0.01)
        raise RuntimeError(f"secret file {path} is unreadable") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        secret = secrets.token_hex(32)
        handle.write(secret)
        handle.flush()
        os.fsync(handle.fileno())
    return secret


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    database = os.environ.get("ARCHIVE_DB", "data/archive.sqlite3")
    app.config.update(
        DATABASE=database,
        MAX_CONTENT_LENGTH=24 * 1024 * 1024,
        JSON_AS_ASCII=False,
    )
    if config:
        app.config.update(config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _load_or_create_secret(app.config["DATABASE"])

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    init_db(app.config["DATABASE"])

    def db_path() -> str:
        return app.config["DATABASE"]

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def check_csrf() -> None:
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected, supplied):
            abort(400, description="表单已过期，请刷新页面后重试")

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            orders=list_orders(db_path(), limit=20),
            form={"order_no": "", "film_no": ""},
            errors=[],
        )

    @app.post("/submit")
    def submit():
        check_csrf()
        order_no = request.form.get("order_no", "")
        film_no = request.form.get("film_no", "")
        upload = request.files.get("csv_file")
        errors: list[str] = []

        if upload is None or not upload.filename:
            errors.append("请选择要上传的剪辑 CSV 文件")
            data = None
        else:
            data = upload.read()
            if not data:
                errors.append("上传的 CSV 文件为空")
                data = None

        rows = []
        if data is not None:
            try:
                rows = parse_edit_csv_checked(data)
            except SubmissionError as exc:
                errors.extend(exc.messages)

        # Validate the identifiers too, so one response can list every problem.
        try:
            order_no = clean_identifier(order_no, "单号")
        except SubmissionError as exc:
            errors.extend(exc.messages)
        try:
            film_no = clean_identifier(film_no, "成片号")
        except SubmissionError as exc:
            errors.extend(exc.messages)

        if not errors:
            try:
                report = submit_order(db_path(), order_no, film_no, rows)
            except DuplicateOrderError as exc:
                errors.extend(exc.messages)
                status = 409
            except SubmissionError as exc:
                errors.extend(exc.messages)
                status = 400
            else:
                return redirect(url_for("order_detail", order_no=report["order_no"]))
        else:
            status = 400

        return (
            render_template(
                "index.html",
                orders=list_orders(db_path(), limit=20),
                form={"order_no": order_no, "film_no": film_no},
                errors=errors,
            ),
            status,
        )

    @app.get("/orders/<path:order_no>")
    def order_detail(order_no: str):
        report = get_order(db_path(), order_no)
        if report is None:
            return render_template("not_found.html", order_no=order_no), 404
        return render_template("order.html", order=report)

    @app.get("/reels/<path:orig_reel>")
    def reel_detail(orig_reel: str):
        state = reel_state(db_path(), orig_reel)
        if state is None:
            return render_template("not_found.html", order_no=orig_reel), 404
        return render_template("reel.html", reel=state)

    @app.get("/api/orders/<path:order_no>")
    def api_order(order_no: str):
        report = get_order(db_path(), order_no)
        if report is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(report)

    @app.get("/api/reels/<path:orig_reel>")
    def api_reel(orig_reel: str):
        state = reel_state(db_path(), orig_reel)
        if state is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(state)

    @app.errorhandler(413)
    def too_large(_err):
        return (
            render_template(
                "index.html",
                orders=list_orders(db_path(), limit=20),
                form={"order_no": "", "film_no": ""},
                errors=["上传文件超过大小限制"],
            ),
            413,
        )

    @app.errorhandler(400)
    def bad_request(err):
        return render_template("error.html", message=err.description), 400

    return app
