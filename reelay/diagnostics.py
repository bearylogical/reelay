"""Read-only self-test of the Overseerr -> Radarr/Sonarr chain.

A request made in the Mini App only reaches a download if four separate links
hold: Reelay can talk to Overseerr, Overseerr can talk to Radarr and Sonarr,
and (for the queue view and the legacy add flow) Reelay can talk to Radarr and
Sonarr directly. When one of those is misconfigured nothing errors loudly --
requests just quietly never arrive -- so an admin otherwise has to read three
services' logs to find out which link is down.

Every check here is a GET. Nothing is created, changed, or deleted, so it is
safe to run at any time.

Two links can't be probed from here and are reported from what we know instead:

  - Whether Overseerr's Radarr/Sonarr is the *same instance* Reelay talks to
    directly. Comparing hostnames is useless (Overseerr in Docker says
    "radarr:7878" for the box Reelay reaches at "192.168.1.5:7878"), so we
    compare root folder paths, which are a property of the instance rather
    than of the network between them.

  - The inbound webhooks. Nothing we send can make Radarr call us, so those
    rows report whether a secret is configured and when that source last
    actually delivered something.
"""

import logging

import requests

from . import commons
from . import db
from . import logger
from . import overseerr
from . import radarr
from . import sonarr
from .config import config

logLevel = logging.DEBUG if config.get("debugLogging", False) else logging.INFO
logger = logger.getLogger("reelay.diagnostics", logLevel, config.get("logToConsole", False))

# Short on purpose: a wedged service should make the self-test slow, not make
# it look like the whole Mini App has hung. Worst case is one timeout per check.
TIMEOUT = 8

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

_ARR_LABELS = {"radarr": "Radarr", "sonarr": "Sonarr"}


def _result(check_id, service, status, summary, detail=None):
    return {"id": check_id, "service": service, "status": status, "summary": summary, "detail": detail}


# --- Reelay -> Radarr/Sonarr ---------------------------------------------------

def _checkArr(app):
    """Direct connectivity to one *arr. Returns (result, rootFolderPaths) --
    the paths are how _checkOverseerrArrLink() later decides whether Overseerr
    is pointed at this same instance, and are None when we never got that far."""
    label = _ARR_LABELS[app]
    server = (config.get(app) or {}).get("server") or {}
    if not server.get("addr"):
        return _result(app, label, SKIP, "Not configured",
                       f"No {app}.server.addr in config.yaml."), None

    url = commons.generateApiQuery(app, "system/status")
    if not url:
        return _result(app, label, FAIL, "Couldn't build an API URL",
                       f"Check the {app}.server and {app}.auth blocks in config.yaml."), None
    try:
        resp = requests.get(url, timeout=TIMEOUT)
    except Exception as e:
        return _result(app, label, FAIL, "Unreachable", str(e)), None
    if resp.status_code in (401, 403):
        return _result(app, label, FAIL, "API key rejected",
                       f"HTTP {resp.status_code} — check {app}.auth.apikey."), None
    if resp.status_code >= 400:
        return _result(app, label, FAIL, f"HTTP {resp.status_code}", resp.text[:200] or None), None
    try:
        version = (resp.json() or {}).get("version")
    except ValueError:
        return _result(app, label, FAIL, "Unexpected response",
                       "That address answered, but not like a *arr API would."), None

    module = radarr if app == "radarr" else sonarr
    roots = [r.get("path") for r in module.getRootFolders() if r.get("path")]
    profiles = module.getQualityProfiles()
    detail = f"{len(roots)} root folder(s), {len(profiles)} quality profile(s)."
    if not roots or not profiles:
        # Reachable but unusable: the add flow needs both to build a payload.
        missing = " and ".join(filter(None, [
            "no root folders" if not roots else "", "no quality profiles" if not profiles else "",
        ]))
        return _result(app, label, WARN, f"Connected, but {missing}",
                       f"Version {version}. Adds will fail until that's set up in {label}."), roots
    return _result(app, label, OK, f"Connected (v{version})", detail), roots


# --- Reelay -> Overseerr -------------------------------------------------------

def _checkOverseerr():
    if not overseerr.enabled():
        return _result("overseerr", "Overseerr", SKIP, "Not enabled",
                       "Set overseerr.enable and overseerr.url in config.yaml.")
    status, err = overseerr.apiGet("/status", timeout=TIMEOUT)
    if err:
        return _result("overseerr", "Overseerr", FAIL, "Unreachable", err)
    version = (status or {}).get("version") or "?"
    # /status is public, so it proves reachability but says nothing about the
    # API key. /request/count is the cheapest authenticated call there is.
    _, err = overseerr.apiGet("/request/count", timeout=TIMEOUT)
    if err:
        return _result("overseerr", "Overseerr", FAIL, "API key rejected",
                       f"Reachable (v{version}) but {err}. Check overseerr.apikey.")
    return _result("overseerr", "Overseerr", OK, f"Connected (v{version})", None)


