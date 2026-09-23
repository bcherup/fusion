"""Read-only PBX inventory, evidence-based recommendations and shareable reports."""
import datetime
import html
import json
import re
import shutil
import subprocess
from pathlib import Path
from .common import STATE, BACKUPS, Error, digest, literal, need, run, hostname

MUSIC_ROOT = Path('/usr/share/freeswitch/sounds/music')

PROFILE_FIELDS = (
    'auth-calls', 'accept-blind-auth', 'accept-blind-reg', 'aggressive-nat-detection',
    'apply-nat-acl', 'local-network-acl', 'sip-ip', 'rtp-ip', 'ext-sip-ip', 'ext-rtp-ip',
    'tls', 'tls-only', 'tls-sip-port', 'tls-version', 'tls-verify-policy',
    'inbound-codec-prefs', 'outbound-codec-prefs', 'apply-inbound-acl')
CORE_UNITS = ('freeswitch.service', 'postgresql.service', 'nginx.service', 'fail2ban.service')
FEATURE_UNITS = ('pbxctl-backup.timer', 'pbxctl-health.timer', 'pbxctl-transcribe.timer',
                 'pbx-whisper.service', 'pbxctl-ai-summary.timer', 'pbxctl-ai-model.service')
LIMITATIONS = [
    'Database settings and saved toolkit preferences are configuration, not proof of live call behavior.',
    'TLS listeners and trunk registration do not prove SRTP audio; verify both legs of a real call.',
    'Router forwarding, external reachability, push ringing and Wi-Fi/cellular handover are not tested.',
    'Phone speaker/microphone controls and arbitrary custom dialplan scripts cannot be measured here.',
    'Firewall summaries do not evaluate every chain, router rule or effective packet path.',
    'A recent backup is not proof of a successful replacement-server recovery. No test email or call is sent.',
]


def clean(value, limit=240):
    """Strip terminal control characters; HTML output is escaped separately."""
    if value is None: return 'Not verified'
    if isinstance(value, bool): return 'Yes' if value else 'No'
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(value))
    return ''.join(c for c in value if c.isprintable())[:limit]


def command(args):
    p = run(args, timeout=8, check=False)
    need(p.returncode == 0 and len(p.stdout) <= 2 * 1024 * 1024, 'Probe unavailable')
    need(not p.stdout.lstrip().startswith('-ERR'), 'Probe unavailable')
    return p.stdout


class ReadDatabase:
    def __init__(self, name): self.name = name

    def rows(self, query):
        # Every inventory query executes inside a server-enforced read-only transaction.
        need(query.startswith('SELECT '), 'Only inventory queries are supported')
        sql = "BEGIN READ ONLY; SET LOCAL statement_timeout='4s'; SELECT coalesce(json_agg(x),'[]'::json) FROM (" + query + ') x; COMMIT;'
        p = run(['runuser', '-u', 'postgres', '--', 'psql', '-X', '-qAt', '-v',
                 'ON_ERROR_STOP=1', '-d', self.name], data=sql, timeout=8)
        need(len(p.stdout) <= 2 * 1024 * 1024, 'Inventory exceeds report limit')
        return json.loads(p.stdout)


def bounded_json(path, maximum=2 * 1024 * 1024):
    path = Path(path)
    need(not path.is_symlink() and path.is_file() and path.stat().st_size <= maximum,
         'Inventory record unavailable')
    return json.loads(path.read_text())


def profile_runtime(raw):
    names = {'SIP-IP', 'RTP-IP', 'Ext-SIP-IP', 'Ext-RTP-IP', 'CODECS IN', 'CODECS OUT', 'TLS-URL'}
    result = {}
    for line in raw.splitlines():
        m = re.match(r'^([^\t]+?)\s{2,}(.+?)\s*$', line)
        if m and m[1] in names:
            if m[1] == 'TLS-URL':
                port = re.search(r':(\d{2,5})(?:[;>]|$)', m[2])
                result['TLS listener port'] = int(port[1]) if port else None
            else: result[m[1]] = clean(m[2])
    need(result, 'Unrecognized profile status')
    return result


def audio_action(application, data):
    """Return only known audio/security values, never raw dialplan or bridge strings."""
    if application == 'set_audio_level':
        m = re.fullmatch(r'(read|write)\s+(-?[0-4])', data)
        if m: return ('Microphone gain' if m[1] == 'read' else 'Listening gain', m[2] + ' steps')
    m = re.match(r'^(?:nolocal:)?(rtp_secure_media(?:_inbound|_outbound)?|hold_music|ringback|transfer_ringback)=(.*)$', data)
    if m:
        key, value = m.groups()
        if key.startswith('rtp_secure_media') and re.fullmatch(r'(?:optional|mandatory|true|false|forbidden)(?::[A-Z0-9_:]+)?', value):
            return (key, value)
        if not key.startswith('rtp_secure_media') and re.fullmatch(r'(?:local_stream|tone_stream)://[A-Za-z0-9_./%()+,;=-]+', value):
            return (key, value)
    if application == 'bridge':
        m = re.search(r'(?:\{|,)rtp_secure_media_outbound=((?:mandatory|optional)(?::[A-Z0-9_:]+)?)(?:,|\})', data)
        if m: return ('Carrier outbound SRTP policy', m[1])
    m = re.fullmatch(r'nolocal:execute_on_answer_pbxctl_volume=lua /opt/pbxctl/assets/call-volume.lua (-?[0-4]) (-?[0-4])', data)
    if m: return ('Answering phone gain', 'Microphone ' + m[1] + ' / listening ' + m[2] + ' steps')
    return None


