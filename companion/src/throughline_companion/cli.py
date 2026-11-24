"""throughline-companion: the desktop app and its command line.

    throughline-companion run              tray icon + dashboard + connectors + bridge
    throughline-companion run --headless   no tray, no browser (a service, an SSH install)
    throughline-companion run --server     server connectors in Docker
    throughline-companion setup ...        unattended: pair, enable, capture, service
    throughline-companion service install  run in the background (systemd, launchd, schtasks)
    throughline-companion deploy user@host install on another computer over SSH
    throughline-companion open             open the running companion's dashboard
    throughline-companion pair CODE        store a pairing code from the server
    throughline-companion status [--json]
    throughline-companion detect           which desktop connectors have data here
    throughline-companion plugin enable|disable|run|set NAME [KEY=VALUE ...]
    throughline-companion activitywatch status|install|mode MODE
    throughline-companion autostart on|off
"""

import argparse
import json
import logging
import signal
import sys
import threading
import webbrowser

from . import __version__, activitywatch, autostart, catalog, paths
from .config import Config


def _config(args):
    """The settings file from --config. When that file does not exist (a
    Docker bind mount of a missing file shows up as a directory) and
    THROUGHLINE_COMPANION_DEFAULT_CONFIG names a file, that file is read
    instead: the image's built-in maintenance jobs run without the owner
    copying an example first."""
    import os
    from pathlib import Path

    path = getattr(args, "config", None)
    fallback = os.getenv("THROUGHLINE_COMPANION_DEFAULT_CONFIG")
    if path and fallback and not Path(path).is_file() and Path(fallback).is_file():
        logging.getLogger("throughline_companion").info(
            "%s not found: using the defaults in %s", path, fallback
        )
        path = fallback
    return Config(path)


def has_display():
    """Whether a person could be looking at this computer's screen now."""
    import os

    if sys.platform.startswith("win") or sys.platform == "darwin":
        return not os.getenv("SSH_CONNECTION")
    return bool(os.getenv("WAYLAND_DISPLAY") or os.getenv("DISPLAY"))


def cmd_run(args):
    from .app import Companion

    config = _config(args)
    _environment_pairing(config)
    # --headless: no tray, no browser (a service, an SSH install); every
    # desktop feature still runs. --server: the Docker runner.
    gui = not (args.headless or args.server) and has_display()
    companion = Companion(config, server=args.server, gui=gui)
    stop = threading.Event()

    def shutdown(*_):
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    companion.start()
    dashboard = None
    if not args.server and not args.no_dashboard:
        from .ui import Dashboard

        dashboard = Dashboard(companion, port=args.port or config.data["ui"]["port"])
        dashboard.start()
        logging.getLogger("throughline_companion").info("dashboard: http://127.0.0.1:%s/ (throughline-companion open)", dashboard.port)
        if gui and not config.paired and not args.no_browser:
            webbrowser.open(dashboard.url)
    try:
        shown = False
        if dashboard and gui and not args.no_tray:
            from . import tray

            if tray.available():
                shown = tray.run(companion, dashboard, stop.set)
        if not shown:
            while not stop.wait(1):
                pass
    finally:
        if dashboard:
            dashboard.stop()
        companion.stop()


def _environment_pairing(config):
    """THROUGHLINE_COMPANION_PAIRING (a pairing code; else the file named by
    THROUGHLINE_COMPANION_PAIRING_FILE) and
    THROUGHLINE_COMPANION_URL (the server as this process reaches it) apply for
    this run only and are never written to the settings file: the Docker
    service keeps its tokens in its environment and its settings read-only."""
    import os

    from .bridge import decode_pairing

    code = os.getenv("THROUGHLINE_COMPANION_PAIRING")
    pairing_file = os.getenv("THROUGHLINE_COMPANION_PAIRING_FILE")
    if not code and pairing_file and os.path.isfile(pairing_file):
        # Written by `throughline lib companion provision` (the migrate
        # service): the owner never copies a code into .env.
        with open(pairing_file, encoding="utf-8") as handle:
            code = handle.read().strip()
    if code:
        config.apply_pairing(decode_pairing(code), save=False)
    url = os.getenv("THROUGHLINE_COMPANION_URL")
    if url:
        config.server["url"] = url.rstrip("/")


