"""Конфигурация сервиса: YAML-файл плюс переменные окружения для секретов."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class StorageConfig:
    type: str = "local"
    root: str = "./slides"
    recursive: bool = False


@dataclass(frozen=True)
class TileConfig:
    tile_size: int = 510
    overlap: int = 1
    jpeg_quality: int = 80


@dataclass(frozen=True)
class CacheConfig:
    max_gb: float = 20.0


@dataclass(frozen=True)
class AuthConfig:
    https_only: bool = False
    session_hours: int = 12


@dataclass(frozen=True)
class Settings:
    storage: StorageConfig = field(default_factory=StorageConfig)
    tiles: TileConfig = field(default_factory=TileConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    data_dir: Path = BASE_DIR / "data"
    sync_minutes: int = 10
    open_slides: int = 8

    @property
    def db_path(self) -> Path:
        return self.data_dir / "viewer.sqlite3"

    @property
    def tile_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "tiles"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "cache" / "thumbs"


def _section(cls, raw: dict, name: str):
    values = raw.get(name) or {}
    unknown = set(values) - set(cls.__dataclass_fields__)
    if unknown:
        raise ValueError(f"config: неизвестные параметры в разделе {name}: {sorted(unknown)}")
    return cls(**values)


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("VIEWER_CONFIG") or BASE_DIR / "config.yaml")
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    data_dir = Path(raw.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir

    return Settings(
        storage=_section(StorageConfig, raw, "storage"),
        tiles=_section(TileConfig, raw, "tiles"),
        cache=_section(CacheConfig, raw, "cache"),
        auth=_section(AuthConfig, raw, "auth"),
        data_dir=data_dir,
        sync_minutes=int(raw.get("sync_minutes", 10)),
        open_slides=int(raw.get("open_slides", 8)),
    )


def load_secret_key(settings: Settings) -> str:
    """Ключ подписи сессий: из окружения, иначе создаётся и хранится в data_dir."""
    key = os.environ.get("VIEWER_SECRET_KEY")
    if key:
        return key
    key_file = settings.data_dir / "secret.key"
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(secrets.token_urlsafe(48), encoding="ascii")
    return key_file.read_text(encoding="ascii").strip()
