#!/bin/bash -p
set -Eeuo pipefail

if [[ ${EUID} -ne 0 || $# -ne 0 ]]; then
    printf 'Privileged mutation helper unavailable.\n' >&2
    exit 1
fi

readonly install_root='/opt/cloudflared-manager'
readonly current_link="${install_root}/current"
if [[ ! -L ${current_link} ]]; then
    printf 'Privileged mutation helper unavailable.\n' >&2
    exit 1
fi
current_target=$(/usr/bin/readlink --no-newline -- "${current_link}") || exit 1
if [[ ! ${current_target} =~ ^releases/([0-9a-f]{40})$ ]]; then
    printf 'Privileged mutation helper unavailable.\n' >&2
    exit 1
fi
readonly manager_python="${install_root}/${current_target}/.venv/bin/python"
if [[ ! -x ${manager_python} ]]; then
    printf 'Privileged mutation helper unavailable.\n' >&2
    exit 1
fi

exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
    /usr/bin/systemd-run --system --pipe --wait --quiet --collect \
    --service-type=exec \
    --property=ProtectSystem=strict \
    '--property=ReadWritePaths=/etc/cloudflared /etc/cloudflared-manager /run/cloudflared-manager' \
    --property=NoNewPrivileges=yes --property=UMask=0077 \
    '--property=CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_SETGID CAP_SETUID CAP_SYS_PTRACE' \
    --property=RuntimeMaxSec=300s \
    --property=Environment=PYTHONDONTWRITEBYTECODE=1 \
    "${manager_python}" -I -m cloudflared_manager.activation.mutation_helper
