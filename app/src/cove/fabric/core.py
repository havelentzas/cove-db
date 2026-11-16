"""Fabric core: manifests, capabilities, `ask` and `gather` (docs/fabric.md).

A consumer never gets an exception from another service: it gets a `Result` or an `Unavailable(reason)`.
Every call runs as the current principal, checks the capability's scope, and is bounded by a timeout.
Arguments and results are never logged (they can hold message text); only capability, service, outcome
and duration are.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

from cove import context
from cove.auth import policy

log = logging.getLogger("cove.fabric")

REASONS = ("disabled", "not_installed", "no_permission", "error", "timeout")
DEFAULT_TIMEOUT = float(os.environ.get("COVE_FABRIC_TIMEOUT", "3"))
CONTRACT_DIR = Path(os.environ.get("COVE_SCHEMA_DIR", Path(__file__).resolve().parents[4] / "schemas")) / "contracts"
# Where each service's manifest lives. A module that fails to import is simply not installed.
SERVICE_MODULES = {"people": "cove.crm.fabric", "notes": "cove.notes.fabric", "messages": "cove.messages.fabric"}

_REF = re.compile(r"^cove:([a-z][a-z0-9_]*)/([a-z_]+)/([A-Za-z0-9._:-]{1,200})$")


# ================================================================ results
@dataclass(frozen=True)
class Result:
    service: str
    value: Any
    ok = True
    reason = None


@dataclass(frozen=True)
class Unavailable:
    service: str
    reason: str            # one of REASONS
    ok = False
    value = None

    def as_missing(self) -> dict:
        return {"service": self.service, "reason": self.reason}


@dataclass
class Gathered:
    """The answers of every provider of a capability, plus what's missing and why."""
    results: list = field(default_factory=list)

    @property
    def values(self) -> dict[str, Any]:
        return {r.service: r.value for r in self.results if r.ok}

    @property
    def coverage(self) -> dict:
        return coverage(self.results)


def coverage(*groups) -> dict:
    """Merge results (or lists of them, or earlier coverage dicts) into {included, missing}.

    A service that failed anywhere is reported missing (with its first reason) and not included."""
    included, missing = [], {}
    def add(x):
        if isinstance(x, (list, tuple)):
            for y in x:
                add(y)
        elif isinstance(x, dict):
            for s in x.get("included", []):
                included.append(s)
            for m in x.get("missing", []):
                missing.setdefault(m["service"], m["reason"])
        elif x.ok:
            included.append(x.service)
        else:
            missing.setdefault(x.service, x.reason)
    add(groups)
    return {"included": sorted({s for s in included if s not in missing}),
            "missing": [{"service": s, "reason": r} for s, r in sorted(missing.items())]}


# ================================================================ refs
def ref(service: str, kind: str, id: Any) -> str:
    return f"cove:{service}/{kind}/{id}"


def parse_ref(text: Any) -> tuple[str, str, str] | None:
    m = _REF.match(str(text or "").strip())
    return (m.group(1), m.group(2), m.group(3)) if m else None


# ================================================================ manifests
@dataclass
class Capability:
    name: str
    fn: Callable[..., Any]
    scope: str | None
    description: str = ""
    timeout: float | None = None


@dataclass
class Consumer:
    name: str
    types: tuple[str, ...]
    handler: Callable[[list[dict]], None]


@dataclass
class Manifest:
    key: str
    version: int
    read_scope: str | None
    provides: dict[str, Capability] = field(default_factory=dict)
    consumes: tuple[str, ...] = ()
    publishes: tuple[str, ...] = ()
    consumers: tuple[Consumer, ...] = ()


_registry: dict[str, Manifest] = {}
_loaded = False


def register(manifest: Manifest) -> Manifest:
    unknown = set(manifest.provides) - set(contracts())
    if unknown:
        raise ValueError(f"{manifest.key} provides capabilities without a contract: {sorted(unknown)}")
    _registry[manifest.key] = manifest
    return manifest


def unregister(key: str) -> None:
    _registry.pop(key, None)


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for key, module in SERVICE_MODULES.items():
        try:
            import_module(module)            # the module calls register() itself
        except ImportError:
            log.info("fabric: service %s has no manifest module (%s); treated as not installed", key, module)


def manifests() -> dict[str, Manifest]:
    _load()
    return dict(_registry)


def providers(capability: str) -> list[tuple[str, Capability]]:
    return [(k, m.provides[capability]) for k, m in manifests().items() if capability in m.provides]


@lru_cache(maxsize=1)
def contracts() -> dict[str, dict]:
    out = {}
    for f in sorted(CONTRACT_DIR.glob("*.v1.json")):
        name = f.name.removesuffix(".v1.json")
        doc = json.loads(f.read_text())
        if "input" in doc and "output" in doc:
            out[name] = doc
    return out


