"""
core/mesh.py — хэш, метаданные, превью
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")

_COLORS = ["#4fc3f7","#81c784","#ffb74d","#ce93d8","#f48fb1","#80deea","#fff176","#a5d6a7"]


def mesh_color(name: str) -> str:
    return _COLORS[sum(ord(c) for c in name) % len(_COLORS)]


def sha256(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while data := f.read(chunk):
            h.update(data)
    return h.hexdigest()


@dataclass
class MeshMeta:
    face_count:    int   | None = None
    vertex_count:  int   | None = None
    volume_mm3:    float | None = None
    surface_mm2:   float | None = None
    bbox_x:        float | None = None
    bbox_y:        float | None = None
    bbox_z:        float | None = None
    is_watertight: bool = False


def extract_meta(path: str | Path) -> MeshMeta:
    try:
        import trimesh
        m = trimesh.load(str(path), force="mesh")
        if not isinstance(m, trimesh.Trimesh):
            return MeshMeta()
        ext = m.bounding_box.extents
        meta = MeshMeta(
            face_count    = int(len(m.faces)),
            vertex_count  = int(len(m.vertices)),
            surface_mm2   = float(m.area),
            is_watertight = bool(m.is_watertight),
            bbox_x=float(ext[0]), bbox_y=float(ext[1]), bbox_z=float(ext[2]),
        )
        if m.is_watertight:
            meta.volume_mm3 = float(abs(m.volume))
        return meta
    except Exception as e:
        log.warning("meta failed %s: %s", path, e)
        return MeshMeta()


def render_preview(stl_path: str | Path, out_path: str | Path) -> bool:
    try:
        import pyvista as pv
        mesh = pv.read(str(stl_path))
        if mesh.n_points == 0:
            return False
        pl = pv.Plotter(off_screen=True, window_size=[480, 480])
        pl.set_background("#0d1117")
        pl.add_mesh(
            mesh,
            color=mesh_color(Path(stl_path).stem),
            specular=0.7, specular_power=20,
            smooth_shading=True, show_edges=False,
            ambient=0.15, diffuse=0.85,
        )
        pl.camera_position = "iso"
        pl.reset_camera()
        pl.camera.zoom(0.82)
        pl.screenshot(str(out_path), transparent_background=False)
        pl.close()
        return Path(out_path).exists()
    except Exception as e:
        log.warning("preview failed %s: %s", stl_path, e)
        return False


def auto_description(name: str, meta: MeshMeta) -> str:
    """Автоматически сгенерированное описание из метаданных."""
    parts = [f"STL модель: {name}."]
    if meta.bbox_x:
        parts.append(
            f"Размеры: {meta.bbox_x:.1f}×{meta.bbox_y:.1f}×{meta.bbox_z:.1f} mm."
        )
    if meta.face_count:
        parts.append(f"Полигонов: {meta.face_count:,}.")
    if meta.volume_mm3:
        parts.append(f"Объём: {meta.volume_mm3:.2f} mm³.")
    parts.append("Замкнутая геометрия." if meta.is_watertight else "Незамкнутая геометрия.")
    return " ".join(parts)
