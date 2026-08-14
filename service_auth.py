"""
service_auth.py
-----------------
このサービス単体(gatewayを経由しない直接アクセス)を保護するための、
最小限の認証。

背景:
- docker-compose.yml では、gatewayだけでなく本サービスも個別に
  ホストへポート公開している(直接デバッグ・単体フロントエンド用途)。
- gatewayは auth.py で認証を一手に引き受ける設計だが、本サービス自身が
  無防備だと、そのポートへ直接アクセスするだけで認証を素通りできてしまう。
  digital-vaultは特に重要書類(パスポート等)を扱うため、これは特に問題。

設計方針(gatewayの auth.py と同じ考え方をそのまま踏襲する):
- 新しい認証の仕組みは増やさない。gatewayが発行する `gw_session` Cookie
  (値は GATEWAY_AUTH_TOKEN と同じ共有シークレット)をそのまま検証するだけ。
- gateway経由のリクエストは、gatewayの `_proxy()` がCookieヘッダーを含む
  全ヘッダーを転送するため、追加の変更なしにそのまま認証が通る。
- ログイン/ログアウトのエンドポイントはこのサービスには持たせない
  (ログインは常にgateway側で行う。ここは検証のみ)。
- GATEWAY_AUTH_TOKEN が未設定の場合は、gatewayと同様に起動を拒否する
  (安全側にフェイルする)。
"""
from __future__ import annotations

import hmac
import os

from fastapi import Request
from fastapi.responses import JSONResponse

SESSION_COOKIE_NAME = "gw_session"

# "/" (静的シェルのHTML/JS/CSS) と ヘルスチェックは、gateway側の auth.py と
# 同じ理由で認証対象外にする。実データはここより先の各APIエンドポイントで守る。
_PUBLIC_PATHS = {"/", "/health", "/health_check"}


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/static/")


def get_auth_token() -> str:
    token = os.environ.get("GATEWAY_AUTH_TOKEN", "")
    if not token:
        raise RuntimeError(
            "GATEWAY_AUTH_TOKEN が設定されていません。このサービスはホストへ"
            "個別ポート公開されており、無防備なまま起動しないようにこの"
            "チェックを入れています。docker-compose.yml でgatewayと同じ値の"
            "GATEWAY_AUTH_TOKEN を設定してください。"
        )
    return token


def _is_authenticated(request: Request) -> bool:
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not cookie_value:
        return False
    # タイミング攻撃を避けるため定数時間比較を使う(gatewayのauth.pyと同じ)
    return hmac.compare_digest(cookie_value, get_auth_token())


async def service_auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS" or _is_public(request.url.path):
        return await call_next(request)

    if not _is_authenticated(request):
        return JSONResponse(
            status_code=401,
            content={
                "detail": (
                    "認証が必要です。http://localhost:3000 (gateway) から"
                    "ログインしてください。"
                )
            },
        )
    return await call_next(request)