def cmd_open(args):
    path = paths.state_dir() / "ui-url"
    if not path.exists():
        print("The companion is not running. Start it with: throughline-companion run "
              "(or throughline-companion service install)", file=sys.stderr)
        return 1
    url = path.read_text(encoding="utf-8").strip()
    if args.print or not has_display():
        port = url.split(":")[2].split("/")[0]
        print(url)
        print(f"From another computer: ssh -L {port}:127.0.0.1:{port} <this host>, then open the URL there.",
              file=sys.stderr)
        return 0
    webbrowser.open(url)
    print("Opened the dashboard in your browser.")
    return 0


def cmd_pair(args):
    from .bridge import decode_pairing

    config = _config(args)
    code = sys.stdin.readline() if args.code in (None, "-") else args.code
    bundle = decode_pairing(code)
    config.apply_pairing(bundle)
    print(f"Paired as {bundle['device']} with {bundle['url']}.")
    for name in sorted(bundle.get("plugin_tokens") or {}):
        print(f"  token for {name} (enabled)")
    return 0


def cmd_status(args):
    from .app import Companion

    companion = Companion(_config(args), isolate=False)
    state = companion.state()
    if args.json:
        print(json.dumps(state, indent=2, default=str))
        return 0
    print(f"Throughline Companion {state['version']} on {state['platform']}")
    if state["paired"]:
        print(f"  paired as {state['server']['device']} with {state['server']['url']}")
    else:
        print("  not paired: run `throughline lib companion enroll` on the server, then `pair`")
    aw = state["activitywatch"]
    print(f"  activitywatch: {aw['mode']}, {'reachable' if aw['reachable'] else 'not reachable'}")
    inf = state.get("infisical") or {}
    if inf.get("configured"):
        proj = inf.get("project_id") or inf.get("project_slug") or "-"
        print(
            f"  infisical: {inf['auth_mode']} ({inf['source']}), "
            f"{inf['site_url']} project={proj} env={inf['environment']} path={inf['secret_path']}"
        )
    for plugin in state["plugins"]:
        if not plugin["enabled"]:
            continue
        print(f"  {plugin['name']:<18} enabled  token={'yes' if plugin['has_token'] else 'NO'}")
    return 0


def cmd_detect(args):
    config = _config(args)
    for entry in catalog.CATALOG:
        item = catalog.describe(entry, config)
        if not item["supported"]:
            continue
        marks = []
        if item["detected"]:
            marks.append("found " + ", ".join(item["detected"]))
        if not item["installed"]:
            marks.append("not installed")
        if item["enabled"]:
            marks.append("enabled")
        print(f"{item['name']:<18} {'; '.join(marks) or '-'}")
    return 0


def cmd_plugin(args):
    from .app import Companion

    config = _config(args)
    companion = Companion(config, isolate=False)
    name = args.name
    if args.action == "enable":
        companion.update_plugin(name, {"enabled": True})
        print(f"{name} enabled")
    elif args.action == "disable":
        companion.update_plugin(name, {"enabled": False})
        print(f"{name} disabled")
    elif args.action == "set":
        env = dict(config.plugin(name).get("env") or {})
        for pair in args.values:
            key, sep, value = pair.partition("=")
            if not sep:
                raise SystemExit(f"expected KEY=VALUE, got {pair}")
            if key == "token":
                companion.update_plugin(name, {"token": value})
            elif key == "interval_s":
                companion.update_plugin(name, {"interval_s": int(value)})
            else:
                env[key] = value
        companion.update_plugin(name, {"env": env})
        print(f"{name} updated")
    elif args.action == "run":
        job = companion.runner.jobs.get(name)
        if not job:
            raise SystemExit(f"{name} is not enabled (or has no entry point)")
        ok = companion.runner.run(job)
        snap = job.snapshot()
        print(json.dumps(snap, indent=2))
        return 0 if ok else 1
    return 0