# --- Overseerr -> Radarr/Sonarr ------------------------------------------------

def _checkOverseerrArrLink(app, reelay_roots):
    """Ask Overseerr to talk to its configured *arr on our behalf: /service/<app>
    proxies a live call, so a reply with profiles and root folders means that
    link genuinely works right now."""
    label = _ARR_LABELS[app]
    check_id = f"overseerr-{app}"
    service = f"Overseerr → {label}"
    if not overseerr.enabled():
        return _result(check_id, service, SKIP, "Overseerr not enabled", None)

    servers, err = overseerr.apiGet(f"/settings/{app}", timeout=TIMEOUT)
    if err:
        return _result(check_id, service, FAIL, "Couldn't read Overseerr's settings", err)
    if not servers:
        return _result(check_id, service, FAIL, f"No {label} server in Overseerr",
                       f"Add one in Overseerr → Settings → Services → {label}.")

    chosen = next((s for s in servers if s.get("isDefault") and not s.get("is4k")), servers[0])
    data, err = overseerr.apiGet(f"/service/{app}/{chosen.get('id')}", timeout=TIMEOUT)
    if err:
        return _result(check_id, service, FAIL, f"Overseerr can't reach its {label}",
                       f"{err}. Test the server in Overseerr → Settings → Services → {label}.")

    roots = [r.get("path") for r in (data.get("rootFolders") or []) if r.get("path")]
    profiles = data.get("profiles") or []
    name = chosen.get("name") or label
    if not roots or not profiles:
        return _result(check_id, service, WARN, f"Answered, but {label} returned nothing usable",
                       f"{name}: {len(roots)} root folder(s), {len(profiles)} quality profile(s).")

    detail = f"{name}: {len(roots)} root folder(s), {len(profiles)} quality profile(s)."
    if not any(s.get("isDefault") for s in servers):
        return _result(check_id, service, WARN, f"No default {label} server",
                       f"{detail} Overseerr won't route requests until one is marked default.")
    if reelay_roots and not set(roots) & set(reelay_roots):
        # Not necessarily broken -- but it means the queue Reelay shows and the
        # library Overseerr fills are two different servers, which is almost
        # always a copy-paste accident rather than a deliberate split.
        return _result(check_id, service, WARN, f"Points at a different {label} than Reelay does",
                       f"{detail} No root folder is shared with the {label} in config.yaml.")
    return _result(check_id, service, OK, "Linked", detail)


# --- Inbound webhooks ----------------------------------------------------------

def _checkWebhook(service, last_events):
    label = _ARR_LABELS.get(service, service.title())
    check_id = f"webhook-{service}"
    name = f"{label} webhook"
    if not (config.get(service) or {}).get("webhookSecret"):
        return _result(check_id, name, SKIP, "Not configured",
                       f"Optional. Set {service}.webhookSecret to feed {label} events into the weekly digest.")
    last = last_events.get(service)
    if not last:
        return _result(check_id, name, WARN, "Nothing received yet",
                       f"Configured here, but {label} hasn't delivered an event in the last 30 days. "
                       f"Use its Test button to check the URL.")
    return _result(check_id, name, OK, "Delivering", f"Last event {last}.")


# --- Entry point ---------------------------------------------------------------

def run():
    """Run every check and return {"ok", "checks"}. Blocking (this is the same
    `requests` stack the rest of the bot uses), so callers on the event loop
    should hand it to a thread."""
    checks = []
    arr_roots = {}
    for app in ("radarr", "sonarr"):
        result, roots = _checkArr(app)
        checks.append(result)
        arr_roots[app] = roots

    checks.append(_checkOverseerr())
    for app in ("radarr", "sonarr"):
        checks.append(_checkOverseerrArrLink(app, arr_roots.get(app)))

    try:
        last_events = db.getLastMediaEventBySource()
    except Exception as e:
        logger.warning(f"Diagnostics: could not read media events: {e}")
        last_events = {}
    for service in ("overseerr", "radarr", "sonarr"):
        checks.append(_checkWebhook(service, last_events))

    failed = [c["id"] for c in checks if c["status"] == FAIL]
    if failed:
        logger.warning(f"Diagnostics self-test failed: {', '.join(failed)}")
    else:
        logger.info("Diagnostics self-test passed.")
    return {"ok": not failed, "checks": checks}
