#!/bin/bash
set -Eeuo pipefail
IFS=$'\n\t'
PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
export PATH
unset PYTHONHOME PYTHONPATH

readonly REPOSITORY='llit47/cloudflared-manager'
readonly MAIN_API_URL="https://api.github.com/repos/${REPOSITORY}/commits/main"

fail() {
    printf 'Installation failed: %s\n' "$1" >&2
    exit 1
}

[[ ${EUID} -eq 0 ]] || fail 'run this installer as root (for example, pipe it to sudo bash)'
[[ $(uname -s) == 'Linux' ]] || fail 'Linux is required'
[[ -d /run/systemd/system ]] || fail 'a running systemd system manager is required'

curl_path=$(command -v curl) || fail 'curl is required'
command -v systemctl >/dev/null || fail 'systemctl is required'
mktemp_path=$(command -v mktemp) || fail 'mktemp is required'
rm_path=$(command -v rm) || fail 'rm is required'
readonly curl_path mktemp_path rm_path

python_path=''
for candidate in /usr/bin/python3.13 /usr/bin/python3.12 /usr/local/bin/python3.13 /usr/local/bin/python3.12; do
    if [[ -x ${candidate} ]] && "${candidate}" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
        python_path=${candidate}
        break
    fi
done
if [[ -z ${python_path} ]]; then
    candidate=$(command -v python3 || true)
    if [[ -n ${candidate} ]] && "${candidate}" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
        python_path=${candidate}
    fi
fi
[[ -n ${python_path} ]] || fail 'Python 3.12 or newer is required; install it from your configured distribution repositories'
"${python_path}" -I -m venv --help >/dev/null 2>&1 || fail 'the selected Python lacks venv support; install the matching distribution venv package'

temporary_dir=$(${mktemp_path} -d -- /tmp/cloudflared-manager-install.XXXXXXXX) || fail 'could not create a temporary directory'
cleanup() {
    if [[ -n ${temporary_dir:-} && ${temporary_dir} == /tmp/cloudflared-manager-install.* ]]; then
        "${rm_path}" -rf -- "${temporary_dir}"
    fi
}
trap cleanup EXIT

revision_json="${temporary_dir}/revision.json"
"${curl_path}" --disable --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 60 \
    --retry 3 --retry-all-errors --output "${revision_json}" -- "${MAIN_API_URL}" \
    || fail 'could not resolve the current main revision'
sha=$("${python_path}" -I -c 'import json, re, sys; from pathlib import Path; raw=Path(sys.argv[1]).read_bytes(); value=json.loads(raw).get("sha", "") if len(raw) <= 1048576 else ""; print(value) if re.fullmatch(r"[0-9a-f]{40}", value) else sys.exit(1)' "${revision_json}") \
    || fail 'GitHub returned an invalid main revision'
readonly sha

bootstrap="${temporary_dir}/bootstrap.py"
bootstrap_url="https://raw.githubusercontent.com/${REPOSITORY}/${sha}/src/cloudflared_manager/deployment/bootstrap.py"
"${curl_path}" --disable --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 60 \
    --retry 3 --retry-all-errors --output "${bootstrap}" -- "${bootstrap_url}" \
    || fail 'could not download the exact-revision deployment bootstrap'
chmod 0700 -- "${bootstrap}"

"${python_path}" -I "${bootstrap}" --sha "${sha}" --python "${python_path}"
