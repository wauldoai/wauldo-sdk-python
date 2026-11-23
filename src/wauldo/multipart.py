"""Request bodies of the upload endpoints, sized as the server counts them:
``multipart/form-data`` for files, JSON for text (stdlib only)."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Tuple

from .exceptions import ValidationError

#: Largest upload the server reads (10 MB). The limit counts the whole
#: request (the file, its name and the form fields), so the file itself must
#: be a few hundred bytes smaller: a larger file is refused before it is
#: read, a larger request before it is sent.
MAX_UPLOAD_FILE_SIZE = 10 * 1024 * 1024


def read_upload_file(file_path: str) -> bytes:
    """Read a file to upload; :class:`ValidationError` when it is missing,
    empty, or larger than :data:`MAX_UPLOAD_FILE_SIZE`."""
    if not os.path.isfile(file_path):
        raise ValidationError(f"File not found: {file_path}", field="file_path")
    size = os.path.getsize(file_path)
    if size == 0:
        raise ValidationError("The file is empty", field="file_path")
    if size > MAX_UPLOAD_FILE_SIZE:
        raise ValidationError(
            f"File exceeds the 10 MB limit ({size} > {MAX_UPLOAD_FILE_SIZE} bytes)",
            field="file_path",
        )
    with open(file_path, "rb") as f:
        return f.read()


def encode_upload_file(file_path: str, fields: Dict[str, str]) -> Tuple[bytes, str]:
    """The body and ``Content-Type`` of the upload of ``file_path`` with
    ``fields``; :class:`ValidationError` when the file is refused by
    :func:`read_upload_file`, or when the encoded request is larger than
    :data:`MAX_UPLOAD_FILE_SIZE` (the server would refuse it unread)."""
    data = read_upload_file(file_path)
    body, content_type = encode_multipart(os.path.basename(file_path), data, fields)
    if len(body) > MAX_UPLOAD_FILE_SIZE:
        raise ValidationError(
            f"Upload exceeds the 10 MB limit ({len(body)} > {MAX_UPLOAD_FILE_SIZE} bytes, "
            "file name and form fields included)",
            field="file_path",
        )
    return body, content_type


def encode_text_upload(body: Dict[str, Any]) -> bytes:
    """The JSON body of a text upload, exactly as it is sent;
    :class:`ValidationError` when it is larger than
    :data:`MAX_UPLOAD_FILE_SIZE`.

    The server's limit counts the bytes of the request, not the characters
    of the text. The body is UTF-8 JSON, as the TypeScript and Rust SDKs
    send it (2 to 4 bytes for a non-ASCII character; escaping them would
    take 6 to 12 and refuse texts the other SDKs accept), so a text of a
    few million characters can still be over the limit while its length
    looks well under it."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_UPLOAD_FILE_SIZE:
        raise ValidationError(
            f"Content exceeds the 10 MB limit once encoded ({len(data)} > "
            f"{MAX_UPLOAD_FILE_SIZE} bytes of UTF-8 JSON; non-ASCII characters take 2 to 4 bytes)",
            field="content",
        )
    return data


def encode_multipart(filename: str, data: bytes, fields: Dict[str, str]) -> Tuple[bytes, str]:
    """The body and the ``Content-Type`` header of a form holding the
    ``file`` part, then each text field. The boundary never occurs in the
    file, and quotes or line breaks in the file name are replaced so the
    name cannot end its header early."""
    boundary = f"----WauldoBoundary{uuid.uuid4().hex}"
    while boundary.encode() in data:
        boundary = f"----WauldoBoundary{uuid.uuid4().hex}"
    safe_name = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    parts = [
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{safe_name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        ).encode(),
        data,
        b"\r\n",
    ]
    for name, value in fields.items():
        header = f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
        parts.append(f"{header}{value}\r\n".encode())
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
