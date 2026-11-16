"""The fabric worker: runs every service's event consumers for every workspace (`python -m cove.fabric.worker`).

It is its own container (docker-compose `worker`, from the `x-app` anchor) rather than a loop inside `web`:
a consumer that is slow or crashes can't hold up page loads or the sync API, restarting the web app doesn't
interrupt it, and it runs exactly once even if `web` is ever started with several processes.

Each round, for each active workspace, it acts as that workspace's owner (`worker:fabric`) and gives each
enabled service's consumers the new events. Old events are pruned once an hour.
"""

from __future__ import annotations

import argparse
import logging
import os
import time

from cove import context
from cove.auth import policy
from cove.fabric import core, events, services

log = logging.getLogger("cove.fabric.worker")
INTERVAL = float(os.environ.get("COVE_WORKER_INTERVAL", "10"))


def principal(ws: context.Workspace) -> context.Principal:
    return context.Principal(actor="worker:fabric", workspace=ws, role="owner", scopes=policy.ALL)


def run_workspace(ws: context.Workspace, prune: bool = False) -> int:
    """One round for one workspace; returns the number of events handled."""
    handled = 0
    with context.use(principal(ws)):
        st = services.states()
        for key, m in core.manifests().items():
            if st.get(key) != "enabled":
                continue
            for consumer in m.consumers:
                handled += events.consume(consumer.name, consumer.types, consumer.handler)
        if prune:
            events.prune()
    return handled


def _workspaces() -> list[context.Workspace]:
    from cove.control import workspaces
    return [workspaces.as_context(w) for w in workspaces.list_all() if w["status"] in ("active", "needs_attention")]


def run_once(prune: bool = False) -> int:
    total = 0
    for ws in _workspaces():
        try:
            total += run_workspace(ws, prune)
        except Exception as e:  # noqa: BLE001 - one broken workspace must not stop the others
            log.warning("worker: workspace %s failed: %s", ws.slug, type(e).__name__)
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="run one round and exit")
    args = ap.parse_args()
    last_prune = 0.0
    while True:
        prune = time.monotonic() - last_prune > 3600
        n = run_once(prune)
        if prune:
            last_prune = time.monotonic()
        if n:
            log.info("worker: handled %d events", n)
        if args.once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
