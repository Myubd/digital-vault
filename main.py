"""
digital-vault / main.py
-------------------------
デジタル金庫プラグインの最小実装。health-support/study-supportで確立した
パターン(memory書き込み・schedule書き込み・権限が無くても本体機能は
落とさない設計)をそのまま踏襲する。

【他アプリとの違い】このアプリだけは、保存するデータ本体(ciphertext/iv)を
サーバー側で一切復号しない(ゼロ知識設計)。サーバーが平文で扱うのは
category(粗い分類)とexpiry_date(有効期限)だけで、タイトル・メモ・
ファイル本体はすべてクライアント側(ブラウザのWeb Crypto API)で
暗号化された状態のまま保存・返却する。鍵はサーバーのどこにも保存しない。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import date, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from local_ai_core.bootstrap import bootstrap_app
from local_ai_core.paths import get_core_db_path
from local_ai_core.permissions import PermissionGate, PermissionDenied
from local_ai_core.memory import MemoryStore
from local_ai_core.schedule import ScheduleStore

from service_auth import service_auth_middleware, get_auth_token

APP_KEY = "digital_vault"
_PLUGIN_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "plugin_manifest.json")
_VAULT_DB_PATH = os.environ.get("VAULT_DB_PATH", "/app/data/digital_vault.db")
_EXPIRY_ALERT_DAYS = 30  # この日数以内に期限が来る書類があれば memory に反映

_VALID_CATEGORIES = {"passport", "insurance", "license", "warranty", "other"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("digital_vault")

_profile_id: Optional[int] = None
_gate: Optional[PermissionGate] = None


def _init_vault_db() -> None:
    os.makedirs(os.path.dirname(_VAULT_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_VAULT_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_key TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL CHECK (category IN
                ('passport', 'insurance', 'license', 'warranty', 'other')),
            expiry_date TEXT,
            ciphertext TEXT NOT NULL,
            iv TEXT NOT NULL,
            schedule_synced INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """
    )
    conn.commit()
    conn.close()


