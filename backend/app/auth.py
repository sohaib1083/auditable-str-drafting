"""Authentication: bcrypt passwords and opaque server-side session tokens.

Roles: analyst (maker), mlro (checker; attests and approves), auditor
(read-only, audit packs and integrity checks). SSO/Keycloak is out of MVP
scope; the role model mirrors the CMS roles so it can be swapped later.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from fastapi import Depends, Header, HTTPException

from app import db
from app.config import settings


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt(rounds=10)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    except ValueError:
        return False


def login(email: str, password: str) -> tuple[str, dict[str, Any]] | None:
    with db.session() as conn:
        u = db.row(conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone())
        if not u or not verify_password(password, u["pw_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)).isoformat()
        conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)", (token, u["id"], exp))
    return token, public(u)


def public(u: dict[str, Any]) -> dict[str, Any]:
    return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"]}


def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not signed in")
    token = authorization[7:]
    conn = db.connect()
    try:
        r = db.row(conn.execute("SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
                                (token,)).fetchone())
    finally:
        conn.close()
    if not r or r["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise HTTPException(401, "Session expired")
    return public(r)


def require(*roles: str):
    def dep(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if user["role"] not in roles:
            raise HTTPException(403, f"This action requires the {' or '.join(roles)} role")
        return user
    return dep
