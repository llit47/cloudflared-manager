"""Round-trip YAML loading and narrow structural mutation primitives."""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from cloudflared_manager.cloudflared.editing.errors import (
    MutationRejectedError,
    RoundTripYamlError,
    UnsupportedConfigStructureError,
)
from cloudflared_manager.cloudflared.editing.source import ConfigSourceSnapshot
from cloudflared_manager.cloudflared.limits import MAX_CLOUDFLARED_CONFIG_BYTES

_MAX_YAML_DEPTH = 100
_CORE_TAG_PREFIX = "tag:yaml.org,2002:"


class MutationOutcome(StrEnum):
    """Whether a controlled operation changed the in-memory document."""

    NO_CHANGE = "no_change"
    CHANGED = "changed"


class EditableCloudflaredConfig:
    """A round-trip document with no generic path-based mutation surface."""

    __slots__ = ("_document", "_yaml", "_changed")

    def __init__(self, document: CommentedMap, yaml: YAML) -> None:
        self._document = document
        self._yaml = yaml
        self._changed = False

    @classmethod
    def from_snapshot(
        cls,
        snapshot: ConfigSourceSnapshot,
    ) -> EditableCloudflaredConfig:
        """Decode and load one supported UTF-8 YAML document in round-trip mode."""

        try:
            contents = snapshot.original_bytes.decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise RoundTripYamlError(
                "The source configuration is not valid UTF-8."
            ) from error

        yaml = YAML(typ="rt")
        yaml.allow_duplicate_keys = False
        yaml.preserve_quotes = True
        yaml.max_depth = _MAX_YAML_DEPTH
        yaml.indent(mapping=2, sequence=4, offset=2)
        try:
            document = yaml.load(contents)
        except (YAMLError, AttributeError, TypeError, ValueError) as error:
            raise RoundTripYamlError(
                "The source configuration is not supported, well-formed YAML."
            ) from error

        if not isinstance(document, CommentedMap):
            raise UnsupportedConfigStructureError(
                "The editable configuration root must be a mapping."
            )
        _require_supported_yaml_value(document, set(), set())
        return cls(document, yaml)

    @property
    def changed(self) -> bool:
        return self._changed

    def insert_ingress_before_terminal_catch_all(
        self,
        rule: Mapping[str, Any],
    ) -> MutationOutcome:
        """Insert one non-catch-all rule immediately before the valid fallback."""

        ingress = _require_safe_ingress(self._document)
        inserted = _copy_rule(rule)
        _validate_rule(inserted, position=None, final=False, inserted=True)
        _keep_terminal_leading_comment_with_fallback(ingress, inserted)
        ingress.insert(len(ingress) - 1, inserted)
        self._changed = True
        return MutationOutcome.CHANGED

    def render_changed(self) -> bytes:
        """Serialize only a document that a controlled primitive actually changed."""

        if not self._changed:
            raise MutationRejectedError(
                "An unchanged configuration must not be rendered as a candidate."
            )
        output = io.StringIO()
        try:
            self._yaml.dump(self._document, output)
            rendered = output.getvalue().encode("utf-8", errors="strict")
        except (YAMLError, UnicodeError, AttributeError, TypeError, ValueError) as error:
            raise RoundTripYamlError(
                "The edited configuration could not be rendered safely."
            ) from error
        if len(rendered) > MAX_CLOUDFLARED_CONFIG_BYTES:
            raise MutationRejectedError(
                "The edited configuration exceeds the supported size limit."
            )
        return rendered


def _require_safe_ingress(document: CommentedMap) -> CommentedSeq:
    if "ingress" not in document:
        raise UnsupportedConfigStructureError(
            "The editable configuration must define an ingress sequence."
        )
    ingress = document["ingress"]
    if not isinstance(ingress, CommentedSeq):
        raise UnsupportedConfigStructureError(
            "The editable configuration ingress value must be a sequence."
        )
    if not ingress:
        raise UnsupportedConfigStructureError(
            "The editable configuration ingress sequence must not be empty."
        )

    for index, entry in enumerate(ingress):
        if not isinstance(entry, CommentedMap):
            raise UnsupportedConfigStructureError(
                "Every editable ingress entry must be a mapping."
            )
        _validate_rule(
            entry,
            position=index,
            final=index == len(ingress) - 1,
            inserted=False,
        )
    return ingress


