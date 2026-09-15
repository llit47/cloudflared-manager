#!/bin/bash
set -Eeuo pipefail
IFS=$'\n\t'
PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
export PATH

if [[ ${EUID} -ne 0 ]]; then
    printf 'cfm-update must run as root.\n' >&2
    exit 1
fi

readonly manager_python='/opt/cloudflared-manager/current/.venv/bin/python'
if [[ ! -x ${manager_python} ]]; then
    printf 'The active Cloudflared Manager runtime is unavailable.\n' >&2
    exit 1
fi

exec "${manager_python}" -I -m cloudflared_manager.deployment.cli update
