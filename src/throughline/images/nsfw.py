"""NudeNet as a subprocess (its own venv), and the score Throughline keeps.

nsfw = the highest score among the exposed-explicit labels; every label found
is kept in nsfw_labels; an asset at or above the threshold (tl.image_settings,
NSFW_TAG_AT = 0.6 by default) gets the `nsfw` tag.
"""

import json
import subprocess
from pathlib import Path

EXPLICIT = (
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
)
NSFW_TAG_AT = 0.6
VERSION = 1
WORKER = Path(__file__).with_name("nsfw_worker.py")


def score(detections):
    """(nsfw 0..1, labels {class: max score}) from NudeNet detections."""
    labels = {}
    for d in detections or []:
        name = d.get("class")
        if not name:
            continue
        labels[name] = max(labels.get(name, 0.0), round(float(d.get("score") or 0), 4))
    nsfw = max((labels[name] for name in EXPLICIT if name in labels), default=0.0)
    return round(nsfw, 4), dict(sorted(labels.items()))


class BridgeError(RuntimeError):
    pass


class NudeNetBridge:
    """A long-lived worker process; classify() sends one batch of paths."""

    def __init__(self, python, worker=WORKER, popen=subprocess.Popen, timeout_s=600):
        if not python:
            raise BridgeError("IMAGES_NUDENET_PYTHON is not set or does not exist")
        self.python = python
        self.worker = str(worker)
        self.popen = popen
        self.timeout_s = timeout_s
        self.process = None

    def _start(self):
        self.process = self.popen(
            [self.python, "-I", self.worker],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        ready = self.process.stdout.readline()
        if not ready or not json.loads(ready).get("ready"):
            self.close()
            raise BridgeError("NudeNet worker failed to start")

    def classify(self, paths):
        """{path: {'nsfw', 'labels'} | {'error'}} for each path."""
        if not paths:
            return {}
        if self.process is None or self.process.poll() is not None:
            self._start()
        self.process.stdin.write(json.dumps({"paths": [str(p) for p in paths]}) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            self.close()
            raise BridgeError("NudeNet worker exited")
        answer = json.loads(line)
        if "error" in answer:
            raise BridgeError(answer["error"])
        out = {}
        for item in answer.get("results", []):
            if "error" in item:
                out[item["path"]] = {"error": item["error"]}
            else:
                nsfw, labels = score(item.get("detections"))
                out[item["path"]] = {"nsfw": nsfw, "labels": labels}
        return out

    def close(self):
        if self.process is not None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=10)
            except Exception:
                self.process.kill()
            self.process = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
