from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest, urlopen
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# Smart-Movies API
# Carpeta esperada: api.py y media/{series,peliculas,banners}/
BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = (BASE_DIR / "media").resolve()
SERIES_DIR = MEDIA_DIR / "series"
BANNERS_DIR = MEDIA_DIR / "banners"
VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogg", ".mov", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
TICKET_TTL_SECONDS = 6 * 60 * 60
TICKET_SECRET = os.getenv("SMART_MOVIES_MEDIA_SECRET") or secrets.token_urlsafe(48)
SMART_ACCOUNT_API_URL = os.getenv(
    "SMART_ACCOUNT_API_URL",
    "https://titans-latter-opinions-invitations.trycloudflare.com",
).rstrip("/")

cors_origins = [
    x.strip() for x in os.getenv(
        "SMART_MOVIES_ALLOWED_ORIGINS",
        "https://smartium25.github.io,http://localhost:5500,http://127.0.0.1:5500",
    ).split(",") if x.strip()
]

app = FastAPI(title="Smart-Movies API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["Authorization", "Range", "Accept", "Origin", "Content-Type"],
    expose_headers=["Accept-Ranges", "Content-Length", "Content-Range", "Content-Type"],
)


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value).strip().casefold()


def _natural_key(value: str):
    return [(0, int(part)) if part.isdigit() else (1, part.casefold()) for part in re.split(r"(\d+)", value)]


def _safe_media_path(relative_path: str) -> Path:
    # Evita traversal fuera de media/.
    candidate = (MEDIA_DIR / relative_path).resolve()
    try:
        candidate.relative_to(MEDIA_DIR)
    except ValueError:
        raise HTTPException(status_code=403, detail="Ruta multimedia no permitida.")
    if candidate.suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Formato multimedia no permitido.")
    return candidate


