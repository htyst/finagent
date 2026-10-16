"""Optional cloud gateway; banking routes, tools and authorizations stay intact.

Only the cloud entry point loads this module. Local app.main:app is unchanged.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import os
from pathlib import Path
import re
import sqlite3

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles


def denial(code: str, message: str, status: int, headers=None):
    return JSONResponse({'status': 'error', 'data': None, 'error_code': code,
                         'message': message}, status_code=status, headers=headers)


class ChatBudget:
    """Persistent global accepted chat/probe count, not a token/currency quota."""
    def __init__(self, path: Path, daily: int, minute: int):
        self.path, self.daily, self.minute = path, daily, minute

    def consume(self, instant=None):
        now = instant or datetime.now(timezone.utc)
        day, minute = now.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%dT%H:%M')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=3) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cloud_budget (scope TEXT, stamp TEXT, count INTEGER NOT NULL, PRIMARY KEY(scope,stamp))')
            db.execute('BEGIN IMMEDIATE')
            for scope, stamp, maximum in [('day', day, self.daily), ('minute', minute, self.minute)]:
                row = db.execute('SELECT count FROM cloud_budget WHERE scope=? AND stamp=?', (scope, stamp)).fetchone()
                if row and row[0] >= maximum:
                    db.rollback()
                    return False
            for scope, stamp in [('day', day), ('minute', minute)]:
                db.execute('INSERT INTO cloud_budget VALUES(?,?,1) ON CONFLICT(scope,stamp) DO UPDATE SET count=count+1', (scope, stamp))
            db.execute("DELETE FROM cloud_budget WHERE scope='minute' AND stamp < ?", (minute,))
            db.execute("DELETE FROM cloud_budget WHERE scope='day' AND stamp < ?", (day,))
            db.commit()
        return True


class CloudGuard(BaseHTTPMiddleware):
    def __init__(self, app, user: str, password: str, budget: ChatBudget):
        super().__init__(app)
        self.user, self.password, self.budget = user.encode(), password.encode(), budget
        self.busy = False

    async def dispatch(self, request, call_next):
        # Health is the only unauthenticated endpoint; it contains no accounts.
        health = request.method == 'GET' and request.url.path == '/api/health'
        costs_model = False
        if not health:
            try:
                scheme, raw = request.headers.get('authorization', '').split(' ', 1)
                decoded = base64.b64decode(raw, validate=True)
                user, password = decoded.split(b':', 1)
                valid = scheme.lower() == 'basic' and hmac.compare_digest(user, self.user) and hmac.compare_digest(password, self.password)
            except (ValueError, binascii.Error):
                valid = False
            if not valid:
                return denial('CLOUD_ACCESS_REQUIRED', '请输入站点所有者提供的演示访问凭据。', 401,
                              {'WWW-Authenticate': 'Basic realm="FinAgent Demo", charset="UTF-8"'})
            length = request.headers.get('content-length', '0')
            if not length.isdigit() or int(length) > 65536:
                return denial('REQUEST_TOO_LARGE', '请求内容过长。', 413)
            costs_model = request.method == 'POST' and (request.url.path in {'/api/tasks', '/api/model/probe'} or
                            re.fullmatch(r'/api/tasks/[^/]+/messages', request.url.path))
            if costs_model:
                if self.busy:
                    return denial('CLOUD_CHAT_BUSY', '另一个演示对话正在处理，请稍后重试。', 429)
                try:
                    allowed = self.budget.consume()
                except (sqlite3.Error, OSError):
                    return denial('CLOUD_BUDGET_UNAVAILABLE', '暂时无法核验演示额度，请稍后重试；仍可查询原任务状态。', 503)
                if not allowed:
                    return denial('CLOUD_CHAT_LIMIT', '云端演示对话额度已用完，请稍后再试；已有回执、任务状态和独立授权入口仍可使用。', 429)
                self.busy = True
        try:
            response = await call_next(request)
        finally:
            if costs_model:
                self.busy = False
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'no-cache'
        return response


def create_cloud_app(banking_app=None, *, static_dir=None, data_dir=None):
    user = os.getenv('FINAGENT_ACCESS_USER', 'guest')
    password = os.getenv('FINAGENT_ACCESS_PASSWORD', '')
    if not user or ':' in user or len(password) < 16:
        raise RuntimeError('云端演示必须配置有效访问用户名及至少16字符的 FINAGENT_ACCESS_PASSWORD。')
    if banking_app is None:
        from app.main import app as banking_app
    from app.config import ROOT
    assets = Path(static_dir or ROOT / 'frontend/dist').resolve()
    if not (assets / 'index.html').is_file() or not (assets / 'assets').is_dir():
        raise RuntimeError('缺少前端生产构建，请使用 Dockerfile.cloud 构建完整云端镜像。')
    directory = Path(data_dir or ROOT / 'var')
    daily, minute = int(os.getenv('CLOUD_DAILY_CHAT_LIMIT', '100')), int(os.getenv('CLOUD_CHAT_PER_MINUTE', '6'))
    if not 1 <= daily <= 10000 or not 1 <= minute <= 60:
        raise RuntimeError('云端演示对话额度配置不合法。')
    budget = ChatBudget(directory / 'cloud-budget.sqlite3', daily, minute)

    @asynccontextmanager
    async def lifespan(application):
        async with banking_app.router.lifespan_context(banking_app):
            yield

    async def page(request):
        return FileResponse(assets / 'index.html')

    application = Starlette(routes=[Route('/api/{rest:path}', banking_app), Route('/api', banking_app),
        Mount('/assets', app=StaticFiles(directory=assets / 'assets')), Route('/{path:path}', page)], lifespan=lifespan)
    application.add_middleware(CloudGuard, user=user, password=password, budget=budget)
    return application


def app_factory():
    return create_cloud_app()
