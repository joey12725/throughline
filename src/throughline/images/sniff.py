"""Image types, header sniffing and data-URI decoding, standard library only.

Throughline's own environment has no imaging library, and the pipeline needs
only a few facts before an upload: is this an image, what type, how large,
is the file complete, and (for JPEG) when was it taken.
"""

import base64
import binascii
import re
import struct
from datetime import datetime

IMAGE_EXTENSIONS = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
    "avif": "image/avif",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}
IMAGE_MIMES = set(IMAGE_EXTENSIONS.values()) | {"image/jpg", "image/pjpeg", "image/x-ms-bmp"}
EXTENSION_OF = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/pjpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/x-ms-bmp": "bmp",
    "image/tiff": "tiff",
}


def extension(name):
    if not name or "." not in str(name):
        return None
    return str(name).rsplit(".", 1)[1].split("?", 1)[0].lower() or None


def is_image(name=None, mime=None):
    """An image by its declared mime type, else by its file name extension."""
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if mime:
        return mime in IMAGE_MIMES
    return extension(name) in IMAGE_EXTENSIONS


def mime_of(name=None, mime=None):
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if mime in IMAGE_MIMES:
        return {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}.get(mime, mime)
    return IMAGE_EXTENSIONS.get(extension(name) or "")


def _jpeg_size(data):
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xFF:
            i += 1
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        i += 2 + length
    return None, None


def sniff(data):
    """(mime, width, height) from the first bytes; (None, None, None) if unknown."""
    head = bytes(data[:64])
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "image/png", w, h
    if head.startswith(b"\xff\xd8"):
        w, h = _jpeg_size(data)
        return "image/jpeg", w, h
    if head[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return "image/gif", w, h
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        chunk = head[12:16]
        try:
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return "image/webp", w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return "image/webp", w, h
            if chunk == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return "image/webp", w, h
        except (struct.error, IndexError):
            pass
        return "image/webp", None, None
    if head.startswith(b"BM") and len(data) >= 26:
        w, h = struct.unpack("<ii", data[18:26])
        return "image/bmp", w, abs(h)
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff", None, None
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif", None, None
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic", None, None
    return None, None, None


def complete(data, mime=None):
    """False when the bytes are visibly cut short (a truncated export or download)."""
    mime = mime or sniff(data)[0]
    if mime is None:
        return False
    if mime == "image/png":
        return data.rstrip(b"\x00")[-12:].startswith(b"\x00\x00\x00\x00IEND") or data[-8:-4] == b"IEND"
    if mime == "image/jpeg":
        return data.rstrip(b"\x00")[-2:] == b"\xff\xd9"
    if mime == "image/gif":
        return data.rstrip(b"\x00")[-1:] == b"\x3b"
    if mime == "image/webp":
        return len(data) >= 8 and struct.unpack("<I", data[4:8])[0] + 8 <= len(data)
    return True


DATA_URI = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?P<params>(;[^,;]+)*?);base64,(?P<data>.*)$", re.S)


class DataUriError(ValueError):
    """reason: 'not_data_uri', 'not_base64', 'not_image', 'truncated'."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def decode_data_uri(uri, *, cut=False):
    """(bytes, mime) of a base64 image data URI, or DataUriError.

    `cut` says the exporter reported the string as cut; the bytes are then
    truncated however well they happen to decode.
    """
    match = DATA_URI.match(uri or "")
    if not match:
        raise DataUriError("not_data_uri")
    if cut:
        raise DataUriError("truncated")
    payload = re.sub(r"\s+", "", match.group("data"))
    if len(payload) % 4:
        raise DataUriError("truncated")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise DataUriError("not_base64") from None
    mime, _, _ = sniff(data)
    if mime is None:
        raise DataUriError("not_image")
    if not complete(data, mime):
        raise DataUriError("truncated")
    return data, mime


def _exif_ifd(tiff, offset, little):
    fmt = "<" if little else ">"
    try:
        count = struct.unpack(fmt + "H", tiff[offset : offset + 2])[0]
    except struct.error:
        return {}
    entries = {}
    for k in range(count):
        base = offset + 2 + 12 * k
        entry = tiff[base : base + 12]
        if len(entry) < 12:
            break
        tag, typ, n = struct.unpack(fmt + "HHI", entry[:8])
        value = entry[8:12]
        if typ == 2:  # ASCII
            if n <= 4:
                raw = value[:n]
            else:
                ptr = struct.unpack(fmt + "I", value)[0]
                raw = tiff[ptr : ptr + n]
            entries[tag] = raw.rstrip(b"\x00").decode("ascii", "replace")
        elif typ == 4:
            entries[tag] = struct.unpack(fmt + "I", value)[0]
    return entries


def exif_facts(data):
    """{'taken': datetime|None, 'make': str|None, 'model': str|None} from JPEG EXIF."""
    facts = {"taken": None, "make": None, "model": None}
    if not data.startswith(b"\xff\xd8"):
        return facts
    i = 2
    while i + 4 < min(len(data), 1 << 20):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker == 0xE1 and data[i + 4 : i + 10] == b"Exif\x00\x00":
            tiff = data[i + 10 : i + 2 + length]
            little = tiff[:2] == b"II"
            fmt = "<" if little else ">"
            try:
                first = struct.unpack(fmt + "I", tiff[4:8])[0]
            except struct.error:
                return facts
            ifd0 = _exif_ifd(tiff, first, little)
            facts["make"] = ifd0.get(0x010F) or None
            facts["model"] = ifd0.get(0x0110) or None
            sub = _exif_ifd(tiff, ifd0[0x8769], little) if 0x8769 in ifd0 else {}
            stamp = sub.get(0x9003) or sub.get(0x9004) or ifd0.get(0x0132)
            if stamp:
                try:
                    facts["taken"] = datetime.strptime(stamp.strip(), "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    pass
            return facts
        if marker == 0xDA:
            break
        i += 2 + length
    return facts