def cmd_activitywatch(args):
    from .app import Companion

    config = _config(args)
    companion = Companion(config, isolate=False)
    if args.action == "install":
        def progress(done, total):
            if total:
                print(f"\r  {done * 100 // total}%", end="", flush=True)

        path = activitywatch.install(progress)
        print(f"\nActivityWatch {activitywatch.VERSION} installed in {path}")
        companion.update_activitywatch({"mode": "managed"})
    elif args.action == "mode":
        companion.update_activitywatch({"mode": args.mode})
        print(f"activitywatch mode: {args.mode}")
    print(json.dumps(companion.activitywatch.snapshot(), indent=2))
    return 0


def cmd_capture(args):
    from .app import Companion

    config = _config(args)
    companion = Companion(config, isolate=False)
    if args.action == "status":
        pass
    elif args.action == "set":
        update = {}
        for pair in args.values:
            key, sep, value = pair.partition("=")
            if not sep:
                raise SystemExit(f"expected section.key=value, got {pair}")
            parsed = _value(value)
            section, dot, name = key.partition(".")
            if dot:
                update.setdefault(section, {})[name] = parsed
            else:
                update[key] = parsed
        companion.update_capture(update)
    elif args.action == "install-local":
        from .capture import local_whisper

        model = (args.values or ["small"])[0]
        local_whisper.install(model, progress=lambda text: print(text, flush=True))
        companion.update_capture({"transcription": {"engine": "local", "local_model": model}})
    elif args.action == "remove-local":
        companion.remove_local_model((args.values or [None])[0])
    elif args.action == "test-screen":
        path = companion.capture.capture_screen()
        print(f"saved {path}" if path else f"skipped: {companion.capture.stats}")
        return 0
    snap = companion.capture.snapshot()
    print(json.dumps(snap, indent=2, default=str))
    return 0


