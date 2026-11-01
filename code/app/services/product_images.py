"""Single-image validation, controlled storage, and rollback cleanup."""

from io import BytesIO
from pathlib import Path
import re
from uuid import uuid4
import warnings

from flask import current_app, send_file
from werkzeug.exceptions import NotFound
from PIL import Image, UnidentifiedImageError

from app.services.product_input import ProductError

_FORMAT_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


def image_directory() -> Path:
    """Keep uploads outside static files so draft images have the same access checks."""
    return Path(current_app.config["UPLOAD_FOLDER"]).resolve()


def image_file(filename: str) -> Path:
    """Reject arbitrary paths even if a stored value has been corrupted."""
    if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|png|webp)", filename):
        raise ProductError("图片不可用。", 404)
    path = (image_directory() / filename).resolve()
    if path.parent != image_directory():
        raise ProductError("图片不可用。", 404)
    return path


def store_image(upload) -> str | None:
    """Verify bytes and image dimensions before writing one random-named file."""
    if upload is None or not upload.filename:
        return None
    limit = current_app.config["MAX_IMAGE_BYTES"]
    content = upload.stream.read(limit + 1)
    if len(content) > limit:
        raise ProductError("图片不能超过 3 MB。", field="image")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content), formats=list(_FORMAT_EXTENSIONS)) as picture:
                if picture.width * picture.height > 16000000:
                    raise ValueError("Image size or frame count exceeds the allowed limit")
                extension = _FORMAT_EXTENSIONS[picture.format]
                picture.verify()
            with Image.open(BytesIO(content), formats=list(_FORMAT_EXTENSIONS)) as picture:
                if getattr(picture, "n_frames", 1) != 1:
                    raise ValueError("Animated images are not allowed")
                picture.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ProductError("请上传有效的单张 JPG、PNG 或 WebP 图片（最多 1600 万像素）。",
                           field="image") from error
    directory = image_directory()
    filename = f"{uuid4().hex}.{extension}"
    path = image_file(filename)
    created = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as target:
            created = True
            target.write(content)
    except OSError as error:
        if created:
            discard_image(filename)
        raise ProductError("图片保存失败，请稍后重试。", 503, "image") from error
    return filename


def discard_image(filename: str | None) -> None:
    """Remove a new failed upload or an old file only after a replacement commits."""
    if filename is not None:
        try:
            image_file(filename).unlink(missing_ok=True)
        except OSError:
            current_app.logger.warning("Could not clean up an unreferenced product image")


def image_response(filename: str | None):
    """Serve an already-authorized product image with a verified MIME type and no caching."""
    if not filename:
        raise NotFound()
    path = image_file(filename)
    if not path.is_file():
        raise NotFound()
    mime = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[path.suffix]
    response = send_file(path, mimetype=mime, conditional=True, max_age=0)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
