"""Server-observed adoption, with aggregate output and no analytics identity."""
import sqlite3
import time
from flask import jsonify, request
from db import get_db
from utils import current_user_id


def record_account_event(event, method="password", context=None):
    context = context or request.headers.get("X-WebMARS-Context", "public")
    if context not in {"public", "internal", "test"}:
        context = "public"
    bucket = int(time.time()) // 60 * 60
    db = get_db()
    cutoff = bucket - 35 * 86400
    db.execute("DELETE FROM account_visits WHERE bucket < ?", (cutoff,))
    db.execute("DELETE FROM account_counters WHERE bucket < ?", (cutoff,))
    db.execute("INSERT INTO account_counters VALUES(?,?,?,?,1) ON CONFLICT(bucket,event,method,context) DO UPDATE SET count=count+1", (bucket,event,method,context))
    user = current_user_id()
    if user:
        db.execute("INSERT OR IGNORE INTO account_visits VALUES(?,?,?)", (bucket,user,context))
    db.commit()


def init_app(app):
    @app.get("/api/statistics")
    def public_account_statistics():
        window = request.args.get("window", "24h")
        context = request.args.get("context", "public")
        if window not in {"60m", "24h", "30d"} or context not in {"public", "internal", "test", "all"}:
            return jsonify({"error": "Invalid statistics filter."}), 400
        cutoff = int(time.time()) // 60 * 60 - {"60m": 3600, "24h": 86400, "30d": 30*86400}[window]
        args = [cutoff]
        where = "bucket >= ?"
        if context != "all":
            where += " AND context=?"
            args.append(context)
        db = get_db()
        rows = db.execute(f"SELECT event,method,SUM(count) AS count FROM account_counters WHERE {where} GROUP BY event,method ORDER BY event,method", args).fetchall()
        active = db.execute(f"SELECT COUNT(DISTINCT user_id) FROM account_visits WHERE {where}", args).fetchone()[0]
        response = jsonify({"precision": "minute", "from": cutoff, "context": context,
                            "active_accounts": active, "events": [dict(r) for r in rows],
                            "note": "Server-observed account actions since instrumentation was installed. Window rounded to minutes; not historical account totals."})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.after_request
    def observe_account_action(response):
        if 200 <= response.status_code < 300:
            event = {"auth.register": "account_created", "auth.login": "login_success",
                     "projects.create_project": "cloud_save", "projects.update_project": "cloud_save",
                     "projects.read_project": "cloud_load"}.get(request.endpoint)
            if event:
                try:
                    record_account_event(event)
                    if event == "account_created":
                        record_account_event("login_success")
                except sqlite3.Error:
                    app.logger.warning("Account statistics unavailable")
            elif request.endpoint in {"auth.me", "projects.list_projects", "projects.read_project"} and current_user_id():
                # Returning signed-in users need not repeat login to be active.
                context = request.headers.get("X-WebMARS-Context", "public")
                context = context if context in {"public", "internal", "test"} else "public"
                try:
                    cutoff = int(time.time()) - 35 * 86400
                    get_db().execute("DELETE FROM account_visits WHERE bucket < ?", (cutoff,))
                    get_db().execute("DELETE FROM account_counters WHERE bucket < ?", (cutoff,))
                    get_db().execute("INSERT OR IGNORE INTO account_visits VALUES(?,?,?)", (int(time.time())//60*60, current_user_id(), context))
                    get_db().commit()
                except sqlite3.Error:
                    app.logger.warning("Account statistics unavailable")
        return response
