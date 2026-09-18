# FusionPBX Migration Utilities

Utilities for moving a FusionPBX installation from an existing Debian server to a fresh FusionPBX server.

## Files

- `prepare-old-pbx.sh` — prepares the source PBX, installs required tools, creates a fresh FusionPBX backup, and records basic source-system information.
- `migrate-new-pbx.sh` — stages the migration on the destination PBX, copies FusionPBX/FreeSWITCH data, restores PostgreSQL, and leaves FreeSWITCH disabled so cutover can be done deliberately.
- `fusionpbx-trixie-firewall.sh` — configures a persistent FusionPBX firewall on Debian 13/Trixie using Debian's nftables-backed `iptables` compatibility layer.

Both migration scripts install `zip` in addition to the migration dependencies. This avoids failures on minimal Debian installs where `zip` is not installed by default.

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

- Run the scripts as `root`.
- The destination PBX must already have a fresh FusionPBX installation.
- Root SSH from the destination PBX to the source PBX must work during the migration.
- Keep the source PBX online and handling calls until the migration script finishes and the destination web UI has been verified.
- Keep the source PBX available as a rollback until inbound/outbound calls are confirmed on the destination.

## Debian 13 / Trixie firewall

The standard FusionPBX Debian installer currently has explicit iptables setup for older Debian releases but may leave a minimal Trixie installation without the intended base firewall.

Run the firewall utility from the VM/local console when possible:

```bash
chmod +x fusionpbx-trixie-firewall.sh
./fusionpbx-trixie-firewall.sh
```

The firewall script:

1. Verifies Debian 13/Trixie.
2. Backs up the existing IPv4 and IPv6 rules.
3. Installs `iptables`, `iptables-persistent`, and `netfilter-persistent`.
4. Selects Debian's `iptables-nft` / `ip6tables-nft` compatibility backend.
5. Detects the active SSH port before applying a DROP policy.
6. Preserves/normalizes FusionPBX `sip-auth-*` chains when they exist.
7. Allows the standard FusionPBX web, SIP, RTP, ICMP, and OpenVPN ports.
8. Adds the standard RTP/SIP DSCP markings.
9. Applies matching IPv6 protection rather than leaving IPv6 open.
10. Saves the rules to `/etc/iptables/rules.v4` and `rules.v6` for reboot persistence.

After applying the firewall, verify SSH and HTTPS from another machine before rebooting. Also test SIP registration and two-way RTP audio before exposing the PBX publicly.

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

## License

This repository is released under the MIT License. See `LICENSE`.

FusionPBX and FreeSWITCH are separate projects with their own licenses. This repository is not an official FusionPBX project.

## Notes

The scripts do not store passwords, API keys, SIP credentials, or SSH credentials in this repository.
