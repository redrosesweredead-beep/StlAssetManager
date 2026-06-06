"""
core/database.py — SQLAlchemy ORM + автомиграция
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text,
    create_engine, event
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

log = logging.getLogger(__name__)

BASE_DIR    = Path(__file__).parent.parent
DATA_DIR    = BASE_DIR / "data"
UPLOAD_DIR  = DATA_DIR / "uploads"
PREVIEW_DIR = BASE_DIR / "static" / "previews"
DB_PATH     = DATA_DIR / "library.db"

for _d in (DATA_DIR, UPLOAD_DIR, PREVIEW_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ── ORM ──────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


class STLModel(Base):
    __tablename__ = "stl_models"

    id            = Column(Integer,      primary_key=True, autoincrement=True)
    name          = Column(String(256),  nullable=False, index=True)
    file_path     = Column(String(1024), nullable=False, unique=True)
    sha256        = Column(String(64),   nullable=False, unique=True, index=True)

    face_count    = Column(Integer, nullable=True)
    vertex_count  = Column(Integer, nullable=True)
    volume_mm3    = Column(Float,   nullable=True)
    surface_mm2   = Column(Float,   nullable=True)
    bbox_x        = Column(Float,   nullable=True)
    bbox_y        = Column(Float,   nullable=True)
    bbox_z        = Column(Float,   nullable=True)
    is_watertight = Column(Boolean, default=False)
    file_size_kb  = Column(Float,   nullable=True)

    preview_path  = Column(String(1024), nullable=True)
    description   = Column(Text,    default="")
    tags          = Column(Text,    default="")
    notes         = Column(Text,    default="")
    category      = Column(String(128), default="")

    uploaded_at   = Column(DateTime, default=datetime.utcnow)   # редактируемая
    indexed_at    = Column(DateTime, default=datetime.utcnow)   # системная

    def tag_list(self) -> list[str]:
        return [t.strip() for t in (self.tags or "").split(",") if t.strip()]

    def preview_url(self) -> str | None:
        if self.preview_path and Path(self.preview_path).exists():
            return "/static/previews/" + Path(self.preview_path).name
        return None

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "name":         self.name,
            "file_path":    self.file_path,
            "face_count":   self.face_count,
            "vertex_count": self.vertex_count,
            "volume_mm3":   round(self.volume_mm3, 3) if self.volume_mm3 else None,
            "surface_mm2":  round(self.surface_mm2, 3) if self.surface_mm2 else None,
            "bbox_x":       round(self.bbox_x, 2) if self.bbox_x else None,
            "bbox_y":       round(self.bbox_y, 2) if self.bbox_y else None,
            "bbox_z":       round(self.bbox_z, 2) if self.bbox_z else None,
            "is_watertight":self.is_watertight,
            "file_size_kb": round(self.file_size_kb, 1) if self.file_size_kb else None,
            "preview_url":  self.preview_url(),
            "description":  self.description or "",
            "tags":         self.tags or "",
            "notes":        self.notes or "",
            "category":     self.category or "",
            "uploaded_at":  self.uploaded_at.strftime("%Y-%m-%d %H:%M") if self.uploaded_at else "",
            "indexed_at":   self.indexed_at.strftime("%Y-%m-%d %H:%M") if self.indexed_at else "",
        }


# ── Автомиграция ─────────────────────────────────────────────────────────
_MIGRATIONS: dict[str, str] = {
    "face_count":    "ALTER TABLE stl_models ADD COLUMN face_count INTEGER",
    "vertex_count":  "ALTER TABLE stl_models ADD COLUMN vertex_count INTEGER",
    "volume_mm3":    "ALTER TABLE stl_models ADD COLUMN volume_mm3 REAL",
    "surface_mm2":   "ALTER TABLE stl_models ADD COLUMN surface_mm2 REAL",
    "bbox_x":        "ALTER TABLE stl_models ADD COLUMN bbox_x REAL",
    "bbox_y":        "ALTER TABLE stl_models ADD COLUMN bbox_y REAL",
    "bbox_z":        "ALTER TABLE stl_models ADD COLUMN bbox_z REAL",
    "is_watertight": "ALTER TABLE stl_models ADD COLUMN is_watertight BOOLEAN DEFAULT 0",
    "file_size_kb":  "ALTER TABLE stl_models ADD COLUMN file_size_kb REAL",
    "preview_path":  "ALTER TABLE stl_models ADD COLUMN preview_path TEXT",
    "description":   "ALTER TABLE stl_models ADD COLUMN description TEXT DEFAULT ''",
    "tags":          "ALTER TABLE stl_models ADD COLUMN tags TEXT DEFAULT ''",
    "notes":         "ALTER TABLE stl_models ADD COLUMN notes TEXT DEFAULT ''",
    "category":      "ALTER TABLE stl_models ADD COLUMN category TEXT DEFAULT ''",
    "uploaded_at":   "ALTER TABLE stl_models ADD COLUMN uploaded_at DATETIME",
    "indexed_at":    "ALTER TABLE stl_models ADD COLUMN indexed_at DATETIME",
}


def _auto_migrate(engine) -> None:
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA table_info(stl_models)")
        existing = {row[1] for row in cur.fetchall()}
        for col, ddl in _MIGRATIONS.items():
            if col not in existing:
                log.info("migrate: ADD COLUMN %s", col)
                cur.execute(ddl)
        raw.commit()
    finally:
        raw.close()


# ── Engine & Session ──────────────────────────────────────────────────────
_engine = None
_Session: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            f"sqlite:///{DB_PATH}",
            connect_args={"check_same_thread": False},
            echo=False,
        )

        @event.listens_for(_engine, "connect")
        def _pragma(conn, _):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

        Base.metadata.create_all(_engine)
        _auto_migrate(_engine)
    return _engine


def get_session() -> Session:
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _Session()


# ── Запросы ───────────────────────────────────────────────────────────────
def all_models(session: Session,
               sort: str = "uploaded_at",
               order: str = "desc",
               watertight_only: bool = False,
               category: str = "",
               size_min: float = 0,
               size_max: float = 0) -> list[STLModel]:

    SORT_MAP = {
        "uploaded_at": STLModel.uploaded_at,
        "indexed_at":  STLModel.indexed_at,
        "name":        STLModel.name,
        "file_size_kb":STLModel.file_size_kb,
        "face_count":  STLModel.face_count,
    }
    col = SORT_MAP.get(sort, STLModel.uploaded_at)
    col = col.asc() if order == "asc" else col.desc()

    q = session.query(STLModel)
    if watertight_only:
        q = q.filter(STLModel.is_watertight == True)
    if category:
        q = q.filter(STLModel.category == category)
    if size_min > 0:
        q = q.filter(STLModel.file_size_kb >= size_min)
    if size_max > 0:
        q = q.filter(STLModel.file_size_kb <= size_max)
    return q.order_by(col).all()


def search_models(session: Session, text: str,
                  sort: str = "uploaded_at", order: str = "desc") -> list[STLModel]:
    like = f"%{text}%"
    SORT_MAP = {
        "uploaded_at": STLModel.uploaded_at,
        "indexed_at":  STLModel.indexed_at,
        "name":        STLModel.name,
        "file_size_kb":STLModel.file_size_kb,
        "face_count":  STLModel.face_count,
    }
    col = SORT_MAP.get(sort, STLModel.uploaded_at)
    col = col.asc() if order == "asc" else col.desc()
    return (
        session.query(STLModel)
        .filter(
            STLModel.name.ilike(like) |
            STLModel.description.ilike(like) |
            STLModel.tags.ilike(like) |
            STLModel.category.ilike(like)
        )
        .order_by(col)
        .all()
    )


def get_by_hash(session: Session, h: str) -> STLModel | None:
    return session.query(STLModel).filter_by(sha256=h).first()


def get_by_path(session: Session, path: str) -> STLModel | None:
    return session.query(STLModel).filter_by(file_path=path).first()


def get_by_id(session: Session, mid: int) -> STLModel | None:
    return session.get(STLModel, mid)


def delete_model(session: Session, mid: int) -> bool:
    m = session.get(STLModel, mid)
    if m:
        session.delete(m)
        session.commit()
        return True
    return False


def db_stats(session: Session) -> dict:
    total = session.query(STLModel).count()
    wt    = session.query(STLModel).filter_by(is_watertight=True).count()
    rows  = session.query(STLModel.file_size_kb).all()
    mb    = round(sum(r[0] or 0 for r in rows) / 1024, 1)
    cats  = session.query(STLModel.category).distinct().all()
    categories = sorted({r[0] for r in cats if r[0]})
    return {"total": total, "watertight": wt, "size_mb": mb, "categories": categories}