class Scanner:
    def __init__(self, config=None, domain=None, database=None, source=None, db=None, probe=command):
        self.c = config or {}; self.domain = domain or self.c.get('domain'); self.domain_id = None
        database = database or self.c.get('database', 'fusionpbx')
        need(re.fullmatch(r'[A-Za-z0-9_-]+', database), 'Invalid database name')
        if self.domain:
            from .common import hostname
            self.domain = hostname(self.domain)
        self.db = db or ReadDatabase(database); self.probe = probe; self.units = {}
        self.r = {'report_kind': 'pbx-status', 'schema_version': 1,
                  'scanned_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'read_only': True, 'domain': self.domain,
                  'configuration_source': clean(source or ('Supplied settings' if config else 'Live discovery; no toolkit site file')),
                  'sections': [], 'findings': [], 'limitations': list(LIMITATIONS)}

    def section(self, key, title):
        s = {'id': key, 'title': title, 'rows': []}; self.r['sections'].append(s); return s

    def row(self, section, item, observed, evidence, saved=None, state='observed'):
        section['rows'].append({'item': clean(item), 'observed': clean(observed),
                                'saved': clean(saved) if saved is not None else None,
                                'evidence': clean(evidence), 'state': state})

    def finding(self, severity, area, message, advice):
        self.r['findings'].append({'severity': severity, 'area': clean(area),
                                   'message': clean(message), 'recommendation': clean(advice, 500)})

    def attempt(self, area, operation):
        try: return operation()
        except (Error, OSError, ValueError, KeyError, TypeError, AttributeError, TimeoutError, subprocess.TimeoutExpired):
            self.finding('unknown', area, 'This check could not be completed.',
                         'Check local permissions, service availability and schema compatibility; rerun the scan. Raw command output is withheld.')
            return None

    def discover(self):
        s = self.section('identity', 'System and scope')
        domains = self.db.rows('SELECT domain_uuid,domain_name FROM v_domains ORDER BY domain_name LIMIT 101')
        if not self.domain and len(domains) == 1: self.domain = hostname(domains[0]['domain_name'])
        matches = [d for d in domains if d['domain_name'] == self.domain]
        if len(matches) == 1: self.domain_id = matches[0]['domain_uuid']
        else:
            self.finding('unknown', 'Scope', 'A single PBX domain was not selected.',
                         'Use --domain with an existing domain. Tenant-specific audio, mail and extension checks are omitted.')
        self.r['domain'] = clean(self.domain) if self.domain else None
        self.row(s, 'Selected SIP domain', self.domain, 'PBX database')
        self.row(s, 'Available domains', ', '.join(clean(x['domain_name']) for x in domains), 'PBX database')
        self.row(s, 'Site preferences', 'Loaded' if self.c else 'Not present; discovering the existing PBX', self.r['configuration_source'])
        self.row(s, 'Scanner version', (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip(), 'Toolkit release')

    def services(self):
        s = self.section('services', 'Services and capacity')
        raw = self.probe(['systemctl', 'list-unit-files', '--no-legend', '--no-pager'])
        discovered = [line.split()[0] for line in raw.splitlines() if line.split() and
                      re.fullmatch(r'(?:pbx[-a-z0-9]*|fusionpbx-local[-a-z0-9]*)\.(?:timer|service)', line.split()[0])]
        names = sorted(set(CORE_UNITS + FEATURE_UNITS + tuple(discovered)))[:40]
        output = self.probe(['systemctl', 'show', *names, '--no-pager',
                             '--property=Id,LoadState,ActiveState,UnitFileState,Result'])
        for block in output.strip().split('\n\n'):
            values = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
            name = values.get('Id')
            if name not in names: continue
            self.units[name] = values
            if values.get('LoadState') == 'not-found':
                if name in CORE_UNITS: self.finding('error', 'Services', name + ' is missing.', 'Install or restore the required service before using the PBX.')
                continue
            self.row(s, name, values.get('ActiveState'), 'systemd; startup ' + values.get('UnitFileState', 'unknown'))
            if name in CORE_UNITS and values.get('ActiveState') != 'active':
                self.finding('error', 'Services', name + ' is not active.', 'Inspect this service locally before restarting it; check for active calls first.')
            elif values.get('ActiveState') == 'failed' or values.get('Result') not in (None, '', 'success'):
                transient = values.get('UnitFileState') == 'transient'
                self.finding('info' if transient else 'warning', 'Services', name + ' reports a failed transient run.' if transient else name + ' reports a failure.',
                             'Review the run history; transient test jobs can retain a failed state after completion.' if transient else 'Inspect this service journal locally; no automatic restart was attempted.')
        if not self.units: raise Error('No service status')
        free = shutil.disk_usage('/').free / 1024 ** 3
        self.row(s, 'Free disk', f'{free:.1f} GiB', 'Filesystem')
        if free < self.c.get('backup_min_free_gib', 5):
            self.finding('warning', 'Storage', 'Disk space is below the backup reserve.', 'Free space or expand storage before starting another backup.')
        calls = self.probe(['fs_cli', '-x', 'show calls count'])
        m = re.search(r'\b(\d+) total\.', calls)
        self.row(s, 'Active calls', int(m[1]) if m else None, 'FreeSWITCH runtime')

    def profiles(self):
        s = self.section('profiles', 'SIP profiles, NAT and codecs')
        profiles = self.db.rows('SELECT sip_profile_uuid,sip_profile_name FROM v_sip_profiles ORDER BY sip_profile_name LIMIT 17')
        if len(profiles) > 16: self.finding('unknown', 'Profiles', 'Profile inventory was limited to 16.', 'Inspect remaining profiles separately.')
        wanted = {self.c.get('internal_profile', 'internal'), self.c.get('external_profile', 'external')}
        for p in profiles[:16]:
            name = p['sip_profile_name']
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name): continue
            settings = self.db.rows('SELECT sip_profile_setting_name,sip_profile_setting_value,sip_profile_setting_enabled '
                'FROM v_sip_profile_settings WHERE sip_profile_uuid=' + literal(p['sip_profile_uuid']) +
                ' AND sip_profile_setting_name IN (' + ','.join(map(literal, PROFILE_FIELDS)) + ')')
            active = {}
            for item in settings:
                if str(item['sip_profile_setting_enabled']).lower() != 'true': continue
                key = item['sip_profile_setting_name']; value = item['sip_profile_setting_value']
                active.setdefault(key, []).append(value)
            for key, values in sorted(active.items()):
                saved = None
                if name == self.c.get('internal_profile', 'internal') and self.c:
                    saved = {'sip-ip': self.c.get('lan_ip'), 'rtp-ip': self.c.get('lan_ip'),
                             'tls-sip-port': self.c.get('tls_port')}.get(key)
                self.row(s, name + ' / ' + key, ', '.join(map(clean, values)), 'Enabled database setting', saved)
                if saved is not None and values != [str(saved)]:
                    self.finding('warning', 'Saved preferences', name + ' / ' + key + ' differs from the site file.', 'Decide which value is intended before applying configuration; the site file is not automatically authoritative.')
                if len(values) > 1 and key not in ('apply-inbound-acl', 'apply-nat-acl'):
                    self.finding('warning', 'Profiles', name + ' has duplicate ' + key + ' settings.', 'Resolve duplicate settings before reloading the profile.')
            if name == self.c.get('internal_profile', 'internal'):
                if active.get('aggressive-nat-detection') != ['true']:
                    self.finding('warning', 'NAT', 'Source-address NAT detection is not enabled on ' + name + '.', 'Review aggressive-nat-detection for remote/mobile phones, then verify remote-party hangup.')
                if active.get('accept-blind-auth') == ['true'] or active.get('accept-blind-reg') == ['true']:
                    self.finding('error', 'Authentication', 'Blind SIP authentication or registration is enabled.', 'Disable blind authentication after reviewing the phone profile and access policy.')
                if active.get('auth-calls') != ['true']:
                    self.finding('warning', 'Authentication', 'Authenticated calling is not explicitly enabled.', 'Review the internal profile authentication and access-list policy.')
                if active.get('tls') != ['true']:
                    self.finding('warning', 'TLS', 'Phone TLS is not enabled in the selected profile.', 'Configure a trusted certificate and TLS listener before requiring secure phone signaling.')
            runtime = self.attempt('Runtime profile ' + name, lambda: profile_runtime(self.probe(['fs_cli', '-x', 'sofia status profile ' + name])))
            if runtime:
                for key, value in runtime.items(): self.row(s, name + ' / ' + key, value, 'FreeSWITCH runtime')
                for configured, live in [('inbound-codec-prefs', 'CODECS IN'), ('outbound-codec-prefs', 'CODECS OUT'), ('sip-ip', 'SIP-IP'), ('rtp-ip', 'RTP-IP')]:
                    values = active.get(configured, [])
                    if len(values) == 1 and live in runtime and not any(x in values[0] for x in ('$', 'host:', 'auto', 'interface:')):
                        if values[0].replace(' ', '') != str(runtime[live]).replace(' ', ''):
                            self.finding('warning', 'Configuration drift', name + ' stored ' + configured + ' differs from runtime.', 'Review the difference and activate configuration during an idle window; the scanner does not reload it.')
            wanted.discard(name)
        for missing in sorted(wanted): self.finding('warning', 'Profiles', 'Expected profile not found: ' + missing, 'Check the selected site configuration and installed SIP profiles.')

    def phones(self):
        if not self.domain_id: return
        s = self.section('phones', 'Extensions and hold-music selections')
        rows = self.db.rows("SELECT extension,to_jsonb(e)->>'enabled' AS enabled,to_jsonb(e)->>'hold_music' AS hold_music "
                            'FROM v_extensions e WHERE domain_uuid=' + literal(self.domain_id) + ' ORDER BY extension LIMIT 101')
        if len(rows) > 100: self.finding('unknown', 'Extensions', 'Extension inventory was limited to 100.', 'Inspect remaining extensions in the PBX interface.')
        for row in rows[:100]:
            self.row(s, 'Extension ' + clean(row['extension']), 'Enabled: ' + clean(row['enabled']), 'PBX database')
            value = row.get('hold_music')
            if value and re.fullmatch(r'(?:local_stream://)?[A-Za-z0-9_./-]+', value):
                self.row(s, 'Extension ' + clean(row['extension']) + ' / music', value, 'PBX database; call routing can override')
        groups = self.attempt('Ring groups', lambda: self.db.rows("SELECT ring_group_extension,ring_group_name,to_jsonb(g)->>'ring_group_ringback' AS music "
            'FROM v_ring_groups g WHERE domain_uuid=' + literal(self.domain_id) + ' ORDER BY ring_group_extension LIMIT 100'))
        for row in groups or []:
            value = row.get('music')
            self.row(s, 'Group ' + clean(row['ring_group_extension']), clean(row['ring_group_name']), 'PBX database')
            if value and re.fullmatch(r'(?:local_stream://)?[A-Za-z0-9_./-]+', value): self.row(s, 'Group music', value, 'PBX database; ringback selection')

    def dialplan(self):
        if not self.domain_id: return
        s = self.section('audio', 'Call gain and media-security rules')
        rows = self.db.rows('SELECT p.dialplan_name,p.dialplan_order,p.dialplan_uuid,d.dialplan_detail_type,d.dialplan_detail_data '
            'FROM v_dialplans p JOIN v_dialplan_details d ON d.dialplan_uuid=p.dialplan_uuid '
            'WHERE p.dialplan_enabled=true AND d.dialplan_detail_enabled=true AND '
            '(p.domain_uuid=' + literal(self.domain_id) + ' OR (p.domain_uuid IS NULL AND p.dialplan_context=' + literal(self.domain) + ')) '
            "AND d.dialplan_detail_tag='action' AND (d.dialplan_detail_type='set_audio_level' OR "
            "d.dialplan_detail_data ~ '(rtp_secure_media|hold_music|ringback|pbxctl_volume)') ORDER BY p.dialplan_order,d.dialplan_detail_order LIMIT 201")
        if len(rows) > 200: self.finding('unknown', 'Call rules', 'Audio rule inventory was limited to 200.', 'Inspect additional custom rules manually.')
        found_gain = False; scopes = set()
        for row in rows[:200]:
            match = audio_action(row['dialplan_detail_type'], row['dialplan_detail_data'])
            if not match: continue
            label, value = match; found_gain |= 'gain' in label.lower()
            if row['dialplan_uuid'] not in scopes:
                scopes.add(row['dialplan_uuid'])
                conditions = self.db.rows('SELECT dialplan_detail_type,dialplan_detail_data FROM v_dialplan_details WHERE dialplan_uuid=' + literal(row['dialplan_uuid']) + " AND dialplan_detail_enabled=true AND dialplan_detail_tag='condition' ORDER BY dialplan_detail_order LIMIT 16")
                for condition in conditions:
                    if condition['dialplan_detail_type'] in ('destination_number', 'caller_id_number', 'sofia_profile_name'):
                        self.row(s, clean(row['dialplan_name']) + ' / condition', condition['dialplan_detail_type'] + ': ' + clean(condition['dialplan_detail_data']), 'Database routing condition; other conditions may also apply')
            self.row(s, clean(row['dialplan_name']) + ' / ' + label, value,
                     'Enabled database rule; order ' + clean(row['dialplan_order']) + '; conditional, not a live-call measurement')
            if 'optional' in value and ('SRTP' in label or label.startswith('rtp_secure_media')):
                self.finding('info', 'Encryption', 'An enabled rule allows optional SRTP.', 'Optional permits unencrypted fallback. Use check-media on each call leg to verify negotiated encryption.')
        if not found_gain:
            self.row(s, 'Explicit phone gain', 'No supported enabled gain action found', 'Database scan; custom scripts and phone controls may still adjust audio', state='unknown')
        self.row(s, 'Gain units', 'Steps for phone gain; dB for toolkit music attenuation', 'Gain is not the handset speaker-volume setting')

    def gateways(self):
        s = self.section('gateways', 'Carrier gateways')
        rows = self.db.rows('SELECT gateway_uuid,gateway,profile,enabled,register_transport FROM v_gateways ORDER BY gateway LIMIT 33')
        if len(rows) > 32: self.finding('unknown', 'Gateways', 'Gateway inventory was limited to 32.', 'Inspect remaining gateways separately.')
        for row in rows[:32]:
            label = clean(row['gateway']); transport = str(row.get('register_transport', '')).lower()
            self.row(s, label, 'Enabled: ' + clean(row['enabled']) + '; profile ' + clean(row['profile']), 'PBX database')
            self.row(s, label + ' / configured transport', transport if transport in ('tls', 'tcp', 'udp') else None, 'PBX database')
            key = row['gateway_uuid']
            if not re.fullmatch(r'[a-fA-F0-9-]{36}', key): continue
            if str(row['enabled']).lower() != 'true': continue
            raw = self.attempt('Gateway ' + label, lambda: self.probe(['fs_cli', '-x', 'sofia status gateway ' + key]))
            if raw:
                state = re.search(r'(?m)^State\s+([A-Z_]+)', raw)
                self.row(s, label + ' / registration', state[1] if state else None, 'FreeSWITCH runtime')
                self.row(s, label + ' / TLS transport evidence', 'TLS indicated' if 'transport=tls' in raw.lower() else 'Not confirmed', 'FreeSWITCH gateway status; audio encryption checked separately')
                if state and state[1] not in ('REGED', 'NOREG'):
                    self.finding('warning', 'Carrier', label + ' registration state is ' + state[1] + '.', 'Check the gateway and provider portal; do not reset credentials based only on this snapshot.')

    def voicemail(self):
        if not self.domain_id: return
        s = self.section('voicemail', 'Voicemail, transcription and email')
        rows = self.db.rows('SELECT domain_setting_category,domain_setting_subcategory,domain_setting_value FROM v_domain_settings WHERE domain_uuid=' + literal(self.domain_id) +
            " AND domain_setting_enabled=true AND ((domain_setting_category='transcribe' AND domain_setting_subcategory IN ('enabled','engine','api_model')) OR "
            "(domain_setting_category='email' AND domain_setting_subcategory IN ('smtp_host','smtp_port','smtp_secure','smtp_auth','smtp_validate_certificate')))")
        for row in rows:
            self.row(s, row['domain_setting_category'] + ' / ' + row['domain_setting_subcategory'], row['domain_setting_value'], 'Domain database setting; global defaults may also apply')
        defaults = self.attempt('Global mail and transcription defaults', lambda: self.db.rows("SELECT default_setting_category,default_setting_subcategory,default_setting_value FROM v_default_settings WHERE default_setting_enabled=true AND ((default_setting_category='transcribe' AND default_setting_subcategory IN ('enabled','engine','api_model')) OR (default_setting_category='email' AND default_setting_subcategory IN ('smtp_host','smtp_port','smtp_secure','smtp_auth','smtp_validate_certificate')))"))
        overridden = {(r['domain_setting_category'],r['domain_setting_subcategory']) for r in rows}
        for row in defaults or []:
            key=(row['default_setting_category'],row['default_setting_subcategory'])
            if key in overridden:continue
            self.row(s, ' / '.join(key), row['default_setting_value'], 'Global database default; user settings may override')
            rows.append({'domain_setting_category':key[0],'domain_setting_subcategory':key[1],'domain_setting_value':row['default_setting_value']})
        boxes = self.db.rows("SELECT voicemail_id,voicemail_transcription_enabled,(coalesce(voicemail_mail_to,'')<>'') AS email_recipient_set FROM v_voicemails WHERE domain_uuid=" + literal(self.domain_id) + ' ORDER BY voicemail_id LIMIT 100')
        for box in boxes:
            self.row(s, 'Mailbox ' + clean(box['voicemail_id']), 'Transcription: ' + clean(box['voicemail_transcription_enabled']) + '; email recipient: ' + clean(box['email_recipient_set']), 'PBX database; recipient address withheld')
        if not any(r['domain_setting_category'] == 'email' for r in rows):
            self.row(s, 'SMTP configuration', 'No enabled domain/global SMTP fields found', 'User settings or external relays require separate inspection', state='unknown')
        if not any(r['domain_setting_category'] == 'email' and r['domain_setting_subcategory'] == 'smtp_host' and r['domain_setting_value'] for r in rows):
            self.finding('warning', 'Email', 'No SMTP host is set in the inspected domain/global settings.', 'Check user overrides or the local mail relay. Configure SMTP and confirm inbox delivery before relying on email alerts.')
        if any(r['domain_setting_subcategory'] == 'smtp_validate_certificate' and r['domain_setting_value'] == 'false' for r in rows):
            self.finding('warning', 'Email', 'SMTP certificate validation is disabled in effective database settings.', 'Use a trusted SMTP certificate and enable validation.')

    def modules(self):
        from .config import MODULES
        s = self.section('modules', 'Toolkit feature records')
        for name in MODULES:
            marker = STATE / (name + '.json')
            if not marker.exists():
                self.row(s, name, 'No toolkit record', 'An existing/manual installation may still provide this feature', state='unmanaged')
                continue
            record = self.attempt('Module ' + name, lambda: bounded_json(marker))
            if not record: continue
            self.row(s, name, 'Recorded enabled: ' + clean(record.get('enabled', True)), 'Toolkit application record; revision ' + clean(record.get('revision', 1)))
            if name == 'call-volume':
                values = record.get('desired', {}).get('call_volume', {})
                for key in ('read_level', 'write_level'):
                    value = values.get(key)
                    if type(value) is int and -4 <= value <= 4:
                        self.row(s, 'Recorded ' + ('microphone' if key == 'read_level' else 'listening') + ' gain', str(value) + ' steps', 'Last toolkit apply; inspect enabled call rules above', self.c.get('call_volume', {}).get(key))
        self.attempt('Music volume verification', self.music)

    def music(self):
        music = self.section('music', 'Hold-music volume')
        marker = STATE / 'hold-music.json'
        if not marker.exists():
            self.row(music, 'Applied music gain', 'Unknown; no toolkit volume baseline', 'Music may already have been adjusted manually', state='unknown')
            self.finding('info', 'Music', 'An exact relative music gain cannot be inferred from an existing WAV alone.', 'Preserve the current tracks before adjusting them. The scanner does not assume unrecorded audio is at 0 dB.')
            return
        record = bounded_json(marker); m = record.get('desired', {}).get('hold_music', {})
        gain = m.get('gain_db') if m.get('enabled') else 0
        need(type(gain) in (int, float) and -30 <= gain <= 6, 'Invalid recorded gain')
        backup = Path(record['backup']).resolve(); root = BACKUPS.resolve()
        need(backup.parent == root, 'Unexpected module backup')
        actions = bounded_json(backup / 'managed.json')
        directory = Path(m['directory']).resolve(); music_root = MUSIC_ROOT.resolve()
        need(directory != music_root and music_root in directory.parents and directory.is_dir(), 'Music directory unavailable')
        files = list(directory.rglob('*.wav')); need(files and len(files) <= 200, 'Music inventory too large or empty')
        expected = {a['path']: a.get('after_sha256') for a in actions if a.get('kind') == 'file'}
        need(all(not p.is_symlink() and directory in p.resolve().parents for p in files), 'Linked music path')
        need(sum(p.stat().st_size for p in files) <= 256 * 1024 ** 2, 'Music verification exceeds 256 MiB')
        expected_music = {str(Path(p)) for p in expected if Path(p).suffix == '.wav' and directory in Path(p).resolve().parents}
        matched = expected_music == {str(p) for p in files} and all(expected.get(str(p)) == digest(p) for p in files)
        self.row(music, 'Recorded music gain', str(gain) + ' dB relative to preserved originals', 'Last toolkit apply', self.c.get('hold_music', {}).get('gain_db'))
        self.row(music, 'Current music files', 'Match recorded output' if matched else 'Changed since recorded output', 'SHA-256 verification of ' + str(len(files)) + ' WAV files', state='verified' if matched else 'warning')
        self.row(music, 'Selected stream', m.get('stream'), 'Last toolkit apply')
        if not matched: self.finding('warning', 'Music', 'Current tracks differ from the recorded volume output.', 'Review replaced or edited tracks; the recorded gain does not prove their current level.')

    def backups(self):
        s = self.section('backups', 'Backup and recovery readiness')
        path = Path('/var/backups/pbxctl/last-success.json')
        if path.exists():
            record = bounded_json(path); date = datetime.datetime.fromisoformat(record['created_utc'])
            age = (datetime.datetime.now(datetime.timezone.utc) - date).total_seconds() / 3600
            self.row(s, 'Last recorded local backup', date.isoformat(), 'Toolkit success record; ' + f'{age:.1f} hours ago')
            if age > 36: self.finding('warning', 'Backup', 'Last recorded backup is older than 36 hours.', 'Check the backup timer and storage; create and verify a fresh recovery set.')
        else:
            self.row(s, 'Last toolkit backup', 'No success record', 'Other backup systems are not assessed', state='unknown')
            self.finding('warning', 'Backup', 'No successful toolkit backup is recorded.', 'Confirm another backup system or configure local backups, then run verify-backup with an isolated database restore.')
        self.row(s, 'Off-server backup preference', self.c.get('remote_backup', {}).get('enabled'), 'Saved preference only; remote storage was not contacted')
        self.row(s, 'Full replacement-server recovery', 'Not verified by this scan', 'Requires an isolated restore and call test', state='unknown')

    def certificate(self):
        if not self.domain: return
        s = self.section('certificate', 'Certificate')
        path = Path('/etc/letsencrypt/live') / self.domain / 'cert.pem'
        raw = self.probe(['openssl', 'x509', '-in', str(path), '-noout', '-enddate'])
        m = re.search(r'^notAfter=(.+)$', raw.strip()); need(m, 'Certificate date unavailable')
        date = datetime.datetime.strptime(m[1], '%b %d %H:%M:%S %Y %Z').replace(tzinfo=datetime.timezone.utc)
        days = (date - datetime.datetime.now(datetime.timezone.utc)).days
        self.row(s, 'Certificate expires', date.isoformat(), 'Local certificate file; not a remote TLS handshake')
        if days < 21: self.finding('error' if days < 0 else 'warning', 'Certificate', 'Certificate expires in ' + str(days) + ' days.', 'Check renewal and deployment hooks; verify the active listener certificate afterward.')

    def network(self):
        s = self.section('network', 'Listeners and firewall overview')
        raw = self.probe(['ss', '-H', '-lntu'])
        for line in raw.splitlines()[:100]:
            fields = line.split()
            if len(fields) >= 5 and fields[0] in ('tcp', 'udp'):
                self.row(s, fields[0].upper() + ' listener', fields[4], 'Local socket; external reachability not tested')
        for tool in ('iptables', 'ip6tables'):
            raw = self.attempt(tool + ' overview', lambda: self.probe([tool, '-S', 'INPUT']))
            if raw is None: continue
            policy = re.search(r'(?m)^-P INPUT (ACCEPT|DROP)\s*$', raw)
            self.row(s, tool + ' INPUT policy', policy[1] if policy else 'Not determined', 'Filter table; jumps and other tables can change the result')
            hooks = re.findall(r'(?m)^-A INPUT .*?-j ([A-Za-z0-9_-]+)', raw)
            self.row(s, tool + ' INPUT targets', ', '.join(sorted(set(hooks))) or 'None', 'Summary only; no rules modified')

    def update_readiness(self):
        s = self.section('updates', 'Application update readiness')
        web = Path(self.c.get('web_root', '/var/www/fusionpbx'))
        args = ['git', '--no-optional-locks', '-c', 'safe.directory=' + str(web), '-C', str(web)]
        head = self.probe(args + ['rev-parse', 'HEAD']).strip()
        need(re.fullmatch(r'[0-9a-f]{40,64}', head), 'Git revision unavailable')
        dirty = self.probe(args + ['status', '--porcelain', '--untracked-files=no']).splitlines()
        self.row(s, 'Application revision', head[:12], 'Local Git checkout; main application only')
        self.row(s, 'Tracked local changes', len(dirty), 'Git status; untracked files and nested app repositories excluded')
        if dirty: self.finding('warning', 'Updates', 'Tracked local application edits exist.', 'Review and preserve these edits before updating. The scanner does not reset or discard files.')
        divergence = self.attempt('Cached upstream comparison', lambda: self.probe(args + ['rev-list', '--left-right', '--count', 'HEAD...@{upstream}']).strip())
        if divergence and re.fullmatch(r'\d+\s+\d+', divergence):
            ahead, behind = map(int, divergence.split())
            self.row(s, 'Cached upstream comparison', f'{ahead} local / {behind} upstream commits', 'Local tracking reference only; no network fetch')
            if ahead: self.finding('warning', 'Updates', 'Application has commits ahead of its cached upstream.', 'Review local commits before a fast-forward update; future merge compatibility is not guaranteed.')

    def collect(self):
        for name, operation in [('Scope', self.discover), ('Services', self.services), ('Profiles', self.profiles),
                                ('Extensions', self.phones), ('Call rules', self.dialplan), ('Gateways', self.gateways),
                                ('Voicemail', self.voicemail), ('Feature records', self.modules),
                                ('Backups', self.backups), ('Certificate', self.certificate),
                                ('Network', self.network), ('Update readiness', self.update_readiness)]:
            self.attempt(name, operation)
        order = {'error': 0, 'warning': 1, 'unknown': 2, 'info': 3}
        self.r['findings'].sort(key=lambda x: order[x['severity']])
        self.r['counts'] = {k: sum(x['severity'] == k for x in self.r['findings']) for k in order}
        self.r['result'] = 'Needs attention' if any(self.r['counts'][k] for k in ('error', 'warning')) else 'Incomplete' if self.r['counts']['unknown'] else 'No issues detected by completed checks'
        return self.r


