#!/usr/bin/env bash
set -Eeuo pipefail

NEW_PBX="${NEW_PBX:-192.168.1.192}"
MIGDIR="/root/fusionpbx-migration"
STAMP="$(date +%Y%m%d-%H%M%S)"

if [[ ${EUID} -ne 0 ]]; then
    echo "ERROR: run this script as root."
    exit 1
fi

echo
echo "=============================================="
echo " FusionPBX Source Server Preparation"
echo "=============================================="
echo "Source PBX addresses: $(hostname -I)"
echo "Destination PBX:      ${NEW_PBX}"
echo

mkdir -p "${MIGDIR}"

echo "[1/6] Installing migration utilities..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y     rsync     openssh-server     zip

systemctl enable --now ssh

echo "[2/6] Preparing Fail2ban for the destination PBX..."

if command -v fail2ban-client >/dev/null 2>&1; then
    if fail2ban-client status sshd >/dev/null 2>&1; then
        fail2ban-client set sshd unbanip "${NEW_PBX}" >/dev/null 2>&1 || true
        fail2ban-client set sshd addignoreip "${NEW_PBX}" >/dev/null 2>&1 || true
        echo "  Destination PBX unbanned/ignored in sshd jail for this runtime."
    else
        echo "  sshd jail not present; skipping."
    fi
else
    echo "  Fail2ban not installed; skipping."
fi

echo "[3/6] Creating a fresh FusionPBX backup..."

if [[ ! -x /etc/cron.daily/fusionpbx-backup ]]; then
    echo "ERROR: /etc/cron.daily/fusionpbx-backup was not found or is not executable."
    exit 1
fi

/etc/cron.daily/fusionpbx-backup

BACKUP="$(
    find /var/backups/fusionpbx/postgresql         -maxdepth 1         -type f         -name 'fusionpbx_pgsql_*.sql'         -printf '%T@ %p\n' 2>/dev/null     | sort -nr     | head -1     | cut -d' ' -f2-
)"

if [[ -z "${BACKUP}" || ! -s "${BACKUP}" ]]; then
    echo "ERROR: no usable FusionPBX PostgreSQL backup was created."
    exit 1
fi

echo "  Latest database backup:"
ls -lh "${BACKUP}"

echo "[4/6] Recording source server information..."

INFO="${MIGDIR}/server-info-${STAMP}.txt"

{
    echo "Migration preparation: $(date)"
    echo
    echo "HOSTNAME"
    hostname
    echo
    echo "ADDRESSES"
    hostname -I
    echo
    echo "OS"
    cat /etc/os-release
    echo
    echo "FUSIONPBX"
    if [[ -d /var/www/fusionpbx/.git ]]; then
        git -C /var/www/fusionpbx config --global --add safe.directory /var/www/fusionpbx >/dev/null 2>&1 || true
        git -C /var/www/fusionpbx branch --show-current || true
        git -C /var/www/fusionpbx describe --tags --always || true
    fi
    echo
    echo "FREESWITCH"
    freeswitch -version || true
} > "${INFO}"

echo "  Wrote ${INFO}"

echo "[5/6] Checking migration source paths..."

for dir in     /var/www/fusionpbx     /etc/fusionpbx     /etc/freeswitch     /usr/share/freeswitch/scripts     /usr/share/freeswitch/sounds     /var/lib/freeswitch/storage     /var/lib/freeswitch/recordings
do
    if [[ -d "${dir}" ]]; then
        printf '  OK       %s\n' "${dir}"
    else
        printf '  MISSING  %s\n' "${dir}"
    fi
done

echo "[6/6] Checking source services..."

for service in postgresql nginx freeswitch fail2ban; do
    printf '  %-14s ' "${service}"
    systemctl is-active "${service}" 2>/dev/null || true
done

echo
echo "=============================================="
echo " SOURCE PBX READY"
echo "=============================================="
echo
echo "Do NOT stop FreeSWITCH on this server yet."
echo "Latest database backup:"
echo "  ${BACKUP}"
echo
echo "Run migrate-new-pbx.sh on the destination PBX next."
echo