def _value(text):
    """A CLI value: JSON when it parses (true, 60, ["mic"]), else the text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def cmd_service(args):
    from . import service

    if args.action == "install":
        for note in service.install(linger=args.linger):
            print("note: " + note)
        print("service installed and started: " + json.dumps(service.status()))
    elif args.action == "uninstall":
        service.uninstall()
        print("service removed")
    elif args.action == "restart":
        service.restart()
        print("restarted: " + json.dumps(service.status()))
    elif args.action == "logs":
        log = paths.logs_dir() / "companion.log"
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-args.lines:] if log.exists() else []
        print("\n".join(lines))
    else:
        print(json.dumps(service.status()))
    return 0


def cmd_setup(args):
    """Everything an unattended install needs, in one non-interactive step."""
    from .app import Companion
    from .bridge import decode_pairing

    config = _config(args)
    if args.pair:
        code = sys.stdin.readline() if args.pair == "-" else args.pair
        bundle = decode_pairing(code)
        config.apply_pairing(bundle)
        print(f"paired as {bundle['device']} with {bundle['url']}")
    companion = Companion(config, isolate=False)
    names = [n for n in (args.enable or "").split(",") if n]
    if args.detected:
        for entry in catalog.CATALOG:
            if catalog.supported(entry) and catalog.detected(entry) and config.data["plugins"].get(entry["name"], {}).get("token"):
                names.append(entry["name"])
    for name in dict.fromkeys(names):
        companion.update_plugin(name, {"enabled": True})
        print(f"enabled {name}")
    for name in [n for n in (args.disable or "").split(",") if n]:
        companion.update_plugin(name, {"enabled": False})
        print(f"disabled {name}")
    for pair in args.set or []:
        plugin, _, rest = pair.partition(":")
        key, sep, value = rest.partition("=")
        if not sep:
            raise SystemExit(f"--set expects plugin:KEY=VALUE, got {pair}")
        env = dict(config.plugin(plugin).get("env") or {})
        env[key] = value
        companion.update_plugin(plugin, {"env": env})
    if args.activitywatch:
        companion.update_activitywatch({"mode": args.activitywatch})
        if args.activitywatch == "managed":
            activitywatch.install()
        print(f"activitywatch: {args.activitywatch}")
    if args.capture_screens is not None:
        update = {"enabled": True, "screens": {"enabled": True, "interval_s": args.capture_screens}}
        companion.update_capture(update)
        print(f"screen capture: every {args.capture_screens} s while active")
    if args.service:
        from . import service

        for note in service.install(linger=args.linger):
            print("note: " + note)
        print("service: " + json.dumps(service.status()))
    return 0


def cmd_deploy(args):
    from . import deploy

    code = None
    if args.pair:
        code = sys.stdin.readline().strip() if args.pair == "-" else args.pair
    return deploy.deploy(
        args.host,
        pairing=code,
        setup_args=args.setup_args,
        windows=args.windows,
        source=args.source,
        extras=args.extras,
        ssh_options=args.ssh_option,
    )


def cmd_infisical(args):
    from .app import Companion

    config = _config(args)
    companion = Companion(config, isolate=False)
    if args.action == "status":
        snap = companion.infisical_state()
        print(json.dumps(snap, indent=2))
        return 0
    if args.action == "set":
        patch = {}
        for pair in args.values:
            key, sep, value = pair.partition("=")
            if not sep:
                raise SystemExit(f"expected key=value, got {pair}")
            patch[key] = value
        snap = companion.update_infisical(patch)
        print(json.dumps(snap, indent=2))
        return 0
    if args.action == "test":
        result = companion.test_infisical()
        print(json.dumps(result, indent=2))
        return 0
    if args.action == "list":
        items = companion.list_infisical_secrets(
            path=args.path, environment=args.env, recursive=args.recursive
        )
        if args.json:
            print(json.dumps(items, indent=2))
            return 0
        if not items:
            print("No secrets found.")
            return 0
        for item in items:
            tag = " [imported]" if item.get("imported") else ""
            print(f"{item['ref']:<42} {item['path']} ({item['environment']}){tag}")
        return 0
    return 0


def cmd_autostart(args):
    on = args.state == "on"
    autostart.enable() if on else autostart.disable()
    print("start at login: " + ("on" if on else "off"))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="throughline-companion", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="settings file (default: the per-user companion.json)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the companion")
    run.add_argument("--headless", action="store_true",
                     help="no tray, no browser (services, SSH installs); connectors and capture still run")
    run.add_argument("--server", action="store_true",
                     help="server mode (Docker): server connectors only, isolated job environments")
    run.add_argument("--no-dashboard", action="store_true", help="do not serve the local dashboard")
    run.add_argument("--no-tray", action="store_true")
    run.add_argument("--no-browser", action="store_true", help="never open the dashboard automatically")
    run.add_argument("--port", type=int)
    run.set_defaults(func=cmd_run)

    op = sub.add_parser("open", help="open the dashboard (prints the URL when there is no screen)")
    op.add_argument("--print", action="store_true", help="print the URL instead of opening a browser")
    op.set_defaults(func=cmd_open)

    svc = sub.add_parser("service", help="run in the background at login (systemd, launchd, Task Scheduler)")
    svc.add_argument("action", choices=["install", "uninstall", "status", "restart", "logs"])
    svc.add_argument("--linger", action="store_true", help="Linux: keep running with nobody logged in")
    svc.add_argument("--lines", type=int, default=100)
    svc.set_defaults(func=cmd_service)

    setup = sub.add_parser("setup", help="unattended setup: pair, enable connectors, capture, service")
    setup.add_argument("--pair", metavar="CODE", help="pairing code, or - to read it from stdin")
    setup.add_argument("--enable", metavar="A,B", help="connectors to switch on")
    setup.add_argument("--disable", metavar="A,B")
    setup.add_argument("--detected", action="store_true",
                       help="switch on every connector that has a token and found data here")
    setup.add_argument("--set", action="append", metavar="PLUGIN:KEY=VALUE", help="a connector setting (repeat)")
    setup.add_argument("--activitywatch", choices=["off", "external", "managed"])
    setup.add_argument("--capture-screens", type=int, metavar="SECONDS",
                       help="switch on screenshots every SECONDS while you are active")
    setup.add_argument("--service", action="store_true", help="install and start the background service")
    setup.add_argument("--linger", action="store_true")
    setup.set_defaults(func=cmd_setup)

    dep = sub.add_parser("deploy", help="install the companion on another computer over SSH")
    dep.add_argument("host", help="user@host (anything ssh accepts)")
    dep.add_argument("--pair", metavar="CODE", help="pairing code for that computer, or - for stdin")
    dep.add_argument("--windows", action="store_true", help="the target runs Windows (OpenSSH + PowerShell)")
    dep.add_argument("--source", help="what to install: a wheel folder, a checkout, or a pip/git URL "
                                      "(default: wheels built from this checkout)")
    dep.add_argument("--extras", default="capture,cookies,desktop-plugins")
    dep.add_argument("--ssh-option", action="append", default=[], metavar="OPT", help="extra ssh -o option")
    dep.epilog = "Arguments after -- go to `setup` on the target, e.g. -- --detected --capture-screens 60 --service"
    dep.set_defaults(func=cmd_deploy, setup_args=[])

    pair = sub.add_parser("pair", help="store a pairing code (or read it from stdin)")
    pair.add_argument("code", nargs="?")
    pair.set_defaults(func=cmd_pair)

    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    sub.add_parser("detect", help="desktop connectors with data on this computer").set_defaults(func=cmd_detect)

    plugin = sub.add_parser("plugin", help="configure or run one connector")
    plugin.add_argument("action", choices=["enable", "disable", "set", "run"])
    plugin.add_argument("name")
    plugin.add_argument("values", nargs="*", help="KEY=VALUE settings for set (token=..., interval_s=...)")
    plugin.set_defaults(func=cmd_plugin)

    aw = sub.add_parser("activitywatch", help="bundled ActivityWatch")
    aw.add_argument("action", choices=["status", "install", "mode"])
    aw.add_argument("mode", nargs="?", choices=["off", "external", "managed"])
    aw.set_defaults(func=cmd_activitywatch)

    cap = sub.add_parser("capture", help="screen and audio capture (optional)")
    cap.add_argument("action", choices=["status", "set", "install-local", "remove-local", "test-screen"])
    cap.add_argument("values", nargs="*", help="set: enabled=true screens.interval_s=60 audio.enabled=false ...; install-local MODEL")
    cap.set_defaults(func=cmd_capture)

    inf = sub.add_parser("infisical", help="Infisical secret store connection and secret listing")
    inf.add_argument("action", choices=["status", "set", "test", "list"])
    inf.add_argument(
        "values",
        nargs="*",
        help="set: site_url=... client_id=... client_secret=... token=... project_id=... project_slug=... environment=... secret_path=...",
    )
    inf.add_argument("--path", help="list: folder path inside Infisical")
    inf.add_argument("--env", help="list: Infisical environment slug")
    inf.add_argument("--recursive", "-r", action="store_true", help="list: include subfolders")
    inf.add_argument("--json", action="store_true", help="list: output JSON")
    inf.set_defaults(func=cmd_infisical)

    auto = sub.add_parser("autostart", help="start at login")
    auto.add_argument("state", choices=["on", "off"])
    auto.set_defaults(func=cmd_autostart)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Internal: the runner starts connectors through this same program, so a
    # packaged app needs no Python installed.
    if argv[:1] == ["exec"] and len(argv) >= 2:
        from .runner import exec_entry

        return exec_entry(argv[1], argv[2:])
    if argv[:1] == ["exec-module"] and len(argv) >= 2:
        from .runner import exec_module

        return exec_module(argv[1], argv[2:])
    setup_args = []
    if argv[:1] == ["deploy"] and "--" in argv:
        cut = argv.index("--")
        argv, setup_args = argv[:cut], argv[cut + 1:]
    args = parser().parse_args(argv or ["run"])
    if setup_args:
        args.setup_args = setup_args
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if not args.verbose:
        # One line per long-poll is noise; errors still show.
        logging.getLogger("httpx").setLevel(logging.WARNING)
    if not getattr(args, "func", None):
        args = parser().parse_args(argv + ["run"])
    if args.command == "activitywatch" and args.action == "mode" and not args.mode:
        raise SystemExit("activitywatch mode needs off, external or managed")
    try:
        return args.func(args) or 0
    except (ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def gui():
    """Entry point of the windowed (no console) build."""
    sys.exit(main(sys.argv[1:] or ["run"]))


if __name__ == "__main__":
    sys.exit(main())
