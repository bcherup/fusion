# FusionPBX Migration Utilities

Utilities for moving a FusionPBX installation from an existing Debian server to a fresh FusionPBX server.

## Files

- `prepare-old-pbx.sh` — prepares the source PBX, installs required tools, creates a fresh FusionPBX backup, and records basic source-system information.
- `migrate-new-pbx.sh` — stages the migration on the destination PBX, copies FusionPBX/FreeSWITCH data, restores PostgreSQL, and leaves FreeSWITCH disabled so cutover can be done deliberately.

Both scripts install `zip` in addition to the migration dependencies. This avoids failures on minimal Debian installs where `zip` is not installed by default.

## Default migration addresses

The scripts default to:

- Old/source PBX: `192.168.1.4`
- New/destination PBX: `192.168.1.192`

You can override them without editing the scripts:

```bash
NEW_PBX=192.168.1.192 ./prepare-old-pbx.sh
OLD_PBX=192.168.1.4 NEW_PBX=192.168.1.192 ./migrate-new-pbx.sh
```

## Requirements

- Run both scripts as `root`.
- The destination PBX must already have a fresh FusionPBX installation.
- Root SSH from the destination PBX to the source PBX must work during the migration.
- Keep the source PBX online and handling calls until the migration script finishes and the destination web UI has been verified.
- Keep the source PBX available as a rollback until inbound/outbound calls are confirmed on the destination.

## Source PBX

Copy `prepare-old-pbx.sh` to the original PBX and run:

```bash
chmod +x prepare-old-pbx.sh
./prepare-old-pbx.sh
```

The script:

1. Installs `rsync`, `openssh-server`, and `zip`.
2. Enables SSH.
3. Attempts to unban and temporarily ignore the destination PBX in the `sshd` Fail2ban jail when available.
4. Runs the FusionPBX daily backup.
5. Records OS/FusionPBX/FreeSWITCH information.
6. Verifies the main FusionPBX/FreeSWITCH paths.

It does **not** stop FreeSWITCH on the source server.

## Destination PBX

Copy `migrate-new-pbx.sh` to the new PBX and run:

```bash
chmod +x migrate-new-pbx.sh
./migrate-new-pbx.sh
```

The script:

1. Installs `rsync`, `openssh-client`, and `zip`.
2. Makes a safety backup of the new PBX database, FusionPBX config, and firewall rules.
3. Opens one reusable SSH connection to the old PBX.
4. Creates one final source database backup.
5. Stops and disables FreeSWITCH on the destination.
6. Copies FusionPBX, FreeSWITCH configuration, voicemail/storage, recordings, scripts, sounds, and database backups.
7. Preserves the destination server's local FusionPBX database credential files.
8. Drops/recreates the destination FusionPBX public schema and restores the source PostgreSQL backup.
9. Runs FusionPBX upgrade/default/permission routines when available.
10. Starts nginx only.

The destination FreeSWITCH service intentionally remains **disabled/inactive** after staging.

## Verify before cutover

Open the destination FusionPBX web interface and verify:

- Domains
- Users
- Extensions
- Devices
- Gateways/trunks
- IVRs
- Ring groups
- Voicemail
- Recordings
- Music on hold / custom sounds

A restored user can still belong to the old FusionPBX domain. If necessary, log in with a domain-qualified username such as:

```text
admin@192.168.1.4
```

## Cutover

After the destination configuration is verified:

On the old PBX:

```bash
systemctl stop freeswitch
```

On the new PBX:

```bash
systemctl enable freeswitch
systemctl start freeswitch
fs_cli -x "status"
fs_cli -x "sofia status"
```

Then test inbound and outbound calls before retiring the old VM.

## Notes

The scripts do not store passwords, API keys, SIP credentials, or SSH credentials in this repository.
