# PBX Toolkit

A neutral command-line installer and configurator for **Debian 13 Trixie**, FusionPBX, and FreeSWITCH. Run `python3 pbxctl.py` for the menu, or use explicit commands for repeatable administration.

This is a **review release**. Offline safety/behavior and adapter HTTP tests are included, along with Debian/PostgreSQL CI. A complete new-VM installation, real carrier call testing, real SMTP delivery, remote storage, and a replacement-server recovery drill remain required before treating every path as production-proven. The toolkit has not been applied to an existing live PBX during development.

## Operations

| Command | Purpose |
|---|---|
| `setup` | Create/edit public site configuration interactively |
| `plan` | Show available and selected modules without modifying the host |
| `install` | Run a pinned official base installer on a fresh Debian 13 host, then optionally configure modules |
| `deploy` | Install only this toolkit beside an existing PBX |
| `configure` | Apply selected independent modules with scoped recovery records |
| `feature` | Enable/disable one feature or adjust its volume settings |
| `restore-summary-text` | Restore unchanged generated voicemail notes to their saved original transcripts |
| `backup` / `verify-backup` | Create a full private recovery set; check hashes and optionally perform an isolated PostgreSQL restore |
| `migrate` / `restore` / `activate` | Transfer, stage, and deliberately activate a replacement PBX |
| `check` / `check-media` | Inspect local health or one active call's SRTP confirmation |
| `firewall` | Apply/confirm an ingress policy with a ten-minute rollback timer |
| `update --target pbx` | Review or perform fast-forward application updates on existing tracking branches |
| `update --target tool` | Verify/install a separately downloaded toolkit release |
| `retention` | Explicitly apply the configured remote retention policy |
| `rollback` | Restore a current module's captured configuration, refusing later edits |

Mutating installation/configuration/recovery/update actions require `--apply`. `smtp-credential` explicitly writes a privately entered credential, and `setup` explicitly saves the requested configuration file. Check/help/plan do not apply server configuration. `update --fetch` refreshes Git tracking data without changing working files.

## Start here

Use a downloaded release containing `MANIFEST.json`, or generate it from a trusted checkout with `python3 tools/package.py`. A checksum manifest detects damaged or changed files; it is not a signature establishing the source's identity.

```sh
python3 pbxctl.py setup --config site.json
python3 pbxctl.py plan --config site.json --modules backup,audio
```

Review domain, LAN address, management networks, certificate email, and verified carrier addresses. Examples contain no real credentials. `site.json` is public configuration and is ignored by Git; secrets belong under `/etc/pbxctl/secrets`, root-owned and mode 600.

For a **fresh machine**, run as root:

```sh
python3 pbxctl.py install --config site.json --apply
```

The base uses the official installer pinned to `eddc685f83fbe9bdf8eb050c80e50d8f51e505e1`. Its downstream package/repository dependencies are not a reproducible OS image. It performs package installation/updates and can take a long time while compiling. Initial credentials/output stay in `/var/lib/pbxctl/base-install.log`, root-only. An existing PBX or PostgreSQL cluster is refused. The resulting base is not declared externally hardened until the chosen security/firewall modules have been configured and tested.

For an **existing PBX**, deploy the toolkit only:

```sh
python3 pbxctl.py deploy --config site.json --apply
```

Installed code is `/opt/pbxctl`; configuration is `/etc/pbxctl/site.json`. Deployment does not rotate phone credentials or create extensions/ring groups. After base installation, create/test your domain, mailbox, carrier gateway, and provider ACL in the PBX as appropriate. Modules validate the objects they require.

## Choose features

```sh
pbxctl configure --modules backup,audio
pbxctl configure --modules backup,audio --apply --allow-restart
```

| Module | Configuration |
|---|---|
| `backup` | Private daily database/configuration/media/runtime recovery sets |
| `tls` | Deploy an existing trusted certificate and renewal hook |
| `hardening` | Authenticated SIP, NAT hostname, TLS profile, exact carrier ACL, failed-auth jail |
| `audio` | Opus → G.722 → G.711 on the internal profile |
| `secure-calling` | Offer or require SRTP on calls to selected phones/groups |
| `carrier-tls` | TLS signaling and required outbound SRTP on one credential-registration trunk |
| `hold-music` | Adjust a selected music folder in dB; preserve and restore originals |
| `call-volume` | Separate phone microphone/listening gain on selected call legs |
| `transcription` | Optional local English Whisper model, external adapter, and new-voicemail worker |
| `ai-summary` | Optional local voicemail summaries, quoted follow-up requests and callback details |
| `smtp` | Generic SMTP transport and the selected mailbox email recipient |
| `alerts` | Local health checks and SMTP change/recovery notifications |
| `offsite` | Optional encrypted SFTP/S3 backup copying after a successful local backup |

