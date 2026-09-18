#!/usr/bin/env bash
set -Eeuo pipefail

OLD_PBX="${OLD_PBX:-192.168.1.4}"
NEW_PBX="${NEW_PBX:-192.168.1.192}"

STAMP="$(date +%Y%m%d-%H%M%S)"
SAFE="/root/pre-migration-${STAMP}"
SOCK="/tmp/fusionpbx-migration-ssh.sock"

cleanup() {
    ssh -S "${SOCK}" -O exit root@"${OLD_PBX}" >/dev/null 2>&1 || true
    rm -f "${SOCK}"
}
trap cleanup EXIT

if [[ ${EUID} -ne 0 ]]; then
    echo "ERROR: run this script as root."
    exit 1
fi

echo
echo "=================================================="
echo " FusionPBX Migration"
echo "=================================================="
echo "Source:      ${OLD_PBX}"
echo "Destination: ${NEW_PBX}"
echo
echo "This script restores the source PBX data onto this"
echo "server. FreeSWITCH on the destination will remain"
echo "disabled after staging so cutover can be deliberate."
echo

read -r -p "Type MIGRATE to continue: " CONFIRM

if [[ "${CONFIRM}" != "MIGRATE" ]]; then
    echo "Cancelled."
    exit 1
fi

echo "[1/10] Installing migration utilities..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y     rsync     openssh-client     zip

echo "[2/10] Creating destination safety backup..."
mkdir -p "${SAFE}"

cp -a /etc/fusionpbx "${SAFE}/etc-fusionpbx"

runuser -u postgres -- pg_dump -Fc fusionpbx     > "${SAFE}/fusionpbx-before-migration.dump"

iptables-save > "${SAFE}/iptables.rules"
ip6tables-save > "${SAFE}/ip6tables.rules"

echo "  Safety backup: ${SAFE}"

echo "[3/10] Connecting to source PBX..."
echo "  Enter the source PBX root password when prompted."

rm -f "${SOCK}"

ssh     -M     -S "${SOCK}"     -o ControlPersist=900     -o StrictHostKeyChecking=accept-new     -fnNT     root@"${OLD_PBX}"

ssh -S "${SOCK}" root@"${OLD_PBX}"     'echo "Connected to source PBX: $(hostname) $(hostname -I)"'

echo "[4/10] Creating final source database backup..."

ssh -S "${SOCK}" root@"${OLD_PBX}"     '/etc/cron.daily/fusionpbx-backup'

ssh -S "${SOCK}" root@"${OLD_PBX}"     'ls -lh "$(find /var/backups/fusionpbx/postgresql -maxdepth 1 -type f -name "fusionpbx_pgsql_*.sql" -printf "%T@ %p\n" | sort -nr | head -1 | cut -d" " -f2-)"'

echo "[5/10] Stopping destination telephony services..."

systemctl disable --now freeswitch
systemctl stop nginx || true

echo "[6/10] Copying FusionPBX and FreeSWITCH data..."

mkdir -p /var/backups/fusionpbx/postgresql

RSYNC_SSH="ssh -S ${SOCK}"

echo "  Database backups..."
rsync -aH     -e "${RSYNC_SSH}"     root@"${OLD_PBX}":/var/backups/fusionpbx/postgresql/     /var/backups/fusionpbx/postgresql/

echo "  FusionPBX web tree..."
rsync -aHAX     -e "${RSYNC_SSH}"     root@"${OLD_PBX}":/var/www/fusionpbx/     /var/www/fusionpbx/

echo "  FusionPBX system configuration..."
rsync -aHAX     --exclude='config.conf'     --exclude='config.php'     --exclude='config.lua'     -e "${RSYNC_SSH}"     root@"${OLD_PBX}":/etc/fusionpbx/     /etc/fusionpbx/

echo "  FreeSWITCH configuration..."
rsync -aHAX     -e "${RSYNC_SSH}"     root@"${OLD_PBX}":/etc/freeswitch/     /etc/freeswitch/

