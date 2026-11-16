"""The fabric: how cove services use each other without depending on each other (docs/fabric.md).

    from cove import fabric
    r = fabric.ask("identity.resolve", provider="people", identifiers=[...])   # Result | Unavailable
    g = fabric.gather("timeline.items", subject_ref=ref)                        # .values, .coverage
    fabric.publish(c, "people", "people.contact.updated", ref)                  # inside the write's transaction
"""

from cove.fabric.core import (REASONS, Capability, Consumer, Gathered, Manifest, Result, Unavailable,  # noqa: F401
                              ask, caller_is_agent, contracts, coverage, describe, gather, manifests, parse_ref,
                              providers, ref, register, unregister)
from cove.fabric.events import consume, publish  # noqa: F401  (the feed: cove.fabric.events.events)
from cove.fabric.services import is_enabled, state, states  # noqa: F401