@contextmanager
def _vault_db():
    conn = sqlite3.connect(_VAULT_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _profile_id, _gate
    # このサービスは個別ポート(8300)でホストへ直接公開されうるため、
    # gatewayと同様にトークン未設定のまま起動しないことを最初に確認する。
    get_auth_token()
    _init_vault_db()
    _profile_id = bootstrap_app(_PLUGIN_MANIFEST_PATH, default_profile_display_name="デフォルトプロフィール")
    _gate = PermissionGate(get_core_db_path())
    logger.info("digital_vault bootstrap done (profile_id=%s)", _profile_id)
    yield


app = FastAPI(title="Digital Vault backend", lifespan=lifespan)
# 登録順序が重要(gatewayのmain.pyと同じ理由): CORSを外側(先)、認証を内側(後)にする。
app.middleware("http")(service_auth_middleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health_check")
def health_check():
    return {"ok": True, "profile_id": _profile_id}


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/", response_class=HTMLResponse)
def frontend():
    """暗号化/復号(Web Crypto API)を行う唯一の画面(static/index.html)。
    鍵はここでしか扱わない(サーバーには一切送らない)。
    """
    index_path = os.path.join(_STATIC_DIR, "index.html")
    with open(index_path, encoding="utf-8") as f:
        return f.read()


class VaultItemIn(BaseModel):
    category: str  # passport / insurance / license / warranty / other
    expiry_date: Optional[str] = None  # "YYYY-MM-DD"。指定した場合のみ共通予定表に反映
    ciphertext: str  # base64。中身(title/note/file本体)はクライアント側で暗号化済み
    iv: str  # base64


def _sync_expiry_alert_to_memory() -> None:
    """expiry_dateが_EXPIRY_ALERT_DAYS以内に迫っている書類が1件以上あれば
    「更新期限が近い書類がある」として memory:write:vault.* に反映する。

    health-supportの _sync_condition_trend_to_memory と全く同じ構造:
    memory:write:vault.* が未許可でも保存自体は失敗させず、静かに諦める。
    条件を満たさなくなったら forget() で古い値を残さず消す。

    タイトルはciphertextの中にしか無くサーバーからは見えないため、
    ここで共有できるのは「近々期限が来る書類がある」という事実(category一覧)
    までで、具体的な書類名までは他アプリと共有しない。
    """
    threshold = (date.today() + timedelta(days=_EXPIRY_ALERT_DAYS)).isoformat()
    today = date.today().isoformat()
    with _vault_db() as conn:
        rows = conn.execute(
            "SELECT category FROM vault_items "
            "WHERE expiry_date IS NOT NULL AND expiry_date BETWEEN ? AND ? "
            "ORDER BY expiry_date ASC",
            (today, threshold),
        ).fetchall()
    categories_expiring_soon = [r["category"] for r in rows]
    try:
        mem = MemoryStore(get_core_db_path(), gate=_gate)
        if categories_expiring_soon:
            mem.set(_profile_id, APP_KEY, "vault.expiring_soon", categories_expiring_soon, confidence="ai_inferred")
        else:
            mem.forget(_profile_id, APP_KEY, "vault.expiring_soon")
    except PermissionDenied:
        pass
    except Exception:
        logger.exception("vault.expiring_soonの同期に失敗(保存自体には影響なし)")


def _sync_expiry_to_schedule(item_key: str, category: str, expiry_date: str) -> bool:
    """指定された有効期限を共通の schedule_items に反映する。
    health-supportの _sync_checkup_to_schedule と全く同じ構造。
    """
    try:
        sched = ScheduleStore(get_core_db_path(), gate=_gate)
        sched.upsert(
            _profile_id, APP_KEY,
            source_ref_id=f"vault_item:{item_key}",
            item_type="document_expiry",
            title=f"デジタル金庫: {category}の更新期限",
            due_at=expiry_date,
        )
        return True
    except PermissionDenied:
        return False
    except Exception:
        logger.exception("更新期限の共通予定表への同期に失敗(保存自体には影響なし)")
        return False


@app.put("/{item_key}")
def put_item(item_key: str, body: VaultItemIn):
    if body.category not in _VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"categoryは{sorted(_VALID_CATEGORIES)}のいずれかで指定してください")
    if body.expiry_date:
        try:
            date.fromisoformat(body.expiry_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="expiry_dateはYYYY-MM-DD形式で指定してください")

    with _vault_db() as conn:
        conn.execute(
            """
            INSERT INTO vault_items (item_key, category, expiry_date, ciphertext, iv, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT (item_key) DO UPDATE SET
                category = excluded.category,
                expiry_date = excluded.expiry_date,
                ciphertext = excluded.ciphertext,
                iv = excluded.iv,
                updated_at = datetime('now', 'localtime')
            """,
            (item_key, body.category, body.expiry_date, body.ciphertext, body.iv),
        )

    schedule_synced: Optional[bool] = None
    if body.expiry_date:
        schedule_synced = _sync_expiry_to_schedule(item_key, body.category, body.expiry_date)
        with _vault_db() as conn:
            conn.execute(
                "UPDATE vault_items SET schedule_synced = ? WHERE item_key = ?",
                (1 if schedule_synced else 0, item_key),
            )

    _sync_expiry_alert_to_memory()

    with _vault_db() as conn:
        row = conn.execute("SELECT * FROM vault_items WHERE item_key = ?", (item_key,)).fetchone()
    return {
        "item_key": item_key, "category": row["category"], "expiry_date": row["expiry_date"],
        "schedule_synced": schedule_synced, "updated_at": row["updated_at"],
    }


@app.get("/list")
def list_items():
    """一覧はメタデータのみ(ciphertext/ivも含む。中身が必要ならここで
    まとめて返す方が、health-support等の list_logs と同じくシンプル)。
    /{item_key} より先に宣言する必要がある(FastAPIは宣言順にマッチする
    ため、後に書くと "list" という文字列がitem_keyとして先に食われてしまう)。
    """
    with _vault_db() as conn:
        rows = conn.execute("SELECT * FROM vault_items ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/{item_key}")
def get_item(item_key: str):
    with _vault_db() as conn:
        row = conn.execute("SELECT * FROM vault_items WHERE item_key = ?", (item_key,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="見つかりません")
    return dict(row)


@app.delete("/{item_key}")
def delete_item(item_key: str):
    with _vault_db() as conn:
        cur = conn.execute("DELETE FROM vault_items WHERE item_key = ?", (item_key,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="見つかりません")

    try:
        sched = ScheduleStore(get_core_db_path(), gate=_gate)
        sched.set_status(_profile_id, APP_KEY, source_ref_id=f"vault_item:{item_key}", status="cancelled")
    except PermissionDenied:
        pass
    except Exception:
        logger.exception("更新期限予定のキャンセル同期に失敗(削除自体には影響なし)")

    _sync_expiry_alert_to_memory()
    return {"ok": True}