echo "  FreeSWITCH scripts..."
rsync -aHAX     --exclude='resources/functions/config.lua'     -e "${RSYNC_SSH}"     root@"${OLD_PBX}":/usr/share/freeswitch/scripts/     /usr/share/freeswitch/scripts/

echo "  Voicemail / storage..."
rsync -aHAX     -e "${RSYNC_SSH}"     root@"${OLD_PBX}":/var/lib/freeswitch/storage/     /var/lib/freeswitch/storage/

echo "  Recordings..."
rsync -aHAX     -e "${RSYNC_SSH}"     root@"${OLD_PBX}":/var/lib/freeswitch/recordings/     /var/lib/freeswitch/recordings/

echo "  Sounds / music on hold..."
rsync -aHAX     -e "${RSYNC_SSH}"     root@"${OLD_PBX}":/usr/share/freeswitch/sounds/     /usr/share/freeswitch/sounds/

echo "[7/10] Copying certificate data when present..."

if ssh -S "${SOCK}" root@"${OLD_PBX}" 'test -d /etc/dehydrated'; then
    mkdir -p /etc/dehydrated
    rsync -aHAX         -e "${RSYNC_SSH}"         root@"${OLD_PBX}":/etc/dehydrated/         /etc/dehydrated/
else
    echo "  No /etc/dehydrated directory on source; skipping."
fi

echo "[8/10] Restoring FusionPBX database..."

BACKUP="$(
    find /var/backups/fusionpbx/postgresql         -maxdepth 1         -type f         -name 'fusionpbx_pgsql_*.sql'         -printf '%T@ %p\n'     | sort -nr     | head -1     | cut -d' ' -f2-
)"

if [[ -z "${BACKUP}" || ! -s "${BACKUP}" ]]; then
    echo "ERROR: no usable database backup found."
    exit 1
fi

echo "  Restoring:"
ls -lh "${BACKUP}"

runuser -u postgres --     psql -v ON_ERROR_STOP=1 -d fusionpbx     -c 'DROP SCHEMA public CASCADE;'

runuser -u postgres --     psql -v ON_ERROR_STOP=1 -d fusionpbx     -c 'CREATE SCHEMA public AUTHORIZATION fusionpbx;'

set +e
runuser -u postgres --     pg_restore     -v     -Fc     --no-owner     --role=fusionpbx     -d fusionpbx     "${BACKUP}"
PGRESTORE_RC=$?
set -e

if [[ ${PGRESTORE_RC} -ne 0 ]]; then
    echo
    echo "WARNING: pg_restore returned exit code ${PGRESTORE_RC}."
    echo "Review the restore output above. A single non-fatal object/ACL"
    echo "warning can occur, but verify the restored database before cutover."
fi

echo "[9/10] Applying ownership and FusionPBX updates..."

chown -R www-data:www-data /var/www/fusionpbx

if [[ -d /usr/share/freeswitch/scripts ]]; then
    chown -R www-data:www-data /usr/share/freeswitch/scripts
fi

cd /var/www/fusionpbx

if [[ -f core/upgrade/upgrade.php ]]; then
    php core/upgrade/upgrade.php || true
    php core/upgrade/upgrade.php --permissions || true
    php core/upgrade/upgrade.php --defaults || true
fi

echo "[10/10] Starting destination web interface only..."

systemctl enable nginx
systemctl start nginx

echo
echo "=================================================="
echo " MIGRATION STAGING COMPLETE"
echo "=================================================="
echo
echo "Destination web UI:"
echo "  https://${NEW_PBX}"
echo
echo "FreeSWITCH state:"
systemctl is-enabled freeswitch 2>/dev/null || true
systemctl is-active freeswitch 2>/dev/null || true
echo
echo "Expected during staging:"
echo "  freeswitch = disabled / inactive"
echo
echo "The source PBX should still be handling calls."
echo
echo "Safety backup:"
echo "  ${SAFE}"
echo
echo "Verify the destination GUI before cutover."
echo
