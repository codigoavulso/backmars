"""Optional social sign-in. Provider tokens never enter our DB, cookies or UI."""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, current_app, jsonify, make_response, request, session

from auth import _valid_email, establish_user_session
from db import get_db
from models import discard_login_attempt, get_user_by_id, get_user_session_version, reserve_login_attempt, reserve_registration_attempt, utc_now
from utils import client_address, current_user_id, json_error, session_is_current

bp = Blueprint("social_auth", __name__)


def init_app(app):
    oauth = OAuth(app)
    enabled = []
    base = app.config.get("OAUTH_CALLBACK_BASE", "")
    for provider in ("GOOGLE", "FACEBOOK"):
        if bool(app.config.get(f"{provider}_CLIENT_ID")) != bool(app.config.get(f"{provider}_CLIENT_SECRET")):
            raise RuntimeError(f"Configure both {provider}_CLIENT_ID and {provider}_CLIENT_SECRET.")
    if any(app.config.get(k) for k in ("GOOGLE_CLIENT_ID", "FACEBOOK_CLIENT_ID")):
        parsed = urlsplit(base)
        if (not parsed.netloc or parsed.query or parsed.fragment or parsed.username
                or (parsed.scheme != "https" and not (app.debug and parsed.hostname in {"localhost", "127.0.0.1"}))):
            raise RuntimeError("Configure BACKMARS_OAUTH_CALLBACK_BASE with the public HTTPS API URL (ending in /api).")
    if app.config.get("GOOGLE_CLIENT_ID") and app.config.get("GOOGLE_CLIENT_SECRET"):
        oauth.register("google", client_id=app.config["GOOGLE_CLIENT_ID"],
                       client_secret=app.config["GOOGLE_CLIENT_SECRET"],
                       server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                       client_kwargs={"scope": "openid email", "code_challenge_method": "S256", "timeout": 15})
        enabled.append("google")
    if app.config.get("FACEBOOK_CLIENT_ID") and app.config.get("FACEBOOK_CLIENT_SECRET"):
        version = app.config.get("FACEBOOK_API_VERSION", "")
        if not re.fullmatch(r"v[1-9][0-9]*\.0", version):
            raise RuntimeError("Set BACKMARS_FACEBOOK_API_VERSION to a supported version, e.g. v25.0.")
        oauth.register("facebook", client_id=app.config["FACEBOOK_CLIENT_ID"],
                       client_secret=app.config["FACEBOOK_CLIENT_SECRET"],
                       authorize_url=f"https://www.facebook.com/{version}/dialog/oauth",
                       access_token_url=f"https://graph.facebook.com/{version}/oauth/access_token",
                       api_base_url=f"https://graph.facebook.com/{version}/",
                       client_kwargs={"scope": "email", "token_endpoint_auth_method": "client_secret_post", "timeout": 15})
        enabled.append("facebook")
    app.extensions["social_login"] = {"oauth": oauth, "providers": enabled}
    app.register_blueprint(bp)


def _provider(name):
    ext = current_app.extensions["social_login"]
    return ext["oauth"].create_client(name) if name in ext["providers"] else None


@bp.get("/api/auth/providers")
def providers():
    connected = []
    if session_is_current():
        connected = [r[0] for r in get_db().execute("SELECT provider FROM external_identities WHERE user_id=?", (current_user_id(),))]
    return jsonify({"providers": current_app.extensions["social_login"]["providers"], "connected": connected})


@bp.post("/api/auth/<name>/start")
def start(name):
    client = _provider(name)
    if client is None:
        return json_error("This sign-in provider is unavailable.", 404)
    # Unlike ordinary API writes, require an Origin for starting a login/link.
    from app import allowed_origins
    if request.headers.get("Origin") not in allowed_origins():
        return json_error("Origin is not allowed.", 403)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("intent", "login") not in {"login", "link"}:
        return json_error("Invalid sign-in request.")
    link = data.get("intent") == "link"
    if link and not session_is_current():
        return json_error("Sign in to your existing account before linking a provider.", 401)
    attempt_id = reserve_login_attempt(client_address(), 900, 30)
    if attempt_id is None:
        return json_error("Too many sign-in attempts. Try again later.", 429)
    state = secrets.token_urlsafe(32)
    binding = session.setdefault("oauth_binding", secrets.token_urlsafe(32))
    db = get_db()
    db.execute("DELETE FROM oauth_flows WHERE expires_at < ?", (int(time.time()),))
    context = data.get("context", "public")
    if context not in {"public", "internal", "test"}:
        return json_error("Invalid usage context.")
    db.execute("INSERT INTO oauth_flows(state,binding_hash,provider,link_user_id,session_version,expires_at,context,attempt_id) VALUES (?,?,?,?,?,?,?,?)",
               (state, hashlib.sha256(binding.encode()).hexdigest(), name, current_user_id() if link else None,
                get_user_session_version(current_user_id()) if link else None, int(time.time()) + 600, context, attempt_id))
    db.commit()
    callback = current_app.config["OAUTH_CALLBACK_BASE"] + f"/auth/{name}/callback"
    response = client.authorize_redirect(callback, state=state, **({"nonce": secrets.token_urlsafe(32)} if name == "google" else {}))
    return jsonify({"url": response.location, "state": state})