def overview(report):
    """Small set of observed facts for an operator landing on a large report."""
    sections = {s['id']:s['rows'] for s in report['sections']}
    rows = [r for values in sections.values() for r in values]
    result = []
    def add(label, matches):
        values = [r['observed'] for r in matches]
        result.append((label, ' / '.join(dict.fromkeys(values)) if values else 'Not verified'))
    add('Active calls', [r for r in rows if r['item']=='Active calls'])
    add('Runtime codecs', [r for r in sections.get('profiles',[]) if r['item'].endswith('/ CODECS IN')])
    add('Source-address NAT detection', [r for r in rows if r['item'].endswith('/ aggressive-nat-detection')])
    add('Carrier registration', [r for r in rows if r['item'].endswith('/ registration')])
    add('Music adjustment', [r for r in sections.get('music',[]) if r['item'] in ('Recorded music gain','Applied music gain')])
    add('Local speech recognition', [r for r in rows if r['item']=='pbx-whisper.service'])
    add('Local summary model', [r for r in rows if r['item'] in ('pbx-ai-model.service','pbxctl-ai-model.service')])
    add('Tracked application edits', [r for r in rows if r['item']=='Tracked local changes'])
    return result


def render_text(report, diagnose=False):
    lines = ['PBX TOOLKIT | ' + ('DIAGNOSTICS' if diagnose else 'SYSTEM STATUS'),
             clean(report.get('domain') or 'Domain not selected') + ' | ' + report['scanned_at'],
             report['result'] + ' | Read-only snapshot', '']
    lines += [label + ': ' + value for label,value in overview(report)] + ['']
    for f in report['findings']:
        lines += ['[' + f['severity'].upper() + '] ' + f['area'] + ': ' + f['message'], '  Next: ' + f['recommendation']]
    for s in report['sections']:
        lines += ['', s['title'].upper(), '-' * min(76, max(24, len(s['title'])))]
        for row in s['rows']:
            lines.append('  ' + row['item'] + ': ' + row['observed'])
            if row['saved'] is not None: lines.append('    Saved preference: ' + row['saved'])
            lines.append('    Evidence: ' + row['evidence'])
    lines += ['', 'VERIFICATION LIMITS', *('  - ' + x for x in report['limitations'])]
    return '\n'.join(lines)


