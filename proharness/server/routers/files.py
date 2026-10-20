"""File endpoints — local FS access for the desktop shell.

Read/write/list within the AgentLoop's workdir. The desktop shell (pywebview)
also exposes a native picker via the JS bridge; this REST surface covers the
browser/LAN path. Paths are resolved against the workdir and confined to it
(no path escape).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter()


def _workdir(request: Request) -> Path:
    return Path(request.app.state.loop.workdir).resolve()


def _safe_path(workdir: Path, relative: str) -> Path:
    """Resolve `relative` under workdir, rejecting escapes."""
    candidate = (workdir / relative).resolve() if relative else workdir
    try:
        candidate.relative_to(workdir)
    except ValueError as e:
        raise HTTPException(403, f"path escapes workdir: {relative}") from e
    return candidate


@router.get("/list")
def list_files(request: Request, path: str = Query("")):
    """List directory contents under workdir."""
    workdir = _workdir(request)
    target = _safe_path(workdir, path)
    if not target.exists():
        raise HTTPException(404, "not found")
    if target.is_file():
        return {"type": "file", "name": target.name}
    entries = []
    for child in sorted(target.iterdir()):
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
            "size": child.stat().st_size if child.is_file() else None,
        })
    return {"type": "dir", "path": path, "entries": entries}


@router.get("/read")
def read_file(request: Request, path: str = Query(...)):
    """Read a text file under workdir."""
    workdir = _workdir(request)
    target = _safe_path(workdir, path)
    if not target.is_file():
        raise HTTPException(404, "file not found")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(500, str(e)) from e
    return {"path": path, "content": text[:200_000], "truncated": len(text) > 200_000}


class WriteBody(BaseModel):
    path: str
    content: str


@router.put("/write")
def write_file(request: Request, body: WriteBody):
    """Write (overwrite) a text file under workdir."""
    workdir = _workdir(request)
    target = _safe_path(workdir, body.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    return {"path": body.path, "bytes": len(body.content)}


class DeleteBody(BaseModel):
    path: str


@router.delete("/delete")
def delete_file(request: Request, body: DeleteBody):
    workdir = _workdir(request)
    target = _safe_path(workdir, body.path)
    if target.exists():
        target.unlink()
    return {"deleted": body.path}
