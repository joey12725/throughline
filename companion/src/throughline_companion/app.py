"""The running companion: runner, bridge, ActivityWatch and the dashboard."""

import json
import logging
import subprocess
import threading

from . import __version__, activitywatch, autostart, catalog, paths
from .bridge import Bridge, decode_pairing
from .config import Config
from .providers import Providers
from .runner import Runner, self_command

log = logging.getLogger("throughline_companion")


class Companion:
    def __init__(self, config=None, *, server=False, isolate=None, gui=True):
        self.config = config or Config()
        # server: the Docker runner (no capture, no ActivityWatch, isolated
        # job environments). gui: a tray/dashboard user is expected; without
        # it every desktop feature still runs (an SSH-installed service).
        self.server = server
        self.headless = server or not gui
        # Server mode isolates each job's environment; a desktop passes its
        # session on (HOME, DBUS, DISPLAY, APPDATA) so connectors can read it.
        self.runner = Runner(self.config, isolate=server if isolate is None else isolate)
        self.providers = Providers(self.config, runner=self.runner, status=self.status_summary)
        self.bridge = Bridge(self.config, self.providers, plugins=self.plugin_states)
        self.activitywatch = activitywatch.Supervisor(self.config)
        from .capture.engine import CaptureEngine

        self.capture = CaptureEngine(self.config)
        self.capture_job = dict(state="idle", detail=None)
        self.lock = threading.RLock()
        self.manifests = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        self.runner.start()
        if not self.server or self.config.server.get("device_token"):
            self.bridge.start()
        if not self.server:
            self.activitywatch.start()
            self.capture.start()

    def stop(self):
        self.capture.stop()
        self.runner.stop()
        self.bridge.stop()
        self.activitywatch.stop()

    # -- state -------------------------------------------------------------

    def plugin_states(self):
        return [
            dict(
                name=job["name"],
                state=job["state"],
                last_run_at=job["last_run_at"],
                last_error=job["last_error"],
            )
            for job in self.runner.snapshot()
            if job["kind"] == "plugin"
        ]

    def status_summary(self):
        return dict(
            version=__version__,
            platform=paths.platform(),
            plugins=self.plugin_states(),
            activitywatch={
                k: v
                for k, v in self.activitywatch.snapshot().items()
                if k in ("mode", "reachable", "running")
            },
        )

    def state(self):
        server = self.config.server
        jobs = {job["name"]: job for job in self.runner.snapshot()}
        entries = []
        for entry in catalog.CATALOG:
            item = catalog.describe(entry, self.config)
            item["job"] = jobs.get(entry["name"])
            entries.append(item)
        extra = [
            dict(name=name, title=name, summary="Configured by hand", job=jobs.get(name),
                 enabled=bool(cfg.get("enabled")), has_token=bool(cfg.get("token")),
                 supported=True, installed=True, detected=[], required=[],
                 interval_s=cfg.get("interval_s"), env=cfg.get("env") or {})
            for name, cfg in sorted(self.config.data["plugins"].items())
            if name not in catalog.BY_NAME
        ]
        return dict(
            version=__version__,
            platform=paths.platform(),
            headless=self.headless,
            server_mode=self.server,
            paired=self.config.paired,
            paused=self.runner.paused,
            server=dict(url=server.get("url"), device=server.get("device")),
            bridge=dict(self.bridge.snapshot(), enabled=self.config.data["bridge"].get("enabled", True),
                        settings=self.config.data["bridge"]["capabilities"]),
            activitywatch=self.activitywatch.snapshot(),
            infisical=self.infisical_state(),
            plugins=entries + extra,
            jobs=[j for j in jobs.values() if j["kind"] == "command"],
            capture=dict(self.capture.snapshot(), job=self.capture_job),
            autostart=_safe(autostart.enabled, False),
            logs=str(paths.logs_dir()),
        )

    # -- actions -----------------------------------------------------------

    def pair(self, code):
        bundle = decode_pairing(code)
        self.config.apply_pairing(bundle)
        self.runner.reload()
        return dict(device=bundle["device"], url=bundle["url"], plugins=sorted(bundle.get("plugin_tokens") or {}))

    def unpair(self):
        with self.config.lock:
            self.config.data["server"] = {"url": "", "device": "", "device_token": ""}
            for entry in self.config.data["plugins"].values():
                entry["token"] = ""
                entry["enabled"] = False
            self.config.save()
        self.runner.reload()

    def update_plugin(self, name, data):
        allowed = {"enabled", "interval_s", "env", "token", "command"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError("unknown settings: " + ", ".join(sorted(unknown)))
        with self.config.lock:
            entry = self.config.plugin(name)
            if "enabled" in data:
                entry["enabled"] = bool(data["enabled"])
                if entry["enabled"] and not entry.get("env"):
                    spec = catalog.BY_NAME.get(name)
                    entry["env"] = catalog.defaults(spec) if spec else {}
            if "interval_s" in data:
                value = data["interval_s"]
                entry["interval_s"] = max(60, int(value)) if value else None
            if "env" in data:
                if not isinstance(data["env"], dict):
                    raise ValueError("env must be an object")
                entry["env"] = {
                    str(k): v for k, v in data["env"].items() if v not in (None, "")
                }
            if data.get("token"):
                entry["token"] = str(data["token"]).strip()
            if "command" in data:
                entry["command"] = data["command"] or None
            self.config.save()
        self.runner.reload()
        return catalog.describe(catalog.BY_NAME[name], self.config) if name in catalog.BY_NAME else entry

    def update_bridge(self, capability, data):
        with self.config.lock:
            if capability is None:
                self.config.data["bridge"]["enabled"] = bool(data.get("enabled", True))
            else:
                options = self.config.capability(capability)
                if options is None:
                    raise ValueError(f"unknown capability {capability}")
                for key, value in data.items():
                    if key not in options:
                        raise ValueError(f"{capability} has no setting {key}")
                    if isinstance(options[key], list) and not isinstance(value, list):
                        raise ValueError(f"{key} must be a list")
                    if isinstance(options[key], dict) and not isinstance(value, dict):
                        raise ValueError(f"{key} must be an object")
                    options[key] = value
            self.config.save()
        return self.config.data["bridge"]

    def update_activitywatch(self, data):
        with self.config.lock:
            options = self.config.data["activitywatch"]
            if "mode" in data:
                if data["mode"] not in ("off", "external", "managed"):
                    raise ValueError("mode is off, external or managed")
                options["mode"] = data["mode"]
            if "watchers" in data:
                options["watchers"] = list(data["watchers"] or [])
            if "url" in data:
                options["url"] = str(data["url"])
            self.config.save()
        if options["mode"] in ("external", "managed"):
            # The ActivityWatch connector reads the same server.
            entry = self.config.plugin("activitywatch")
            entry.setdefault("env", {})["ACTIVITYWATCH_URL"] = options["url"].rstrip("/") + "/api/0"
            self.config.save()
        return self.activitywatch.snapshot()

    def install_activitywatch(self):
        threading.Thread(target=self._install_aw, daemon=True).start()
        return dict(started=True)

    def _install_aw(self):
        try:
            activitywatch.install()
            self.activitywatch.last_error = None
        except Exception as error:  # noqa: BLE001
            self.activitywatch.last_error = str(error)[:300]

    def update_capture(self, data):
        """Merge capture settings (one level deep per section). Switching
        capture on also switches on the capture connector that uploads it."""
        sections = ("screens", "audio", "transcription")
        with self.config.lock:
            options = self.config.data["capture"]
            for key, value in data.items():
                if key in sections:
                    if not isinstance(value, dict):
                        raise ValueError(f"{key} must be an object")
                    unknown = set(value) - set(options[key])
                    if unknown:
                        raise ValueError(f"{key} has no setting " + ", ".join(sorted(unknown)))
                    if key == "transcription" and value.get("api_key") == "set":
                        value = {k: v for k, v in value.items() if k != "api_key"}
                    options[key].update(value)
                elif key in ("enabled", "device", "spool_max_mb"):
                    options[key] = value
                else:
                    raise ValueError(f"capture has no setting {key}")
            if options.get("enabled"):
                entry = self.config.plugin("capture")
                entry["enabled"] = True
                entry.setdefault("env", {})
            self.config.save()
        self.runner.reload()
        return self.capture.snapshot()

    def install_local_model(self, model):
        from .capture import local_whisper

        if model not in local_whisper.MODELS:
            raise ValueError("unknown model")

        def work():
            self.capture_job.update(state="running", detail="starting")
            try:
                local_whisper.install(model, progress=lambda text: self.capture_job.update(detail=text))
                self.capture_job.update(state="done", detail=f"{model} ready")
            except Exception as error:  # noqa: BLE001
                self.capture_job.update(state="failed", detail=str(error)[:300])

        threading.Thread(target=work, daemon=True).start()
        return dict(started=True)

    def remove_local_model(self, model=None):
        from .capture import local_whisper

        if self.capture.local:
            self.capture.local.close()
            self.capture.local = None
        return local_whisper.remove(model)

    def manifest(self, name):
        """Settings and secrets a catalog plugin declares (cached)."""
        if name in self.manifests:
            return self.manifests[name]
        spec = catalog.BY_NAME.get(name)
        if not spec or not catalog.installed(spec):
            raise ValueError(f"{name} is not installed in this companion")
        result = subprocess.run(
            self_command() + ["exec", spec["entry"], "manifest"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise ValueError(f"{name} manifest failed: {result.stderr.strip()[-300:]}")
        data = json.loads(result.stdout)
        manifest = dict(
            name=data["name"],
            version=data.get("version"),
            channels=[c["name"] for c in data.get("channels") or []],
            settings=data.get("settings") or {},
            secrets=data.get("secrets") or {},
            default_interval_s=data.get("default_interval_s"),
        )
        self.manifests[name] = manifest
        return manifest

    def log_tail(self, name, lines=200):
        path = paths.logs_dir() / f"{name}.log"
        if "/" in name or "\\" in name or not path.exists():
            return ""
        with open(path, encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])

    def infisical_state(self):
        """Non-sensitive summary of the Infisical connection (config + env fallback)."""
        from throughline_sdk import infisical

        stored = dict(self.config.data.get("infisical") or {})
        effective = infisical.InfisicalConfig.from_settings(stored)
        stored_has_auth = bool(
            (stored.get("client_id") and stored.get("client_secret")) or stored.get("token")
        )
        source = "config" if stored_has_auth else ("env" if effective.configured else "none")
        raw_secret = stored.get("client_secret") or ""
        raw_token = stored.get("token") or ""
        return dict(
            configured=effective.configured,
            auth_mode=effective.auth_mode,
            source=source,
            site_url=effective.site_url,
            project_id=effective.project_id,
            project_slug=effective.project_slug,
            environment=effective.environment,
            secret_path=effective.secret_path,
            client_id=effective.client_id,
            has_client_secret=bool(effective.client_secret),
            client_secret_ref=raw_secret if isinstance(raw_secret, str) and raw_secret.startswith("env:") else "",
            has_token=bool(effective.token),
            token_ref=raw_token if isinstance(raw_token, str) and raw_token.startswith("env:") else "",
        )

    def update_infisical(self, data):
        from throughline_sdk import infisical

        allowed = {
            "site_url",
            "client_id",
            "client_secret",
            "token",
            "project_id",
            "project_slug",
            "environment",
            "secret_path",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ValueError("unknown infisical settings: " + ", ".join(sorted(unknown)))
        with self.config.lock:
            options = self.config.data.setdefault("infisical", {})
            for key, value in data.items():
                text = "" if value is None else str(value).strip()
                if key in ("client_secret", "token") and text == "set":
                    continue
                if key == "site_url" and text:
                    try:
                        text = infisical.normalize_site_url(text)
                    except infisical.InfisicalError as error:
                        raise ValueError(str(error)) from None
                elif key == "secret_path":
                    text = infisical.normalize_secret_path(text, default="/") or "/"
                options[key] = text
            self.config.save()
        return self.infisical_state()

    def test_infisical(self):
        from throughline_sdk import infisical

        cfg = infisical.InfisicalConfig.from_settings(self.config.data.get("infisical"))
        try:
            return infisical.InfisicalClient(cfg).test()
        except infisical.InfisicalError as error:
            raise ValueError(str(error)) from None

    def list_infisical_secrets(self, path=None, environment=None, recursive=False):
        from throughline_sdk import infisical

        cfg = infisical.InfisicalConfig.from_settings(self.config.data.get("infisical"))
        try:
            return infisical.InfisicalClient(cfg).list_secret_names(
                secret_path=path or None,
                environment=environment or None,
                recursive=bool(recursive),
            )
        except infisical.InfisicalError as error:
            raise ValueError(str(error)) from None

    def set_autostart(self, on):
        return autostart.enable() if on else autostart.disable()


def _safe(function, default):
    try:
        return function()
    except Exception:  # noqa: BLE001
        return default
