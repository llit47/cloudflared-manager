from types import SimpleNamespace

import pytest

from cloudflared_manager.deployment import identity
from cloudflared_manager.deployment.errors import HostOperationError


def _group(gid: int = 995):
    return SimpleNamespace(gr_gid=gid, gr_mem=[])


def _user(uid: int = 995, gid: int = 995):
    return SimpleNamespace(
        pw_uid=uid,
        pw_gid=gid,
        pw_dir="/nonexistent",
        pw_shell="/usr/sbin/nologin",
    )


def _identity_state(monkeypatch, *, group=None, user=None):
    state = {"group": group, "user": user}
    commands: list[str] = []

    def get_group(name: str):
        if state["group"] is None:
            raise KeyError(name)
        return state["group"]

    def get_user(name: str):
        if state["user"] is None:
            raise KeyError(name)
        return state["user"]

    def run(program: str, arguments: tuple[str, ...]) -> None:
        commands.append(program)
        if program == "groupadd":
            state["group"] = _group()
        elif program == "useradd":
            state["user"] = _user()

    monkeypatch.setattr(identity.grp, "getgrnam", get_group)
    monkeypatch.setattr(identity.pwd, "getpwnam", get_user)
    monkeypatch.setattr(identity, "_run_account_command", run)
    monkeypatch.setattr(identity, "_nologin_shell", lambda: "/usr/sbin/nologin")
    monkeypatch.setattr(identity.os, "getgrouplist", lambda name, gid: [gid])
    return state, commands


def test_existing_dedicated_system_identity_is_accepted(monkeypatch) -> None:
    _, commands = _identity_state(monkeypatch, group=_group(), user=_user())

    identity.ensure_service_identity()

    assert commands == []


def test_missing_group_is_created_before_validation(monkeypatch) -> None:
    state, commands = _identity_state(monkeypatch, group=None, user=_user())

    identity.ensure_service_identity()

    assert commands == ["groupadd"]
    assert state["group"] is not None


def test_missing_user_with_existing_group_is_created(monkeypatch) -> None:
    state, commands = _identity_state(monkeypatch, group=_group(), user=None)

    identity.ensure_service_identity()

    assert commands == ["useradd"]
    assert state["user"] is not None


def test_missing_group_and_user_are_created_in_order(monkeypatch) -> None:
    _, commands = _identity_state(monkeypatch, group=None, user=None)

    identity.ensure_service_identity()

    assert commands == ["groupadd", "useradd"]


@pytest.mark.parametrize("failed_program", ["groupadd", "useradd"])
def test_account_command_failure_is_sanitized(monkeypatch, failed_program: str) -> None:
    group = None if failed_program == "groupadd" else _group()
    user = _user() if failed_program == "groupadd" else None
    _identity_state(monkeypatch, group=group, user=user)

    def fail(program: str, arguments: tuple[str, ...]) -> None:
        raise HostOperationError("The dedicated service identity could not be created.")

    monkeypatch.setattr(identity, "_run_account_command", fail)

    with pytest.raises(HostOperationError, match="could not be created"):
        identity.ensure_service_identity()


@pytest.mark.parametrize("missing_record", ["group", "user"])
def test_created_identity_record_must_be_retrievable(monkeypatch, missing_record: str) -> None:
    group = None if missing_record == "group" else _group()
    user = None if missing_record == "user" else _user()
    _, commands = _identity_state(monkeypatch, group=group, user=user)
    monkeypatch.setattr(
        identity,
        "_run_account_command",
        lambda program, arguments: commands.append(program),
    )

    with pytest.raises(HostOperationError, match="was not created"):
        identity.ensure_service_identity()


def test_existing_identity_with_privileged_membership_is_rejected(monkeypatch) -> None:
    _identity_state(monkeypatch, group=_group(), user=_user())
    monkeypatch.setattr(identity.os, "getgrouplist", lambda name, gid: [gid, 27])

    with pytest.raises(HostOperationError, match="supplementary"):
        identity.ensure_service_identity()


def test_non_root_user_with_root_primary_group_is_rejected(monkeypatch) -> None:
    _identity_state(monkeypatch, group=_group(0), user=_user(uid=995, gid=0))

    with pytest.raises(HostOperationError, match="unsafe"):
        identity.ensure_service_identity()


def test_root_uid_is_rejected_even_with_non_root_primary_group(monkeypatch) -> None:
    _identity_state(monkeypatch, group=_group(), user=_user(uid=0, gid=995))

    with pytest.raises(HostOperationError, match="unsafe"):
        identity.ensure_service_identity()


def test_unexpected_primary_group_is_rejected(monkeypatch) -> None:
    _identity_state(monkeypatch, group=_group(), user=_user(uid=995, gid=994))

    with pytest.raises(HostOperationError, match="unsafe"):
        identity.ensure_service_identity()


def test_existing_non_system_uid_is_rejected(monkeypatch) -> None:
    _identity_state(monkeypatch, group=_group(1000), user=_user(uid=1000, gid=1000))

    with pytest.raises(HostOperationError, match="unsafe"):
        identity.ensure_service_identity()
