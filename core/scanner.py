"""
core/scanner.py — фоновый сканер + индексация одного файла
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.database import PREVIEW_DIR, STLModel, get_by_hash, get_by_path, get_session
from core.mesh import MeshMeta, auto_description, extract_meta, render_preview, sha256

log = logging.getLogger(__name__)


@dataclass
class ScanProgress:
    total:        int  = 0
    current:      int  = 0
    current_file: str  = ""
    added:        int  = 0
    dupes:        int  = 0
    errors:       int  = 0
    done:         bool = False
    cancelled:    bool = False
    error_msg:    str  = ""


class ScanJob:
    def __init__(self, directory: str, recursive: bool = True):
        self.directory = Path(directory)
        self.recursive = recursive
        self.progress  = ScanProgress()
        self._stop     = threading.Event()
        self._thread   = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        p = self.progress
        try:
            pattern = "**/*.stl" if self.recursive else "*.stl"
            files = sorted({
                *self.directory.glob(pattern),
                *self.directory.glob(pattern.upper()),
            })
            p.total = len(files)

            session = get_session()
            try:
                for idx, path in enumerate(files):
                    if self._stop.is_set():
                        p.cancelled = True
                        break
                    p.current = idx + 1
                    p.current_file = path.name
                    _process_file(session, path, p)
            finally:
                session.close()
        except Exception as e:
            log.exception("scanner error")
            p.error_msg = str(e)
        finally:
            p.done = True


def _process_file(session, path: Path, p: ScanProgress | None = None) -> STLModel | None:
    """Общая логика обработки одного STL файла."""
    try:
        h = sha256(path)
    except OSError as e:
        log.warning("read error %s: %s", path, e)
        if p:
            p.errors += 1
        return None

    if get_by_hash(session, h):
        if p:
            p.dupes += 1
        return None

    meta = extract_meta(path)
    desc = auto_description(path.stem, meta)

    preview_file = PREVIEW_DIR / f"{h[:16]}.png"
    if not preview_file.exists():
        render_preview(path, preview_file)

    existing = get_by_path(session, str(path))
    if existing:
        existing.sha256        = h
        existing.face_count    = meta.face_count
        existing.vertex_count  = meta.vertex_count
        existing.volume_mm3    = meta.volume_mm3
        existing.surface_mm2   = meta.surface_mm2
        existing.bbox_x        = meta.bbox_x
        existing.bbox_y        = meta.bbox_y
        existing.bbox_z        = meta.bbox_z
        existing.is_watertight = meta.is_watertight
        existing.file_size_kb  = round(path.stat().st_size / 1024, 1)
        existing.preview_path  = str(preview_file) if preview_file.exists() else None
        existing.description   = desc
        session.commit()
        if p:
            p.added += 1
        return existing
    else:
        now = datetime.utcnow()
        model = STLModel(
            name          = path.stem,
            file_path     = str(path),
            sha256        = h,
            face_count    = meta.face_count,
            vertex_count  = meta.vertex_count,
            volume_mm3    = meta.volume_mm3,
            surface_mm2   = meta.surface_mm2,
            bbox_x        = meta.bbox_x,
            bbox_y        = meta.bbox_y,
            bbox_z        = meta.bbox_z,
            is_watertight = meta.is_watertight,
            file_size_kb  = round(path.stat().st_size / 1024, 1),
            preview_path  = str(preview_file) if preview_file.exists() else None,
            description   = desc,
            uploaded_at   = now,
            indexed_at    = now,
        )
        session.add(model)
        session.flush()
        session.commit()
        if p:
            p.added += 1
        return model


def index_single_file(path: Path) -> tuple[bool, str, int | None]:
    """
    Синхронная индексация одного файла.
    Возвращает (success, message, model_id).
    """
    session = get_session()
    try:
        model = _process_file(session, path)
        if model is None:
            # проверим дубликат
            try:
                h = sha256(path)
                existing = get_by_hash(session, h)
                if existing:
                    return False, "Файл уже в библиотеке (дубликат)", existing.id
            except Exception:
                pass
            return False, "Файл уже существует или ошибка чтения", None
        return True, f"Добавлен: {path.name}", model.id
    except Exception as e:
        log.exception("index_single_file error")
        return False, str(e), None
    finally:
        session.close()