Repeated unchanged module configuration is skipped. Later edits to managed files/database fields stop reconfiguration for review. Unselected module settings must not change implicitly. Credentials, devices, ring groups, and incoming routing are not automatically recreated. On an installed host, `pbxctl setup` saves a candidate at `/root/pbxctl-site.json`; apply its selected changes with `pbxctl configure --config /root/pbxctl-site.json --modules ... --apply`. Setup does not overwrite the active site file.

For a new certificate, `certificate --apply --agree-acme-tos` supports Cloudflare DNS validation using the configured private credentials file. Other DNS providers can obtain a certificate with Certbot separately; `tls` uses the existing lineage under `/etc/letsencrypt/live/DOMAIN`. Apply `tls` before `hardening`. Configure carrier addresses and the external profile's provider ACL before hardening. Firewall application is a separate step.

### Enable, disable, or adjust a feature

Version 0.3 adds a `feature` menu and command. Older site files receive disabled defaults for the new modules. First use `setup` to select the desired extensions, music folder/stream, and optional trunk. Apply that candidate using `configure --config /root/pbxctl-site.json --modules ... --apply`. Subsequent feature commands use the active configuration by default:

```sh
pbxctl feature --name secure-calling --enable
pbxctl feature --name secure-calling --enable --apply
pbxctl feature --name hold-music --enable --gain-db -8 --apply
pbxctl feature --name call-volume --enable --read-level 0 --write-level -1 --apply
pbxctl feature --name ai-summary --enable --apply
pbxctl feature --name ai-summary --disable --apply
```

Omit `--apply` to preview. Toggle commands save successful changes to `/etc/pbxctl/site.json`; a supplied candidate file remains unchanged. New call rules live in tenant-scoped, toolkit-owned dialplans. They do not edit the application's tracked source. Failed configuration attempts restore their recorded changes; later external edits stop automatic reconfiguration for review.

### Phone and carrier encryption

`secure_calling.destinations` lists phone extensions or ring groups (defaults: `1000`, `600`). After trusted TLS is working, enable `secure-calling` to export an SDES SRTP offer to those destinations. `mode: optional` supports mixed phones but **permits unencrypted RTP fallback**. `mandatory` refuses peers that cannot negotiate the selected SRTP suite. This rule controls PBX-originated phone legs; configure each phone for TLS and required outgoing SDES too. TLS signaling and SRTP audio must both be tested. Disabling removes the toolkit's offer rule from new calls; it leaves certificates, TLS listeners, and other media policies intact.

`carrier-tls` is separate. It requires a dedicated external profile with exactly one enabled credential-registration gateway, an existing TLS certificate, verified provider `/32` addresses, and one tenant outbound route with a single direct gateway bridge. Select `gateway_uuid` and `route_uuid` from the PBX records. The initial provider example is `sip.telnyx.com:5061`, with a local TLS listener on `5081`. Prepare the provider portal's encrypted-media policy and inbound TLS routing, then set `carrier_tls.portal_ready` to `true`.

```sh
pbxctl feature --name carrier-tls --enable --apply --allow-restart
pbxctl feature --name carrier-tls --disable --apply --allow-restart
```

Activation checks the remote certificate's trust and hostname, configures a TLS-only external listener, and requires outbound SRTP on the selected route. It restarts only the external profile when no other selected module requires a full restart, checks for zero active calls, and rolls back if TLS registration fails. Disable restores the selected gateway/profile/route fields captured before first enable. Adjust the provider portal to match. Other trunk authentication types require manual configuration in this release.

Firewall and router changes remain explicit. With this option enabled, `firewall` generates provider-only TCP rules for the selected TLS listener. An already-installed guard must be rolled back/reapplied deliberately using its saved transaction; feature activation does not replace an existing firewall. Ensure required inbound routing works before the switch. **Registered over TLS does not prove encrypted call audio**: use `check-media` on incoming and outgoing carrier legs. PSTN calls are not end-to-end encrypted by this setting.

### Music and call volume

