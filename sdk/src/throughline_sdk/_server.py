"""Disposable local PostgreSQL database and external core process."""

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx


@contextlib.contextmanager
def core_server(reparse_channels=()):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    admin = os.getenv(
        "THROUGHLINE_TEST_DATABASE_URL",
        "postgresql://postgres@127.0.0.1:54331/postgres",
    )
    settings = conninfo_to_dict(admin)
    if (
        settings.get("host") != "127.0.0.1"
        or settings.get("hostaddr") not in (None, "127.0.0.1")
        or settings.get("dbname") != "postgres"
    ):
        raise ValueError("contract tests require local administrative postgres")
    name = "tl_test_" + uuid.uuid4().hex
    process = None
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings["dbname"] = name
    env = dict(
        os.environ,
        THROUGHLINE_DATABASE_URL=make_conninfo(**settings),
        THROUGHLINE_DEV="1",
    )
    try:
        _safe_run(
            [sys.executable, "-m", "throughline", "migrate"],
            env=env,
            check=True,
            capture_output=True,
        )
        token = json.loads(
            _safe_output(
                [
                    sys.executable,
                    "-m",
                    "throughline",
                    "token",
                    "create",
                    "contract",
                    "--json",
                ],
                env=env,
            )
        )["token"]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryFile() as log:
            bootstrap = (
                "import json, uvicorn; from throughline.reparse import register_mapper; "
                f"[register_mapper(c, lambda payload: {{'content': json.dumps(payload, sort_keys=True)}}) for c in {list(reparse_channels)!r}]; "
                f"uvicorn.run('throughline.api:create_app', factory=True, host='127.0.0.1', port={port})"
            )
            process = _safe_popen(
                [sys.executable, "-c", bootstrap], env=env, stdout=log, stderr=log
            )
            with httpx.Client(
                base_url=url + "/v1/",
                headers={"Authorization": "Bearer " + token},
                timeout=60,
            ) as client:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    try:
                        if (
                            client.get(
                                "channels",
                                timeout=min(5, max(0.1, deadline - time.monotonic())),
                            ).status_code
                            == 200
                        ):
                            break
                    except httpx.TransportError:
                        pass
                    if process.poll() is not None:
                        raise RuntimeError("core server exited")
                    time.sleep(0.05)
                else:
                    raise RuntimeError("core server did not start")
                yield client, dict(env, THROUGHLINE_URL=url, THROUGHLINE_TOKEN=token)
    finally:
        if process:
            process.terminate()
            process.wait(timeout=15)
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _safe_run(*args, **kwargs):
    try:
        return subprocess.run(*args, **kwargs)
    except (subprocess.SubprocessError, OSError):
        args, kwargs = (), {}  # Pytest includes function arguments in tracebacks.
        raise RuntimeError("core subprocess failed (details redacted)") from None


def _safe_output(*args, **kwargs):
    kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    try:
        return _safe_run(*args, **kwargs).stdout
    except RuntimeError:
        args, kwargs = (), {}
        raise


def _safe_popen(*args, **kwargs):
    try:
        return subprocess.Popen(*args, **kwargs)
    except (subprocess.SubprocessError, OSError):
        args, kwargs = (), {}  # Pytest includes function arguments in tracebacks.
        raise RuntimeError("core server launch failed (details redacted)") from None