def _resolve_identity(name, subject, email, verified, flow):
    """One transaction: never merge accounts merely because emails match."""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        identity = db.execute("SELECT user_id FROM external_identities WHERE provider=? AND subject=?", (name, subject)).fetchone()
        link_id = flow["link_user_id"]
        if identity:
            if link_id and identity[0] != link_id:
                raise ValueError("identity_conflict")
            db.commit()
            return identity[0], False
        if link_id:
            user_id = link_id
        else:
            if not _valid_email(email) or (name == "google" and not verified):
                raise ValueError("verified_email_required")
            if db.execute("SELECT 1 FROM users WHERE lower(email)=?", (email,)).fetchone():
                raise ValueError("existing_account")
            username = "user-" + secrets.token_hex(6)
            now = utc_now()
            user_id = db.execute("INSERT INTO users(username,email,password_hash,created_at,email_verified_at) VALUES (?,?,?, ?,?)",
                                 (username, email, "", now, now if verified else None)).lastrowid
        db.execute("INSERT INTO external_identities VALUES (?,?,?,?)", (name, subject, user_id, utc_now()))
        db.commit()
        return user_id, not bool(link_id)
    except Exception:
        db.rollback()
        raise


def _finish(status, provider, state):
    # The opener verifies /api/me before accepting success. No credentials here.
    import json
    origin_parts = urlsplit(current_app.config["WEBMARS_PUBLIC_URL"])
    origin = f"{origin_parts.scheme}://{origin_parts.netloc}"
    nonce = secrets.token_urlsafe(24)
    payload = json.dumps({"type": "webmars-social-auth", "status": status, "provider": provider, "state": state}).replace("<", "\\u003c")
    response = make_response(f'<!doctype html><html lang="en"><meta charset="utf-8"><title>webMARS Cloud</title>'
                             f'<p>Return to webMARS to continue. You can close this window.</p>'
                             f'<script nonce="{nonce}">if(window.opener)window.opener.postMessage({payload},{json.dumps(origin)});window.close();</script></html>')
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = f"default-src 'none'; script-src 'nonce-{nonce}'; frame-ancestors 'none'; base-uri 'none'"
    return response


@bp.get("/api/auth/<name>/callback")
def callback(name):
    state = request.args.get("state", "")
    client = _provider(name)
    if client is None or not re.fullmatch(r"[A-Za-z0-9_-]{40,80}", state):
        return _finish("invalid_state", name, "")
    db = get_db()
    flow = db.execute("SELECT * FROM oauth_flows WHERE state=?", (state,)).fetchone()
    binding = hashlib.sha256(str(session.get("oauth_binding", "")).encode()).hexdigest()
    if not flow or flow["provider"] != name or flow["binding_hash"] != binding or flow["expires_at"] < time.time() or flow["consumed"]:
        return _finish("invalid_state", name, state)
    if flow["link_user_id"] and (not session_is_current() or current_user_id() != flow["link_user_id"] or get_user_session_version(current_user_id()) != flow["session_version"]):
        return _finish("invalid_state", name, state)
    # Atomic consumption also stops callback replay with an old signed cookie.
    consumed = db.execute("UPDATE oauth_flows SET consumed=1 WHERE state=? AND consumed=0", (state,)).rowcount
    db.commit()
    if not consumed:
        return _finish("invalid_state", name, state)
    try:
        token = client.authorize_access_token()
        if name == "google":
            info = token.get("userinfo", {})  # Authlib verifies signature, issuer, audience, expiry and nonce.
            subject, email, verified = info.get("sub"), str(info.get("email", "")).lower(), info.get("email_verified") is True
        else:
            proof = __import__("hmac").new(current_app.config["FACEBOOK_CLIENT_SECRET"].encode(), token["access_token"].encode(), hashlib.sha256).hexdigest()
            res = client.get("me", token=token, params={"fields": "id,email", "appsecret_proof": proof})
            res.raise_for_status()
            info = res.json()
            subject, email = info.get("id"), str(info.get("email", "")).lower()
            # Facebook email is not treated as proof of ownership. Existing
            # local accounts can link it; first-time Facebook users verify email.
            verified = False
        if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
            raise ValueError("invalid_identity")
        known = db.execute("SELECT 1 FROM external_identities WHERE provider=? AND subject=?", (name, subject)).fetchone()
        if not flow["link_user_id"] and not known and reserve_registration_attempt(client_address(), 3600, 20) is None:
            raise ValueError("rate_limited")
        user_id, created = _resolve_identity(name, subject, email, verified, flow)
        if flow["attempt_id"]:
            discard_login_attempt(flow["attempt_id"])
        establish_user_session(user_id)
        from account_statistics import record_account_event
        try:
            if created:
                record_account_event("account_created", name, flow["context"])
            record_account_event("account_linked" if flow["link_user_id"] else "login_success", name, flow["context"])
        except sqlite3.Error:
            current_app.logger.warning("Account statistics unavailable")
        if created and name == "facebook":
            from auth import _issue_account_code, EMAIL_VERIFICATION_PURPOSE
            from mailer import mail_is_configured
            if mail_is_configured():
                try:
                    _issue_account_code(get_user_by_id(user_id), EMAIL_VERIFICATION_PURPOSE, client_address())
                except Exception:
                    current_app.logger.warning("Verification email could not be issued")
        return _finish("linked" if flow["link_user_id"] else ("created" if created else "success"), name, state)
    except (ValueError, sqlite3.IntegrityError) as error:
        status = str(error) if str(error) in {"existing_account", "verified_email_required", "identity_conflict", "rate_limited"} else "sign_in_failed"
        return _finish(status, name, state)
    except Exception:
        # Provider errors can contain tokens/authorization codes. Never log them.
        return _finish("sign_in_failed", name, state)