For music, select one directory beneath `/usr/share/freeswitch/sounds/music` and its existing local-stream name, without the `local_stream://` prefix or sample-rate suffix. For example, directory `/usr/share/freeswitch/sounds/music/voip.example.com/default` and stream `voip.example.com/default`. Verify the actual directory and name in your installation before using them. All WAV variants below that folder are adjusted together: PCM16, mono/stereo, 8/16/32/48 kHz, at most 64 MiB per file. Gain ranges from -30 to +6 dB; clipping is refused.

The first apply privately preserves each original. Every later adjustment starts from that baseline, so repeated `-8` settings do not make the music progressively quieter. `--disable` restores the exact original bytes and refreshes the streams. Changes to the track set or externally edited files require a baseline review; the tool will not overwrite them. Shared music folders affect every tenant that uses those files. The toolkit does not import tracks or change music licensing.

Call volume uses FreeSWITCH gain **steps**, from -4 to +4, rather than dB. `read_level` changes audio received from the selected phone; `write_level` changes audio sent to it. Zero is neutral. `call_volume.extensions` selects originating phone numbers and `destinations` selects answered extensions/groups. A group setting applies to its resulting call legs, including any external destinations in that group. Listening gain also affects prompts/music sent on that leg. Adjust music with `hold-music` when only the music is too loud.

```sh
pbxctl feature --name hold-music --disable --apply
pbxctl feature --name call-volume --disable --apply
```

Call-gain changes affect new calls. Disable turns off the toolkit rules without changing existing calls or the device's own volume settings. Positive gain can distort already-loud speech; start with one step and test both directions.

### Optional local transcription

Skip `transcription` to avoid Whisper/model installation and processing. Add it at any time after choosing an existing mailbox:

```sh
pbxctl configure --modules transcription --apply
```

Requires an existing mailbox, filesystem voicemail, suitable disk/RAM, and a compatible Transcribe interface. If absent, the official Transcribe app is installed at the reviewed revision and its schema/defaults are applied. Whisper source/model pins and resource limits are in `lib/services.py`. The custom adapter lives under `/opt/pbxctl/assets/app`, linked into the application's loader; vendor adapter files remain unchanged.

To disable, set `transcription_enabled` to `false` in a candidate configuration and run Configure for that module. Re-enabling preserves the existing cutoff/retry state. Originals and existing transcripts are retained. Existing differently managed custom Whisper workers are detected and refused rather than overwritten; those installations need an explicit migration of ownership/state.

The shortcut is `pbxctl feature --name transcription --enable --apply`, or `--disable`. Disable summaries first if they are enabled.

### Optional local voicemail summaries

Enable toolkit-managed transcription first, then `ai-summary`. Debian 13 **amd64** is supported by the pinned CPU runtime. Plan for at least 4 GiB system RAM and 3 GiB free installation space, plus voicemail and backup storage. Installation downloads checksum-pinned llama.cpp `b10964` and Qwen2.5-1.5B-Instruct Q4_K_M; this uses local CPU and has no paid API dependency. There are no model downloads if the module is skipped.

The authenticated model service listens only on loopback. Separate systemd services limit CPU/RAM; the summary worker waits for idle calls and shares an inference lock with the background transcription worker. Defaults cap model use at half of one core and 1500 MiB. The worker stops its request if phone activity begins. Direct on-demand transcription through the PBX interface is outside this background-worker lock.

New opted-in voicemail in the configured tenant receives labeled notes above the exact original transcript. Follow-up and contact quotes must match the transcript. Model output can be wrong; the recording and original text remain available. The worker cannot place calls, send messages, or act on instructions contained in a voicemail. It processes one message per run, preserves concurrent manual edits, bounds retries, and reports repeated failures through `check`. This is asynchronous mailbox text; it does not hold an already-sent voicemail email waiting for a summary.

Disable stops the summary timer, worker, and model. It retains models, recordings, transcripts, existing notes, and progress so re-enabling does not redownload everything. To remove unchanged generated notes from existing messages:

```sh
pbxctl feature --name ai-summary --disable --apply
pbxctl restore-summary-text --apply
```

Restoration uses compare-and-set updates and preserves manual edits. Re-enabling may summarize restored messages again. Private original-text copies are cleaned up by the enabled worker after the corresponding voicemail is deleted; disabled workers do not perform cleanup. Backups include these private copies and models. An existing standalone AI deployment is refused to avoid duplicate workers; migrate its units/state deliberately rather than running the fresh-install module over it.

