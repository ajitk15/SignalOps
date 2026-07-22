"""What "approved" is pinned to.

An approval that names only a run is worthless: the plan can change between the
click and the execution, and the audit trail would still read as though a human
signed off on whatever ran. So an approval is bound to a hash of the exact
payload it was shown.

The hash is over a canonical form — sorted keys, no insignificant whitespace —
so a payload that is *semantically* the same still matches after a round trip
through JSON and back, while a payload with one step edited does not.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class StaleApproval(Exception):
    """The payload changed after it was approved. The decision does not carry
    over to the new one — a human has to look again."""