def _make_ticket(relative_path: str) -> str:
    expires = int(time.time()) + TICKET_TTL_SECONDS
    payload = f"{expires}\n{relative_path}".encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(TICKET_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def _ticket_is_valid(ticket: str | None, relative_path: str) -> bool:
    if not ticket or "." not in ticket:
        return False
    payload_b64, signature = ticket.rsplit(".", 1)
    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        expires_text, ticket_path = payload.decode("utf-8").split("\n", 1)
        expires = int(expires_text)
    except (ValueError, UnicodeDecodeError, TypeError):
        return False
    expected = hmac.new(TICKET_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return (
        hmac.compare_digest(signature, expected)
        and expires >= int(time.time())
        and hmac.compare_digest(ticket_path.encode("utf-8"), relative_path.encode("utf-8"))
    )


def _media_url(relative_path: str) -> str:
    encoded = quote(relative_path.replace("\\", "/"), safe="/")
    return f"/media/{encoded}?ticket={_make_ticket(relative_path)}"


def _display_movie_title(stem: str) -> str:
    key = _norm(stem)
    friendly = {
        "pepito the movie remake": "Pepito the Movie Remake",
        "pepito y french toast the movie": "Pepito y French Toast the Movie",
        "momentos inovlidables 5 grado": "Momentos Inolvidables — 5.º grado",
        "momentos inolvidables 5 grado": "Momentos Inolvidables — 5.º grado",
        "momentos inolvidables 6 grado": "Momentos Inolvidables — 6.º grado",
        "momentos inovlidables 6 grado": "Momentos Inolvidables — 6.º grado",
    }
    return friendly.get(key, stem.strip())


def _episode_title(file_path: Path, series_name: str, relative_to_series: Path) -> str:
    stem = file_path.stem.strip()
    key = _norm(series_name)
    if key == "momentos inolvidables":
        video_key = _norm(stem)
        if "5 grado" in video_key:
            return "5.º grado"
        if "6 grado" in video_key:
            return "6.º grado"
    normalized_stem = _norm(stem)
    match = re.match(r"^dia\s*(\d+)\s*,?\s*videos sentimentales.*$", normalized_stem)
    if match and "videos sentimentales" in key:
        return f"Día {match.group(1)}"
    # Si está bajo una subcarpeta (por ejemplo Temporada 1), añadirla al título.
    if relative_to_series.parent != Path("."):
        return f"{relative_to_series.parent.as_posix().replace('/', ' · ')} — {stem}"
    return stem


def _iter_video_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        (p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda p: _natural_key(p.relative_to(folder).as_posix()),
    )


def _banner_payload(filename: str, title: str, description: str, label: str) -> dict | None:
    if not BANNERS_DIR.is_dir():
        return None
    wanted = _norm(filename)
    file = next((p for p in BANNERS_DIR.iterdir() if p.is_file() and _norm(p.name) == wanted), None)
    if not file:
        # Busca por nombre sin depender de mayúsculas/minúsculas de JPG/JPG.
        stem = _norm(Path(filename).stem)
        file = next((p for p in BANNERS_DIR.iterdir() if p.is_file() and _norm(p.stem) == stem), None)
    if not file or file.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    rel = file.relative_to(MEDIA_DIR).as_posix()
    return {
        "file": file.name,
        "title": title,
        "description": description,
        "label": label,
        "url": _media_url(rel),
    }


def _build_catalog() -> dict:
    series = []
    if SERIES_DIR.is_dir():
        series_folders = sorted(
            (p for p in SERIES_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: _natural_key(p.name),
        )
        for folder in series_folders:
            videos = _iter_video_files(folder)
            if not videos:
                continue
            episodes = []
            for video in videos:
                rel_to_media = video.relative_to(MEDIA_DIR).as_posix()
                rel_to_series = video.relative_to(folder)
                episodes.append({
                    "id": rel_to_media,
                    "title": _episode_title(video, folder.name, rel_to_series),
                    "file_name": video.name,
                    "url": _media_url(rel_to_media),
                    "description": "",
                })
            series.append({
                "id": folder.relative_to(MEDIA_DIR).as_posix(),
                "title": folder.name,
                "thumbnail_url": episodes[0]["url"],
                "episode_count": len(episodes),
                "episodes": episodes,
                "type": "series",
            })

    # Todos los vídeos que no pertenezcan a media/series o media/banners se tratan como películas.
    movies = []
    if MEDIA_DIR.is_dir():
        all_files = sorted(
            (p for p in MEDIA_DIR.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
            key=lambda p: _natural_key(p.relative_to(MEDIA_DIR).as_posix()),
        )
        for video in all_files:
            rel = video.relative_to(MEDIA_DIR)
            if rel.parts and rel.parts[0].casefold() in {"series", "banners"}:
                continue
            rel_path = rel.as_posix()
            stem = video.stem.strip()
            title = _display_movie_title(stem)
            description = ""
            if _norm(stem) in {"pepito y french toast the movie", "french toast y pepito the movie"}:
                description = "Pepito y French Toast se unen para una nueva aventura."
            movies.append({
                "id": rel_path,
                "title": title,
                "file_name": video.name,
                "url": _media_url(rel_path),
                "thumbnail_url": _media_url(rel_path),
                "description": description,
                "type": "movie",
            })

    banners = [
        _banner_payload(
            "banner 1.JPG", "Videos Sentimentales",
            "Sigue la historia a lo largo de Videos Sentimentales.", "Serie",
        ),
        _banner_payload(
            "banner 3.JPG", "Pepito y French Toast the Movie",
            "Pepito y French Toast se unen para una nueva aventura.", "Película",
        ),
    ]
    banners = [b for b in banners if b is not None]
    return {"series": series, "movies": movies, "banners": banners, "generated_at": int(time.time())}


def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.casefold() not in {"bearer", "smart-token"} or not token:
        return None
    return token


def _verify_token_sync(token: str) -> bool:
    # Intenta ambos esquemas para mantener compatibilidad con implementaciones de Smart-Account.
    for scheme in ("Smart-Token", "Bearer"):
        req = URLRequest(
            f"{SMART_ACCOUNT_API_URL}/api/me",
            headers={"Authorization": f"{scheme} {token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(req, timeout=7) as response:
                return 200 <= response.status < 300
        except HTTPError as exc:
            if exc.code in (401, 403):
                continue
            return False
        except (URLError, TimeoutError, OSError):
            return False
    return False


async def _require_account(authorization: str | None) -> str:
    token = _extract_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Se necesita una sesión de Smart-Account.")
    valid = await asyncio.to_thread(_verify_token_sync, token)
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Sesión inválida o API de Smart-Account inaccesible. Comprueba SMART_ACCOUNT_API_URL.",
        )
    return token


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Smart-Movies API", "version": app.version}


@app.get("/api/catalog")
async def catalog(authorization: str | None = Header(default=None)):
    await _require_account(authorization)
    return _build_catalog()


@app.get("/media/{relative_path:path}")
async def serve_media(relative_path: str, request: Request):
    if not _ticket_is_valid(request.query_params.get("ticket"), relative_path):
        raise HTTPException(status_code=403, detail="Enlace multimedia caducado o no válido. Recarga Smart-Movies.")
    path = _safe_media_path(relative_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Archivo multimedia no encontrado.")

    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": media_type,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name)}",
    }
    range_header = request.headers.get("range")
    start, end = 0, max(size - 1, 0)
    status_code = 200
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match or size == 0:
            return JSONResponse(
                {"detail": "Rango de bytes no válido."},
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        left, right = match.groups()
        if left:
            start = int(left)
            end = int(right) if right else size - 1
        else:
            suffix_len = int(right or "0")
            if suffix_len <= 0:
                return JSONResponse(
                    {"detail": "Rango de bytes no válido."},
                    status_code=416,
                    headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
                )
            start = max(size - suffix_len, 0)
            end = size - 1
        if start >= size or start > end:
            return JSONResponse(
                {"detail": "Rango fuera del archivo."},
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        end = min(end, size - 1)
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)

    def file_chunks() -> Iterator[bytes]:
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining > 0:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(file_chunks(), status_code=status_code, headers=headers, media_type=media_type)


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("SMART_MOVIES_HOST", "0.0.0.0")
    port = int(os.getenv("SMART_MOVIES_PORT", "8007"))
    uvicorn.run(app, host=host, port=port, access_log=False)