Application updates run a read-only schema/interface check while services are stopped. Custom source remains under `/opt/pbxctl`, runtime under `/opt/pbxctl-ai`, and configuration/progress under `/etc/pbxctl` and `/var/lib/pbxctl`. Future upstream interface changes can still require an adapter update; keeping vendor Git clean prevents local-edit conflicts, not every possible compatibility issue.

### Any compatible SMTP provider

`setup` prompts for hostname, port, STARTTLS / implicit TLS / authorized unencrypted relay, authentication, sender, and recipient. Password authentication requires TLS. Certificate validation remains enabled. Examples:

- Typical third-party SMTP: port 587, `security: starttls`.
- Implicit TLS: commonly port 465, `security: tls`.
- Authorized relay: `auth: false`; follow the provider's access restrictions.

```sh
pbxctl smtp-credential
pbxctl configure --modules smtp,alerts --apply
pbxctl test-email --apply
```

Enter the provider SMTP password/app password at the hidden prompt. It is not passed in command arguments or printed. PBX SMTP settings are stored in its protected database; independent health alerts use the private file. Confirm the test email arrived. SMTP acceptance alone is not inbox delivery.

Google Workspace with a dynamic IP can use `smtp.gmail.com:587` and an app password when permitted by the account's 2-Step Verification/admin policy. Providers requiring OAuth-only authentication need an OAuth-capable relay or a future authentication adapter; this release does not implement OAuth token flows. An ordinary Google/Microsoft sign-in password is not universally usable for SMTP.

### Off-server backups now or later

Keep `remote_backup.enabled` false to defer setup. Local backup continues independently. Later select an SFTP repository such as `sftp:backup@host:/repository`, or an HTTPS S3-compatible repository such as `s3:https://storage.example.com/bucket`. SFTP uses a preconfigured SSH key and verified host key; no root login is required on the storage server.

Save the enabled repository settings in a candidate configuration (for example `/root/offsite.json`). For S3, the optional one-line JSON credential file accepts `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN`, and `AWS_DEFAULT_REGION`; enter these privately in the configured root-only file on the server.

```sh
pbxctl offsite-init --config /root/offsite.json --apply
pbxctl configure --config /root/offsite.json --modules offsite --apply
pbxctl backup --apply
pbxctl offsite-restore --destination /root/recovery-test --apply
```

Initialization installs Restic and prompts privately for a missing encryption password. Initialize only a new repository; for an existing repository, create its private password file and skip initialization. Configuration verifies repository access before enabling unattended uploads. Retain the encryption password separately for disaster recovery. Verify downloaded recovery sets with `verify-backup --archive PATH --apply`.

Retention is opt-in and independent of local storage. Counts default to zero (no automatic deletion). `retention --apply` applies the chosen daily/weekly/monthly policy and prunes tagged remote snapshots. Local archives are preserved; monitor space and choose local cleanup separately. Backups stop below the configured free-space reserve. Destination storage may have its own charges.

## Firewall

Management is restricted to configured networks. Public IPv4 signaling is TCP 5061; media uses the configured RTP range; carrier trunk signaling is restricted to explicit addresses. IPv6 SIP/media is LAN-only. Existing ban chains remain in the traffic path. Fresh hosts with no unrelated INPUT rules can receive a matching base policy; unknown existing topologies are refused.

```sh
pbxctl firewall --apply
# Open a NEW SSH transport from an allowed management network:
pbxctl firewall --confirm /root/pbxctl-backups/PRINTED-TRANSACTION
```

Confirmation must occur within ten minutes. Reusing a multiplexed SSH channel is not a new transport. Do not reboot before confirmation: the transient rollback timer does not survive a reboot. An existing toolkit guard must be checked or deliberately rolled back before changing its policy. The current SSH port must be 22 for this release.

To undo a confirmed policy, use `pbxctl firewall --archive EXACT-TRANSACTION --apply`. Manual rollback checks that the installed policy and persistent files still match the transaction, restores the previous policy, and keeps current live ban chains.

## Backup, migration, and restore

Recovery sets are private directories under `/var/backups/pbxctl` containing `database.dump`, `files.tar.gz`, and `manifest.json`. These contain secrets and media. Only transfer trusted, verified recovery sets.

```sh
pbxctl backup --apply
pbxctl verify-backup --archive /var/backups/pbxctl/EXACT-ID --apply
```

