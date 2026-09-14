"""Validate image links and prepare per-request image references; no OCR."""

import base64
import re
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit


URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；！？]+", re.IGNORECASE)
IMAGE_PATH = re.compile(r"\.(?:png|jpe?g|gif|webp|bmp|svg|avif|tiff?)$", re.IGNORECASE)
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ImageAccessError(ValueError):
    """An image could not be fetched as a nonempty image response."""


@dataclass(frozen=True)
class ImageReference:
    url: str
    data_url: str = field(repr=False)


def check_image(url, user_agent=None):
    """Download once using the source UA and return a Base64 image data URL."""
    try:
        # GET also handles hosts that reject HEAD. Bound download size and
        # socket waits; never forward API credentials or browser cookies.
        request = urllib.request.Request(
            url, headers={"User-Agent": user_agent or "OCSLLMAnswerer/1.0"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise ImageAccessError("图片请求失败")
            mime = response.headers.get_content_type()
            if not mime.startswith("image/"):
                raise ImageAccessError("链接未返回图片")
            body = response.read(MAX_IMAGE_BYTES + 1)
            if not body or len(body) > MAX_IMAGE_BYTES:
                raise ImageAccessError("图片为空或超过 20 MiB")
            encoded = base64.b64encode(body).decode("ascii")
            return f"data:{mime};base64,{encoded}"
    except Exception as exc:
        raise ImageAccessError("图片无法访问或不是有效图片响应") from exc


def prepare_images(title, options, user_agent=None):
    """Replace image URLs in occurrence order; duplicate links share an ID."""
    images = {}
    references = {}

    def replace(match):
        url = match.group(0).rstrip(".,;!?)）]}")
        suffix = match.group(0)[len(url):]
        if not IMAGE_PATH.search(urlsplit(url).path):
            return match.group(0)
        if url not in images:
            data_url = check_image(url, user_agent=user_agent)
            image_id = f"[Image {len(images) + 1}]"
            images[url] = image_id
            references[image_id] = ImageReference(url, data_url)
        return images[url] + suffix

    title = URL_PATTERN.sub(replace, title)
    options = URL_PATTERN.sub(replace, options)
    return title, options, references


def attach_images(messages, images):
    """Return new messages so vision calls never mutate text-only calls."""
    result = list(messages)
    user = dict(result[-1])
    parts = [{"type": "text", "text": user["content"]}]
    for image_id, reference in images.items():
        parts.extend((
            {"type": "text", "text": image_id},
            {"type": "image_url", "image_url": {"url": reference.data_url}},
        ))
    user["content"] = parts
    result[-1] = user
    return result


def label_image_options(options, images):
    """Label options only when every row contains known image IDs and no text."""
    rows = [row.strip() for row in options.splitlines() if row.strip()]
    if not rows or not images or len(rows) > 26:
        return None
    contents = []
    for row in rows:
        content = re.sub(r"^(?:[A-Z]|\d{1,2})[.．、:：)）]\s*", "", row)
        ids = re.findall(r"\[Image \d+\]", content)
        if (not ids or any(image_id not in images for image_id in ids)
                or re.sub(r"\[Image \d+\]", "", content).strip()):
            return None
        contents.append(content)
    return "\n".join(f"{chr(65 + index)}. {content}"
                     for index, content in enumerate(contents))
