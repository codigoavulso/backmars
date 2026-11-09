"""Server-observed adoption, with aggregate output and no analytics identity."""
import hashlib
import json
import re
import sqlite3
import time
from flask import current_app, jsonify, request
from urllib.request import Request, urlopen
from db import get_db
from utils import current_user_id, login_required, session_is_current


def internal_account_ids():
    ids = {int(value) for value in current_app.config["INTERNAL_USER_IDS"].split(",")
           if re.fullmatch(r"[0-9]{1,16}", value.strip())}
    names = {value.strip().casefold() for value in current_app.config["INTERNAL_USERNAMES"].split(",") if value.strip()}
    if names:
        ids.update(row["id"] for row in get_db().execute("SELECT id, username FROM users")
                   if row["username"].casefold() in names)
    return sorted(ids)


def account_context(context=None):
    if session_is_current() and current_user_id() in internal_account_ids():
        return "internal"
    context = context or request.headers.get("X-WebMARS-Context", "public")
    return context if context in {"public", "internal", "test"} else "public"


def record_account_event(event, method="session", context=None):
    context = account_context(context)
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
    @app.post("/api/statistics/session")
    @login_required
    def associate_metrics_session():
        data = request.get_json(silent=True)
        token = data.get("session") if isinstance(data, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            return jsonify({"error": "Invalid metrics session."}), 400
        db = get_db()
        user = current_user_id()
        now = int(time.time())
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        db.execute("DELETE FROM account_metric_sessions WHERE linked_at < ?", (now - 35*86400,))
        db.execute("INSERT OR IGNORE INTO account_metric_sessions VALUES(?,?,?)", (token_hash, user, now))
        owner = db.execute("SELECT user_id FROM account_metric_sessions WHERE session_hash=?", (token_hash,)).fetchone()
        db.commit()
        if owner["user_id"] != user:
            return jsonify({"error": "Metrics session is already associated."}), 409
        if user in internal_account_ids() and app.config["METRICS_INTERNAL_RELAY"]:
            # The collector binds to loopback and this path is not proxied.
            try:
                payload = json.dumps({"session": token}).encode("ascii")
                relay = Request("http://127.0.0.1:8766/associate", data=payload,
                                headers={"Content-Type": "application/json"})
                with urlopen(relay, timeout=2) as response:
                    if response.status != 204:
                        return jsonify({"error": "Metrics collector unavailable."}), 503
            except OSError:
                return jsonify({"error": "Metrics collector unavailable."}), 503
        return "", 204


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
        # Cloud reads/writes use an authenticated session, not necessarily a
        # password login. Normalize historical defaults without rewriting data.
        method = "CASE WHEN event IN ('cloud_load','cloud_save') THEN 'session' ELSE method END"
        rows = db.execute(f"SELECT event,{method} AS method,SUM(count) AS count FROM account_counters WHERE {where} GROUP BY event,{method} ORDER BY event,method", args).fetchall()
        account_args = [cutoff]
        account_where = "bucket >= ?"
        internal = internal_account_ids()
        if context != "all":
            account_where += " AND context=?"
            account_args.append(context)
            if internal:
                placeholders = ",".join("?" for _ in internal)
                if context == "internal":
                    account_where = f"bucket >= ? AND (context=? OR user_id IN ({placeholders}))"
                else:
                    account_where += f" AND user_id NOT IN ({placeholders})"
                account_args.extend(internal)
        active = db.execute(f"SELECT COUNT(DISTINCT user_id) FROM account_visits WHERE {account_where}", account_args).fetchone()[0]
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
                    record_account_event(event, "password" if event in {"account_created", "login_success"} else "session")
                    if event == "account_created":
                        record_account_event("login_success", "password")
                except sqlite3.Error:
                    app.logger.warning("Account statistics unavailable")
            elif request.endpoint in {"auth.me", "projects.list_projects", "projects.read_project"} and session_is_current() and current_user_id():
                # Returning signed-in users need not repeat login to be active.
                context = account_context()
                try:
                    cutoff = int(time.time()) - 35 * 86400
                    get_db().execute("DELETE FROM account_visits WHERE bucket < ?", (cutoff,))
                    get_db().execute("DELETE FROM account_counters WHERE bucket < ?", (cutoff,))
                    get_db().execute("INSERT OR IGNORE INTO account_visits VALUES(?,?,?)", (int(time.time())//60*60, current_user_id(), context))
                    get_db().commit()
                except sqlite3.Error:
                    app.logger.warning("Account statistics unavailable")
        return response
