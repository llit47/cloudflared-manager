"""Deterministic identity for non-secret mutable runtime settings."""

import hashlib
import json


def runtime_config_id(bind_host: str, bind_port: int, discovery_enabled: bool) -> str:
    encoded = json.dumps(
        [bind_host, bind_port, discovery_enabled], separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