The second command verifies hashes and restores into an isolated random database, then removes only that temporary database. Backups include custom modules, runtime/model, configuration/state, TLS material, PBX data, and media. Media is copied live; use a quiesced final backup for migration cutover.

On a replacement host with a matching base installation, deploy this toolkit and configure the same SIP domain/layout with the replacement LAN address. The source also needs this toolkit deployed for the migration command:

```sh
pbxctl migrate --ssh-host SOURCE --ssh-user ADMIN --apply
pbxctl restore --archive /var/backups/pbxctl/IMPORTED-ID
pbxctl restore --archive /var/backups/pbxctl/IMPORTED-ID --apply
```

SSH uses key authentication and strict host-key verification. A non-root account requires working noninteractive sudo for the selected backup/rsync commands. Migration transfers the exact fresh recovery set, not the newest file in a mixed legacy directory. Legacy `.sql` backup discovery is not used.

Restore validates archive paths/hashes, tests a scratch database restore, checks architecture/runtime/layout, makes a destination backup, and preserves destination database credentials, firewall, and host identity. It stages the replacement application and data with services blocked from starting, including after a reboot. The previous application directory is retained beside the new one. Restore failures stop; they are never reported as successful staging.

After stopping the source and completing the final sync:

```sh
pbxctl activate --source-stopped --apply
pbxctl check
```

Update router/DNS/addressing as appropriate, then test registration, carrier calls, voicemail, and transcription. Existing custom units/runtime dependencies need review on the replacement host. An isolated database restore test does not prove a full-machine recovery.

## Updates

```sh
pbxctl update --target pbx --fetch
pbxctl update --target pbx --apply --allow-restart
```

PBX updates keep the current tracking branches, require clean tracked files, reject local commits/divergence, use fast-forward merges, back up, stop services, apply schema/default updates, and check the custom adapter. The tool never uses `git reset --hard`, hides edits in a stash, switches release branches, or performs a distribution upgrade. Optional application repositories are checked independently.

On failure, services remain stopped and an update marker blocks boot activation. The journal and backup location are recorded under `/var/lib/pbxctl`. Recover from the full backup or resolve the failed update deliberately; do not remove the guard merely to suppress the failure. Schema changes cannot safely be undone by reverting source alone. A successful update still needs real call and voicemail tests.

Toolkit updates use a separately downloaded trusted release:

```sh
pbxctl update --target tool --source /root/new-release
pbxctl update --target tool --source /root/new-release --apply
```

The installed release must still match its manifest. The candidate is verified in a staging directory before swapping the code directory; the previous release is retained privately under `/opt/.pbxctl-before-ID`. Site settings, secrets, and transcription progress are outside the code release. No background self-update is scheduled.

## Check audio encryption

Configure the test phone for TLS and SDES Required on outgoing calls, then call `*9196` and keep it connected. Obtain that call's UUID locally with `fs_cli -x 'show channels as json'`, then:

```sh
pbxctl check-media --uuid CALL_UUID
```

The check reports actual audio-security confirmation and negotiated cipher without dumping key-bearing SDP. Test each call leg, incoming foreground, and push-woken calls separately. Acrobits documents SDES as disabled for pushed calls; do not require it globally before verifying the required call behavior. A phone-to-PBX encrypted leg does not prove encryption across the carrier/PSTN.

## Development checks

```sh
python3 -B tests/test_toolkit.py
python3 -B tests/test_features.py
python3 -B tests/test_adapter_http.py
php tests/test_summary.php
python3 tools/package.py
```

The HTTP test downloads a pinned public interface and uses synthetic bytes, not real voicemail. PostgreSQL integration tests require an isolated Linux test environment and `PBXCTL_INTEGRATION=1`. The legacy three shell entry points are retained as wrappers around the new commands.

## References and license

- [Official base installer](https://github.com/fusionpbx/fusionpbx-install.sh)
- [Application upgrades](https://docs.fusionpbx.com/en/latest/advanced/upgrade.html)
- [SMTP configuration](https://docs.fusionpbx.com/en/latest/additional_information/email.html)
- [Google device/app SMTP](https://knowledge.workspace.google.com/admin/gmail/send-email-from-a-printer-scanner-or-app)
- [Restic repositories](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
- [Acrobits SRTP behavior](https://faq.acrobits.net/srtp-on-acrobits-softphone-and-groundwire)

MIT license; see `LICENSE`. Upstream software retains its own licenses and attribution.
