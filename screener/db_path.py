"""ローカル SQLite ファイルパスの解決（DATABASE_URL 未設定時のフォールバック）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# プロジェクトルート（screener/ の1つ上）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ローカル開発のデフォルト
DEFAULT_LOCAL_RELATIVE = Path("data") / "screener.db"

# Render Persistent Disk の標準マウント先
DEFAULT_RENDER_DISK_MOUNT = "/var/data"
DEFAULT_RENDER_DB_FILENAME = "screener.db"


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def resolve_db_path() -> Path:
    """
    DB ファイルパスを決定する。

    優先順位:
      1. DB_PATH 環境変数（明示指定）
      2. Render 本番 + Persistent Disk マウント先が書き込み可能
      3. プロジェクト直下 data/screener.db（ローカル開発）
    """
    explicit = os.getenv("DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    if os.getenv("RENDER") == "true":
        mount = Path(os.getenv("RENDER_DISK_MOUNT", DEFAULT_RENDER_DISK_MOUNT))
        if _is_writable_dir(mount):
            return mount / os.getenv("RENDER_DB_FILENAME", DEFAULT_RENDER_DB_FILENAME)

    return PROJECT_ROOT / DEFAULT_LOCAL_RELATIVE


def resolve_disk_mount() -> Optional[Path]:
    """Persistent Disk のマウントディレクトリ（該当しない場合は None）。"""
    mount_env = os.getenv("RENDER_DISK_MOUNT", DEFAULT_RENDER_DISK_MOUNT).strip()
    if mount_env:
        mount = Path(mount_env)
        if mount.exists() and mount.is_dir():
            return mount
    return None


def is_persistent_storage(db_path: Path) -> bool:
    """
    再起動・再デプロイ後もデータが残るストレージかどうか。

    - Render: Persistent Disk 配下 (/var/data 等) のみ True
    - Vercel /tmp: False
    - ローカル data/: True（開発者のディスク上）
    """
    resolved = db_path.resolve()
    path_str = str(resolved)

    if os.getenv("VERCEL") == "1" or path_str.startswith("/tmp"):
        return False

    if os.getenv("RENDER") == "true":
        mount = resolve_disk_mount()
        if mount is None:
            return False
        try:
            resolved.relative_to(mount.resolve())
            return True
        except ValueError:
            return False

    # ローカル: プロジェクト配下 data/ は永続
    try:
        resolved.relative_to(PROJECT_ROOT / "data")
        return True
    except ValueError:
        pass

    # 明示 DB_PATH がプロジェクト外の絶対パスでも、Render 以外なら永続とみなす
    return resolved.is_absolute() and os.getenv("RENDER") != "true"
