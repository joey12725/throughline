"""NudeNet worker: run with the python of a venv that has nudenet (and,
optionally, pillow + pillow-heif). It never imports Throughline.

Protocol: one JSON object per line on stdin, {"paths": [...]}; one JSON line
per request on stdout, {"results": [{"path", "detections": [{"class", "score",
"box"}]} | {"path", "error"}]}. stdout carries nothing else; logs go to stderr.
"""

import json
import os
import sys
import tempfile


def _readable(path):
    """A path NudeNet's OpenCV reader can open: HEIC/AVIF/WebP-animated go through Pillow."""
    lower = path.lower()
    if not lower.endswith((".heic", ".heif", ".avif", ".gif", ".tif", ".tiff", ".bmp")):
        return path, None
    try:
        from PIL import Image

        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        with Image.open(path) as image:
            image.seek(0)
            handle, temp = tempfile.mkstemp(suffix=".jpg")
            os.close(handle)
            image.convert("RGB").save(temp, "JPEG", quality=92)
            return temp, temp
    except Exception:
        return path, None


def main():
    stdout = sys.stdout
    sys.stdout = sys.stderr  # Model loading chatter must not reach the protocol.
    from nudenet import NudeDetector

    detector = NudeDetector()
    stdout.write(json.dumps({"ready": True}) + "\n")
    stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"error": "invalid request"}) + "\n")
            stdout.flush()
            continue
        results = []
        for path in request.get("paths") or []:
            readable, temp = _readable(path)
            try:
                found = detector.detect(readable) or []
                results.append(
                    {
                        "path": path,
                        "detections": [
                            {
                                "class": d.get("class"),
                                "score": round(float(d.get("score") or 0), 4),
                                "box": [int(v) for v in d.get("box") or []],
                            }
                            for d in found
                        ],
                    }
                )
            except Exception as error:  # One bad file costs only itself.
                results.append({"path": path, "error": f"{type(error).__name__}: {error}"[:200]})
            finally:
                if temp:
                    try:
                        os.unlink(temp)
                    except OSError:
                        pass
        stdout.write(json.dumps({"results": results}) + "\n")
        stdout.flush()


if __name__ == "__main__":
    main()
