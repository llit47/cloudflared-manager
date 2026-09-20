#!/bin/bash
set -Eeuo pipefail
IFS=$'\n\t'
PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
export PATH

if [[ ${EUID} -ne 0 ]]; then
    printf 'cfm-update must run as root.\n' >&2
    exit 1
fi

readonly install_root='/opt/cloudflared-manager'
readonly current_link="${install_root}/current"
if [[ ! -L ${current_link} ]]; then
    printf 'The active Cloudflared Manager release is unavailable.\n' >&2
    exit 1
fi

current_target=''
if ! current_target=$(readlink --no-newline -- "${current_link}" && printf '.'); then
    printf 'The active Cloudflared Manager release is unavailable.\n' >&2
    exit 1
fi
current_target=${current_target%.}
readonly current_target
if [[ ! ${current_target} =~ ^releases/([0-9a-f]{40})$ ]]; then
    printf 'The active Cloudflared Manager release is invalid.\n' >&2
    exit 1
fi

readonly release_sha=${BASH_REMATCH[1]}
readonly manager_python="${install_root}/releases/${release_sha}/.venv/bin/python"
if [[ ! -x ${manager_python} ]]; then
    printf 'The active Cloudflared Manager runtime is unavailable.\n' >&2
    exit 1
fi

exec "${manager_python}" -I -m cloudflared_manager.deployment.cli update