def _validate_rule(
    rule: Mapping[str, Any],
    *,
    position: int | None,
    final: bool,
    inserted: bool,
) -> None:
    service = rule.get("service")
    if not isinstance(service, str) or not service.strip():
        raise UnsupportedConfigStructureError(
            "Every editable ingress entry must define a non-empty service."
        )
    for key in ("hostname", "path"):
        if key in rule and (
            not isinstance(rule[key], str) or not rule[key].strip()
        ):
            raise UnsupportedConfigStructureError(
                f"An editable ingress {key} must be a non-empty string when present."
            )

    catch_all = "hostname" not in rule and "path" not in rule
    if inserted and catch_all:
        raise MutationRejectedError(
            "The inserted ingress rule must include a hostname or path matcher."
        )
    if catch_all and not final:
        raise UnsupportedConfigStructureError(
            "A catch-all ingress entry must be the final entry."
        )
    if final and not catch_all:
        raise UnsupportedConfigStructureError(
            "The final ingress entry must be a catch-all."
        )


def _copy_rule(rule: Mapping[str, Any]) -> CommentedMap:
    if not isinstance(rule, Mapping):
        raise MutationRejectedError("The inserted ingress rule must be a mapping.")
    copied = _copy_plain_value(rule, set())
    if not isinstance(copied, CommentedMap):
        raise MutationRejectedError("The inserted ingress rule must be a mapping.")
    return copied


def _keep_terminal_leading_comment_with_fallback(
    ingress: CommentedSeq,
    inserted: CommentedMap,
) -> None:
    """Keep a standalone comment immediately above the fallback in that position."""

    if len(ingress) < 2 or not isinstance(ingress[-2], CommentedMap):
        return
    previous = ingress[-2]
    if not previous:
        return
    final_key = next(reversed(previous))
    comment_slots = previous.ca.items.get(final_key)
    if not comment_slots or len(comment_slots) < 3:
        return
    comment = comment_slots[2]
    if comment is None or not getattr(comment, "value", "").startswith("\n"):
        return
    comment_slots[2] = None
    inserted_final_key = next(reversed(inserted))
    inserted.ca.items[inserted_final_key] = [None, None, comment, None]


def _copy_plain_value(value: Any, active: set[int]) -> Any:
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise MutationRejectedError(
                "The inserted ingress rule contains a recursive value."
            )
        active.add(identity)
        copied = CommentedMap()
        for key, child in value.items():
            if not isinstance(key, str):
                raise MutationRejectedError(
                    "The inserted ingress rule contains a non-string key."
                )
            copied[key] = _copy_plain_value(child, active)
        active.remove(identity)
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in active:
            raise MutationRejectedError(
                "The inserted ingress rule contains a recursive value."
            )
        active.add(identity)
        copied = CommentedSeq(_copy_plain_value(child, active) for child in value)
        active.remove(identity)
        return copied
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise MutationRejectedError(
        "The inserted ingress rule contains an unsupported YAML value."
    )


def _require_supported_yaml_value(
    value: Any,
    active: set[int],
    visited: set[int],
) -> None:
    tag = getattr(value, "tag", None)
    if tag is not None:
        tag_value = str(tag)
        if tag_value not in {"", "None"} and not tag_value.startswith(
            _CORE_TAG_PREFIX
        ):
            raise RoundTripYamlError(
                "The source configuration contains an unsupported YAML tag."
            )

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise RoundTripYamlError(
                "The source configuration contains a recursive YAML alias."
            )
        if identity in visited:
            return
        active.add(identity)
        for key, child in value.items():
            if not isinstance(key, str):
                raise RoundTripYamlError(
                    "The source configuration contains a non-string mapping key."
                )
            _require_supported_yaml_value(child, active, visited)
        active.remove(identity)
        visited.add(identity)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in active:
            raise RoundTripYamlError(
                "The source configuration contains a recursive YAML alias."
            )
        if identity in visited:
            return
        active.add(identity)
        for child in value:
            _require_supported_yaml_value(child, active, visited)
        active.remove(identity)
        visited.add(identity)
        return
    if value is None or isinstance(
        value,
        (str, bool, int, float, dt.date, dt.datetime),
    ):
        return
    raise RoundTripYamlError(
        "The source configuration contains an unsupported YAML value."
    )
