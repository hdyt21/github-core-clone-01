"""Entrypoint: python3 main.py

Startup sequence (the harness lifecycle, after the kit's starter/main.ts):
  1. MAKEFUN_ENV_URL gives the env origin. If it is missing there is
     nothing to talk to, so exit 1 immediately.
  2. Bind an ephemeral port (port 0 lets the kernel choose). Never 8080.
  3. POST $MAKEFUN_ENV_URL/register {"port": N}. The env stores the
     caller address plus N as the base URL. 200 is the normal answer;
     409 means an earlier register already won (wait on like any other).
  4. GET $MAKEFUN_ENV_URL/done until the body reads {"done": true}. Poll
     with a short exponential backoff; never keep a request open, and
     never exit before it answers true.
  5. Shut the server down and exit 0.

Standard library only. No model calls, no network beyond the env.
"""

import json
import os
import sys
import threading
import time
from urllib import error as url_error
from urllib import request as url_request

# The grader launches `python3 -P -s /bot/main.py`: -P removes the script's
# own folder from sys.path, so put it back before importing forge.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge.server import VIEWER_LOGIN as VIEWER, create_server as build_server
from forge.store import Store as ForgeStore

REQUEST_TIMEOUT_SECONDS = 30
RETRY_FIRST_SECONDS = 0.25
RETRY_LIMIT_SECONDS = 2.0


def _backoff_delay(tries: int) -> float:
    return min(RETRY_LIMIT_SECONDS, RETRY_FIRST_SECONDS * (2 ** min(tries, 8)))


def _post_payload(url: str, obj: dict, timeout: float) -> int:
    payload = json.dumps(obj).encode("utf-8")
    http_req = url_request.Request(url, data=payload, method="POST",
                                   headers={"Content-Type": "application/json"})
    try:
        with url_request.urlopen(http_req, timeout=timeout) as reply:
            return reply.status
    except url_error.HTTPError as err:
        return err.code
    except Exception:
        return 0


def _fetch_json(url: str, timeout: float) -> dict | None:
    http_req = url_request.Request(url, method="GET")
    try:
        with url_request.urlopen(http_req, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))
    except Exception:
        return None


def register_port(listen_port: int, env_origin: str) -> None:
    """POST the bound port until the env accepts it (200 or 409)."""
    tries = 0
    while True:
        code = _post_payload(env_origin + "/register", {"port": listen_port},
                             REQUEST_TIMEOUT_SECONDS)
        if code in (200, 409):
            return
        if code in (400, 405):
            raise RuntimeError("register answered HTTP %d" % code)
        time.sleep(_backoff_delay(tries))
        tries += 1


def await_done(env_origin: str) -> None:
    """Poll GET /done until it answers {"done": true}."""
    tries = 0
    while True:
        payload = _fetch_json(env_origin + "/done", REQUEST_TIMEOUT_SECONDS)
        if isinstance(payload, dict) and payload.get("done") is True:
            return
        time.sleep(_backoff_delay(tries))
        tries += 1


def run_entry() -> int:
    env_origin = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_origin:
        sys.stderr.write("[entry] MAKEFUN_ENV_URL is not set: "
                         "there is no env to register with\n")
        return 1
    shop_token = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
    forge_store = ForgeStore(owner_login=VIEWER)
    forge_server = build_server(forge_store, shop_token, port=0)
    # build_server already binds + activates (port 0 = ephemeral).
    listen_port = forge_server.server_address[1]
    # Serve on a background thread: the main thread keeps polling /done.
    serve_worker = threading.Thread(target=forge_server.serve_forever, daemon=True)
    serve_worker.start()
    sys.stdout.write("[entry] listening on port %d\n" % listen_port)
    sys.stdout.flush()
    try:
        register_port(listen_port, env_origin)
    except RuntimeError as err:
        sys.stderr.write("[entry] %s\n" % err)
        forge_server.shutdown()
        forge_server.server_close()
        return 1
    sys.stdout.write("[entry] registered; waiting for the env to say done\n")
    sys.stdout.flush()
    await_done(env_origin)
    forge_server.shutdown()
    forge_server.server_close()
    sys.stdout.write("[entry] done\n")
    return 0


if __name__ == "__main__":
    sys.exit(run_entry())
