"""
app.py — STL Asset Manager (Flask)
Запуск: python app.py
"""
from __future__ import annotations

import logging
import os
import socket
import threading
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, abort, jsonify, redirect, render_template,
    request, send_file, url_for
)
from werkzeug.utils import secure_filename

from core.database import (
    UPLOAD_DIR, all_models, db_stats, delete_model,
    get_by_id, get_engine, get_session, search_models
)
from core.scanner import ScanJob, index_single_file

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")

# ── App ───────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = "stl-manager-secret-2025"
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024

# Jinja2 фильтры
app.jinja_env.filters["basename"] = os.path.basename


def fmt_number(value):
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


app.jinja_env.filters["fmt_number"] = fmt_number

# ── Глобальный скан ───────────────────────────────────────────────────────
_scan_lock    = threading.Lock()
_current_scan: ScanJob | None = None


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "localhost"


# ── Routes: Library ───────────────────────────────────────────────────────
@app.route("/")
def index():
    q              = request.args.get("q", "").strip()
    sort           = request.args.get("sort", "uploaded_at")
    order          = request.args.get("order", "desc")
    tag            = request.args.get("tag", "").strip()
    watertight     = request.args.get("wt", "") == "1"
    category       = request.args.get("cat", "").strip()
    size_min       = float(request.args.get("smin", 0) or 0)
    size_max       = float(request.args.get("smax", 0) or 0)

    session = get_session()
    try:
        if q:
            models = search_models(session, q, sort=sort, order=order)
        else:
            models = all_models(
                session, sort=sort, order=order,
                watertight_only=watertight,
                category=category,
                size_min=size_min,
                size_max=size_max,
            )
        if tag:
            models = [m for m in models if tag in m.tag_list()]

        stats = db_stats(session)
        return render_template("index.html",
            models=models, stats=stats,
            query=q, sort=sort, order=order,
            active_tag=tag, wt=watertight,
            active_cat=category,
            size_min=size_min, size_max=size_max,
        )
    finally:
        session.close()


# ── Routes: Model detail ──────────────────────────────────────────────────
@app.route("/model/<int:mid>")
def model_detail(mid: int):
    session = get_session()
    try:
        m = get_by_id(session, mid)
        if not m:
            abort(404)
        return render_template("model.html", model=m)
    finally:
        session.close()


@app.route("/model/<int:mid>/edit", methods=["GET", "POST"])
def model_edit(mid: int):
    session = get_session()
    try:
        m = get_by_id(session, mid)
        if not m:
            abort(404)
        if request.method == "POST":
            m.name        = (request.form.get("name") or m.name).strip()
            m.description = (request.form.get("description") or "").strip()
            m.tags        = (request.form.get("tags") or "").strip()
            m.category    = (request.form.get("category") or "").strip()
            m.notes       = (request.form.get("notes") or "").strip()
            raw_dt        = request.form.get("uploaded_at", "").strip()
            if raw_dt:
                try:
                    m.uploaded_at = datetime.strptime(raw_dt, "%Y-%m-%dT%H:%M")
                except ValueError:
                    pass
            session.commit()
            return redirect(url_for("model_detail", mid=mid))
        # Формат для input datetime-local
        dt_str = m.uploaded_at.strftime("%Y-%m-%dT%H:%M") if m.uploaded_at else ""
        return render_template("edit.html", model=m, dt_str=dt_str)
    finally:
        session.close()


@app.route("/model/<int:mid>/delete", methods=["POST"])
def model_delete(mid: int):
    session = get_session()
    try:
        delete_model(session, mid)
    finally:
        session.close()
    return redirect(url_for("index"))


@app.route("/model/<int:mid>/download")
def model_download(mid: int):
    session = get_session()
    try:
        m = get_by_id(session, mid)
        if not m or not Path(m.file_path).exists():
            abort(404)
        return send_file(m.file_path, as_attachment=True,
                         download_name=Path(m.file_path).name)
    finally:
        session.close()


# ── Routes: Upload ────────────────────────────────────────────────────────
@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template("upload.html")

    f = request.files.get("stl_file")
    if not f or not f.filename:
        return jsonify({"ok": False, "msg": "Файл не выбран"}), 400
    fname = secure_filename(f.filename)
    if not fname.lower().endswith(".stl"):
        return jsonify({"ok": False, "msg": "Только .stl файлы"}), 400

    dest = UPLOAD_DIR / fname
    stem, ext = Path(fname).stem, Path(fname).suffix
    counter = 1
    while dest.exists():
        dest = UPLOAD_DIR / f"{stem}_{counter}{ext}"
        counter += 1

    f.save(str(dest))
    ok, msg, model_id = index_single_file(dest)
    return jsonify({"ok": ok, "msg": msg, "model_id": model_id})


# ── Routes: Scan ──────────────────────────────────────────────────────────
@app.route("/scan", methods=["GET"])
def scan_page():
    return render_template("scan.html")


@app.route("/scan", methods=["POST"])
def scan_start():
    global _current_scan
    data      = request.get_json(silent=True) or {}
    directory = (data.get("directory") or "").strip()
    recursive = bool(data.get("recursive", True))

    if not directory or not Path(directory).is_dir():
        return jsonify({"ok": False, "msg": "Папка не найдена или не существует"}), 400

    with _scan_lock:
        if _current_scan and _current_scan.is_running():
            return jsonify({"ok": False, "msg": "Сканирование уже запущено"}), 409
        _current_scan = ScanJob(directory, recursive=recursive)
        _current_scan.start()

    return jsonify({"ok": True, "msg": "Сканирование запущено"})


@app.route("/scan/progress")
def scan_progress():
    global _current_scan
    if not _current_scan:
        return jsonify({"running": False, "done": True, "total": 0,
                        "current": 0, "pct": 0, "added": 0, "dupes": 0,
                        "errors": 0, "current_file": "", "error_msg": ""})
    p   = _current_scan.progress
    pct = int(p.current / p.total * 100) if p.total else 0
    return jsonify({
        "running":      _current_scan.is_running(),
        "done":         p.done,
        "cancelled":    p.cancelled,
        "total":        p.total,
        "current":      p.current,
        "current_file": p.current_file,
        "added":        p.added,
        "dupes":        p.dupes,
        "errors":       p.errors,
        "pct":          pct,
        "error_msg":    p.error_msg,
    })


@app.route("/scan/stop", methods=["POST"])
def scan_stop():
    global _current_scan
    if _current_scan:
        _current_scan.stop()
    return jsonify({"ok": True})


# ── Routes: API ───────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    session = get_session()
    try:
        return jsonify(db_stats(session))
    finally:
        session.close()


# ── Main ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    get_engine()
    ip = _local_ip()
    print("\n" + "═" * 52)
    print("  ◈  STL Asset Manager")
    print(f"  Локально :  http://localhost:5000")
    print(f"  Сеть     :  http://{ip}:5000")
    print("═" * 52 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