# ================================================================ calling
_executor = ThreadPoolExecutor(max_workers=int(os.environ.get("COVE_FABRIC_THREADS", "16")),
                               thread_name_prefix="fabric")
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("cove_fabric_depth", default=0)


def _invoke(service: str, cap: Capability, kw: dict):
    """Run the provider, mapping every failure to Unavailable. Runs in the caller's context (principal)."""
    token = _depth.set(_depth.get() + 1)
    try:
        return Result(service, cap.fn(**kw))
    except policy.Forbidden:
        return Unavailable(service, "no_permission")
    except Exception as e:  # noqa: BLE001 - the point of the fabric: never let a provider break the caller
        log.warning("fabric: %s from %s failed: %s", cap.name, service, type(e).__name__)
        return Unavailable(service, "error")
    finally:
        _depth.reset(token)


def _precheck(service: str, cap: Capability, states: dict[str, str]) -> Unavailable | None:
    state = states.get(service, "not_installed")
    if state != "enabled":
        return Unavailable(service, state)
    if not policy.allows(cap.scope):
        return Unavailable(service, "no_permission")
    return None


def _states() -> dict[str, str]:
    from cove.fabric import services
    return services.states()


def _start(service: str, cap: Capability, kw: dict, states: dict[str, str]):
    """Begin a call: an immediate answer (precheck failed, or nested call run inline) or a future."""
    early = _precheck(service, cap, states)
    if early is not None:
        return early
    if _depth.get() > 0:   # called from inside a provider: run inline, bounded by the outer timeout
        return _invoke(service, cap, kw)
    ctx = contextvars.copy_context()
    return _executor.submit(ctx.run, _invoke, service, cap, kw)


def _finish(service: str, cap: Capability, pending, deadline: float, started: float):
    if isinstance(pending, (Result, Unavailable)):
        res = pending
    else:
        try:
            res = pending.result(timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeout:
            pending.cancel()
            res = Unavailable(service, "timeout")
    log.info("fabric: %s from %s: %s in %.0f ms", cap.name, service, "ok" if res.ok else res.reason,
             (time.monotonic() - started) * 1000)
    return res


def ask(capability: str, provider: str | None = None, *, timeout: float | None = None, **kw) -> Result | Unavailable:
    """One answer to `capability`: from `provider`, or from the first provider that can answer."""
    cands = providers(capability)
    if provider is not None:
        cands = [c for c in cands if c[0] == provider]
    if not cands:
        return Unavailable(provider or capability.split(".")[0], "not_installed")
    states = _states()
    last: Unavailable | None = None
    for service, cap in cands:
        started = time.monotonic()
        deadline = started + (timeout or cap.timeout or DEFAULT_TIMEOUT)
        res = _finish(service, cap, _start(service, cap, kw, states), deadline, started)
        if res.ok:
            return res
        last = res
    return last


def gather(capability: str, *, timeout: float | None = None, exclude: tuple[str, ...] = (), **kw) -> Gathered:
    """Ask every provider of `capability` in parallel; each gets its own timeout."""
    states = _states()
    cands = [(s, c) for s, c in providers(capability) if s not in exclude]
    started = time.monotonic()
    pending = [(s, c, _start(s, c, kw, states)) for s, c in cands]
    out = Gathered()
    for s, c, p in pending:
        out.results.append(_finish(s, c, p, started + (timeout or c.timeout or DEFAULT_TIMEOUT), started))
    return out


# ================================================================ introspection
def describe() -> list[dict]:
    """Every known service: its state here, what it provides (and whether you can use it), consumes, publishes."""
    states = _states()
    out = []
    for key in sorted(set(manifests()) | set(states)):
        m = _registry.get(key)
        state = states.get(key, "not_installed") if m else "not_installed"
        out.append({
            "service": key, "state": state, "version": m.version if m else None,
            "provides": [{"capability": c.name, "scope": c.scope, "description": c.description,
                          "available": state == "enabled" and policy.allows(c.scope)}
                         for c in (m.provides.values() if m else [])],
            "consumes": list(m.consumes) if m else [], "publishes": list(m.publishes) if m else [],
        })
    return out


def caller_is_agent() -> bool:
    """True for MCP and personal-token API callers: they never see hidden or sensitive data."""
    p = context.current()
    return bool(p and p.agent)


from cove.auth import policy as _policy  # noqa: E402

_policy.guard(__name__, {
    _policy.PURE: ["coverage", "ref", "parse_ref", "register", "unregister", "manifests", "providers", "contracts",
                   "caller_is_agent"],
    # each capability checks its own scope; these only orchestrate
    None: ["ask", "gather", "describe"],
})
