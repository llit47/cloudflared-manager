#!/bin/bash -p
set -Eeuo pipefail

if [[ ${EUID} -ne 0 || $# -ne 0 ]]; then
    printf 'Privileged helper unavailable.\n' >&2
    exit 1
fi

readonly install_root='/opt/cloudflared-manager'
readonly current_link="${install_root}/current"
if [[ ! -L ${current_link} ]]; then
    printf 'Privileged helper unavailable.\n' >&2
    exit 1
fi
current_target=$(/usr/bin/readlink --no-newline -- "${current_link}") || exit 1
if [[ ! ${current_target} =~ ^releases/([0-9a-f]{40})$ ]]; then
    printf 'Privileged helper unavailable.\n' >&2
    exit 1
fi
readonly manager_python="${install_root}/${current_target}/.venv/bin/python"
if [[ ! -x ${manager_python} ]]; then
    printf 'Privileged helper unavailable.\n' >&2
    exit 1
fi

exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
    "${manager_python}" -I -m cloudflared_manager.activation.bridge_helper