def render_html(report):
    esc = lambda value: html.escape(str(value), quote=True)
    findings = ''.join('<article class="finding ' + esc(f['severity']) + '"><strong>' + esc(f['severity'].upper()) + ' · ' + esc(f['area']) +
                       '</strong><p>' + esc(f['message']) + '</p><p class="next">' + esc(f['recommendation']) + '</p></article>' for f in report['findings'])
    sections = ''
    for s in report['sections']:
        rows = ''.join('<tr><th scope="row">' + esc(r['item']) + '</th><td>' + esc(r['observed']) +
                       ('<small>Saved preference: ' + esc(r['saved']) + '</small>' if r['saved'] is not None else '') +
                       '</td><td class="evidence">' + esc(r['evidence']) + '</td></tr>' for r in s['rows'])
        sections += '<section><h2>' + esc(s['title']) + '</h2><div class="scroll"><table><thead><tr><th>Setting</th><th>Observed</th><th>Evidence / scope</th></tr></thead><tbody>' + rows + '</tbody></table></div></section>'
    cards = ''.join('<div class="metric"><b>' + str(report['counts'][k]) + '</b><span>' + label + '</span></div>' for k, label in [('error', 'Errors'), ('warning', 'Warnings'), ('unknown', 'Incomplete checks'), ('info', 'Notes')])
    summary = '<section><h2>At a glance</h2><div class="glance">' + ''.join('<div><span>' + esc(label) + '</span><strong>' + esc(value) + '</strong></div>' for label,value in overview(report)) + '</div></section>'
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>PBX Toolkit · System report</title><style>
:root{color-scheme:light;--ink:#142638;--muted:#516476;--line:#dae3ea;--paper:#f3f6f9}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}main{max-width:1180px;margin:auto;padding:36px 24px 60px}header{background:#112e43;color:white;padding:30px 34px;border-radius:14px}.eyebrow{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:#a9d9dc}h1{font-size:30px;margin:10px 0}header p{margin:6px 0;color:#d0dee7}.badge{display:inline-block;margin-top:14px;padding:5px 11px;border:1px solid #638599;border-radius:30px;font-size:13px}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}.metric,section{background:white;border:1px solid var(--line);border-radius:12px}.metric{padding:18px 22px}.metric b{font-size:29px;display:block}.metric span{color:var(--muted)}section{margin-top:22px;overflow:hidden}h2{font-size:19px;margin:0;padding:19px 22px;border-bottom:1px solid var(--line)}.findings{padding:18px}.finding{border-left:4px solid #7692a6;padding:12px 16px;background:#f6f8fa;margin:0 0 12px}.finding:last-child{margin-bottom:0}.finding.error{border-color:#b64040}.finding.warning{border-color:#ba8418}.finding.info{border-color:#208188}.finding p{margin:5px 0}.next{color:var(--muted)}table{width:100%;border-collapse:collapse;text-align:left}th,td{padding:12px 20px;vertical-align:top;border-bottom:1px solid #e6edf2;overflow-wrap:anywhere}thead{background:#f5f8fa;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}tbody th{font-weight:600;width:29%}td{width:36%}.evidence{color:var(--muted);font-size:13px;width:35%}small{display:block;color:var(--muted);margin-top:5px}.limits{padding:10px 26px 18px;color:var(--muted)}footer{margin-top:24px;font-size:13px;color:var(--muted)}.scroll{overflow:auto}@media(max-width:650px){main{padding:16px 10px}.metrics{grid-template-columns:repeat(2,1fr)}header{padding:22px}th,td{padding:10px}table{min-width:580px}}@media print{body{background:white}main{padding:0}header{color:var(--ink);background:white;border:1px solid var(--line)}header p,.eyebrow{color:var(--muted)}section{break-inside:avoid}.scroll{overflow:visible}table{min-width:0}.finding{break-inside:avoid}}
.glance{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 30px;padding:6px 22px 18px}.glance>div{padding:14px 0;border-bottom:1px solid var(--line)}.glance span{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.glance strong{display:block;font-weight:600;overflow-wrap:anywhere}@media(max-width:650px){.glance{grid-template-columns:1fr}}
</style></head><body><main><header><div class="eyebrow">PBX Toolkit / Operations report</div><h1>''' + esc(report.get('domain') or 'System inventory') + '</h1><p>' + esc(report['result']) + '</p><p>' + esc(report['scanned_at']) + '</p><span class="badge">Read-only · No configuration changes</span></header><div class="metrics">' + cards + '</div>' + summary + '<section><h2>Recommendations</h2><div class="findings">' + (findings or '<p>No issues detected by the completed checks.</p>') + '</div></section>' + sections + '<section><h2>Verification limits</h2><ul class="limits">' + ''.join('<li>' + esc(x) + '</li>' for x in report['limitations']) + '</ul></section><footer>Configuration source: ' + esc(report['configuration_source']) + ' · Snapshot only. Internal hostnames and extensions may be present; review before sharing.</footer></main></body></html>'
