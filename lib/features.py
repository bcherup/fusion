"""Optional call security and audio controls, separate from vendor source."""
import array
import io
import json
import re
import socket
import ssl
import sys
import uuid
import wave
from pathlib import Path
import xml.etree.ElementTree as ET
from .common import STATE, ROOT, Error, atomic, digest, literal, need, run
from . import pbx

DEFAULTS = {
    'secure_calling': {'enabled': False, 'destinations': ['1000', '600'], 'mode': 'optional'},
    'carrier_tls': {'enabled': False, 'gateway_uuid': '', 'route_uuid': '',
                    'host': 'sip.telnyx.com', 'server_port': 5061, 'listen_port': 5081,
                    'portal_ready': False},
    'hold_music': {'enabled': False, 'directory': '', 'stream': '', 'gain_db': -8},
    'call_volume': {'enabled': False, 'extensions': ['1000'], 'destinations': ['1000', '600'],
                    'read_level': 0, 'write_level': 0},
    'ai_summary': {'enabled': False, 'port': 18081, 'cpu_percent': 50, 'memory_mb': 1500},
}
KEYS = {'secure-calling': 'secure_calling', 'carrier-tls': 'carrier_tls',
        'hold-music': 'hold_music', 'call-volume': 'call_volume', 'ai-summary': 'ai_summary'}
MUSIC_ROOT = Path('/usr/share/freeswitch/sounds/music')


def tls_listener_ports(c):
    ports = [c['tls_port']]
    if c.get('carrier_tls', {}).get('enabled'): ports.append(c['carrier_tls']['listen_port'])
    return ports


def number_expression(numbers):
    need(numbers and all(isinstance(x, str) and re.fullmatch(r'[0-9]{2,8}', x) for x in numbers), 'Select numeric phone destinations')
    return '^(' + '|'.join(sorted(set(numbers))) + ')$'


def rule(db, c, ch, name, conditions, enabled, order=90):
    domain = pbx.domain(db, c)
    key = str(uuid.uuid5(uuid.UUID(domain), 'pbxctl:' + name))
    owned = db.rows('SELECT * FROM v_dialplans WHERE dialplan_uuid=' + literal(key))
    description = 'Managed by PBX Toolkit: ' + name
    need(not owned or owned[0].get('dialplan_description') == description, 'Dialplan identifier collision')
    root = ET.Element('extension', name=name, **{'continue': 'true', 'uuid': key})
    details = []
    for group, (field, expression, actions) in enumerate(conditions):
        node = ET.SubElement(root, 'condition', field=field, expression=expression)
        details.append((group, 'condition', field, expression))
        for application, data in actions:
            ET.SubElement(node, 'action', application=application, data=data)
            details.append((group, 'action', application, data))
    ch.row('v_dialplans', 'dialplan_uuid', {'dialplan_uuid': key, 'domain_uuid': domain,
        'dialplan_context': c['domain'], 'dialplan_name': name, 'dialplan_continue': True,
        'dialplan_destination': False, 'dialplan_order': order, 'dialplan_enabled': enabled,
        'dialplan_description': description, 'dialplan_xml': ET.tostring(root, encoding='unicode')})
    existing = db.rows('SELECT dialplan_detail_uuid FROM v_dialplan_details WHERE dialplan_uuid=' + literal(key))
    retained = set()
    for i, (group, tag, kind, data) in enumerate(details):
        item = str(uuid.uuid5(uuid.UUID(key), str(i)))
        retained.add(item)
        ch.row('v_dialplan_details', 'dialplan_detail_uuid', {'dialplan_detail_uuid': item,
            'domain_uuid': domain, 'dialplan_uuid': key, 'dialplan_detail_tag': tag,
            'dialplan_detail_type': kind, 'dialplan_detail_data': data,
            'dialplan_detail_group': group, 'dialplan_detail_order': (i + 1) * 10,
            'dialplan_detail_enabled': True})
    for item in existing:
        if item['dialplan_detail_uuid'] not in retained:
            ch.row('v_dialplan_details', 'dialplan_detail_uuid', {**item, 'dialplan_detail_enabled': False})


def secure_calling(db, c, ch):
    s = c['secure_calling']
    if s['enabled']:
        p = pbx.profile(db, c['internal_profile'])
        settings = db.rows('SELECT sip_profile_setting_name,sip_profile_setting_value FROM v_sip_profile_settings '
                          'WHERE sip_profile_uuid=' + literal(p) + ' AND sip_profile_setting_enabled=true')
        values = {r['sip_profile_setting_name']: r['sip_profile_setting_value'] for r in settings}
        need(values.get('tls') == 'true', 'Activate and verify phone TLS before enabling secure calling')
    rule(db, c, ch, 'PBX Toolkit phone SRTP', [('destination_number', number_expression(s['destinations']),
         [('export', 'nolocal:rtp_secure_media_outbound=' + s['mode'] + ':AES_CM_128_HMAC_SHA1_80')])], s['enabled'])
    return {'next': 'Set phone TLS and SDES; verify foreground, background and hold/resume. Optional permits RTP fallback.'}


def call_volume(db, c, ch):
    s = c['call_volume']
    levels = [('set_audio_level', 'read ' + str(s['read_level'])),
              ('set_audio_level', 'write ' + str(s['write_level']))]
    rule(db, c, ch, 'PBX Toolkit originating phone gain', [
        ('${sofia_profile_name}', '^' + re.escape(c['internal_profile']) + '$', []),
        ('caller_id_number', number_expression(s['extensions']), levels)], s['enabled'], 88)
    # A distinct execute_on_answer suffix preserves existing application hooks.
    hook = 'nolocal:execute_on_answer_pbxctl_volume=lua ' + str(ROOT / 'assets/call-volume.lua')
    hook += ' ' + str(s['read_level']) + ' ' + str(s['write_level'])
    rule(db, c, ch, 'PBX Toolkit answering phone gain', [
        ('destination_number', number_expression(s['destinations']), [('export', hook)])], s['enabled'], 89)
    return {'scope': 'New selected phone legs; gain steps are not dB. Read=phone to PBX, write=PBX to phone.'}


def scale_wav(data, db_gain):
    """Render PCM16 from the preserved source; reject clipping rather than silently distort."""
    with wave.open(io.BytesIO(data), 'rb') as source:
        need(source.getsampwidth() == 2 and source.getcomptype() == 'NONE', 'Hold music must be PCM16 WAV')
        need(source.getnchannels() in (1, 2) and source.getframerate() in (8000, 16000, 32000, 48000), 'Unsupported WAV format')
        params = source.getparams()
        samples = array.array('h', source.readframes(source.getnframes()))
    if sys.byteorder != 'little': samples.byteswap()
    factor = 10 ** (db_gain / 20)
    for i, sample in enumerate(samples):
        scaled = round(sample * factor)
        need(-32768 <= scaled <= 32767, 'Requested gain would clip; choose a lower level')
        samples[i] = scaled
    if sys.byteorder != 'little': samples.byteswap()
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setparams(params); target.writeframes(samples.tobytes())
    return output.getvalue(), params.framerate


def refresh_music(c, rates):
    for rate in sorted(set(rates)):
        response = run(['fs_cli', '-x', 'local_stream hup ' + c['hold_music']['stream'] + '/' + str(rate)]).stdout
        need('-ERR' not in response and 'not found' not in response.lower(), 'Hold stream refresh failed; check stream name')


def hold_music(db, c, ch):
    s = c['hold_music']; base = STATE / 'hold-music-originals'; manifest = base / 'manifest.json'
    if not s['enabled'] and not manifest.exists(): return {'status': 'already disabled'}
    directory = Path(s['directory']).resolve()
    allowed = MUSIC_ROOT.resolve()
    need(directory.is_dir() and directory != allowed and allowed in directory.parents, 'Select one folder inside the music directory')
    files = sorted(directory.rglob('*.wav'))
    need(files and all(not p.is_symlink() and directory in p.resolve().parents for p in files), 'Music files must stay inside the selected folder')
    need(all(p.stat().st_size <= 64 * 1024 ** 2 for p in files), 'WAV exceeds 64 MiB; split long tracks first')
    if manifest.exists():
        original = json.loads(manifest.read_text())
        need(original['directory'] == str(directory), 'Restore and review the existing music baseline before changing folders')
        need(set(original['files']) == {p.relative_to(directory).as_posix() for p in files}, 'Music set changed; review originals before applying gain')
    else:
        original = {'directory': str(directory), 'files': {}}
        for i, p in enumerate(files):
            dest = base / (str(i) + '.wav')
            need(not dest.exists(), 'Unrecorded music baseline exists; inspect before continuing')
            ch.file(dest, p.read_bytes(), 0o600)
            original['files'][p.relative_to(directory).as_posix()] = {'file': dest.name, 'sha256': digest(dest)}
        ch.file(manifest, json.dumps(original, indent=2), 0o600)
    rendered = []
    stage = ch.path / 'rendered-music'; stage.mkdir(mode=0o700)
    for i, p in enumerate(files):
        item = original['files'][p.relative_to(directory).as_posix()]; source = base / item['file']
        need(not source.is_symlink() and digest(source) == item['sha256'], 'Music original changed')
        data, rate = scale_wav(source.read_bytes(), s['gain_db'] if s['enabled'] else 0)
        rendered_path = stage / (str(i) + '.wav')
        atomic(rendered_path, data if s['enabled'] else source.read_bytes())
        rendered.append((p, rendered_path, rate))
    # Validate every variant before replacing any live track.
    atomic(ch.path / 'music-refresh.json', json.dumps({'stream': s['stream'], 'rates': sorted({x[2] for x in rendered})}))
    for p, rendered_path, rate in rendered:
        st = p.stat(); ch.file(p, rendered_path.read_bytes(), st.st_mode & 0o777, (st.st_uid, st.st_gid))
    refresh_music(c, [x[2] for x in rendered])
    return {'files': len(rendered), 'gain_db': s['gain_db'] if s['enabled'] else 0,
            'baseline': str(manifest), 'scope': 'Music files only; gain is relative to the first preserved originals'}


def carrier_tls(db, c, ch):
    s = c['carrier_tls']; baseline = STATE / 'carrier-tls-baseline.json'
    if not s['enabled']:
        if not baseline.exists(): return {'status': 'already disabled'}
        saved = json.loads(baseline.read_text())
        for table, key, row in saved['rows']: ch.row(table, key, row)
        return {'restart_profile': saved['profile'], 'next': 'Restore the matching carrier portal media policy and test both directions.'}
    need(s['portal_ready'], 'Confirm carrier SRTP support, portal media policy and inbound TLS routing with carrier_tls.portal_ready')
    need(c['provider_cidrs'], 'Configure verified carrier signaling addresses first')
    profile = pbx.profile(db, c['external_profile'])
    gateways = db.rows('SELECT gateway_uuid FROM v_gateways WHERE profile=' + literal(c['external_profile']) + ' AND enabled=true')
    need(len(gateways) == 1 and gateways[0]['gateway_uuid'] == s['gateway_uuid'], 'Use a dedicated external profile with only the selected gateway')
    gateway = db.one('SELECT gateway_uuid,proxy,register_transport,register FROM v_gateways WHERE gateway_uuid=' + literal(s['gateway_uuid']))
    need(gateway.pop('register') is True, 'Automatic carrier activation currently supports credential-registration trunks')
    certdir = '/etc/freeswitch-tls/' + c['domain'] + '/current'
    need((Path(certdir) / 'agent.pem').is_file(), 'Deploy and verify the TLS certificate first')
    ctx = ssl.create_default_context(cafile=str(Path(certdir) / 'cafile.pem'))
    with socket.create_connection((s['host'], s['server_port']), timeout=8) as sock:
        with ctx.wrap_socket(sock, server_hostname=s['host']): pass
    values = {'tls': 'true', 'tls-only': 'true', 'tls-cert-dir': certdir,
              'tls-sip-port': str(s['listen_port']), 'tls-version': 'tlsv1.2,tlsv1.3',
              'tls-verify-date': 'true', 'tls-verify-policy': 'subjects_out', 'tls-verify-depth': '5',
              'tls-ciphers': 'ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384'}
    settings = db.rows('SELECT * FROM v_sip_profile_settings WHERE sip_profile_uuid=' + literal(profile))
    need(any(r['sip_profile_setting_name']=='apply-inbound-acl' and r['sip_profile_setting_value']==c['provider_acl'] and r['sip_profile_setting_enabled'] for r in settings),
         'External profile must use the configured provider ACL; configure hardening first')
    acl = db.one('SELECT access_control_uuid,access_control_default FROM v_access_controls WHERE access_control_name=' + literal(c['provider_acl']))
    nodes = db.rows('SELECT node_cidr,node_type,node_domain FROM v_access_control_nodes WHERE access_control_uuid=' + literal(acl['access_control_uuid']))
    need(acl['access_control_default']=='deny' and {r['node_cidr'] for r in nodes}==set(c['provider_cidrs']) and all(r['node_type']=='allow' and not r['node_domain'] for r in nodes),
         'Provider ACL must allow exactly the verified carrier addresses')
    selected = []
    for name in values:
        matches = [r for r in settings if r['sip_profile_setting_name'] == name]
        need(len(matches) == 1, 'Expected one existing carrier profile setting: ' + name)
        r = matches[0]; selected.append({k: r[k] for k in ('sip_profile_setting_uuid', 'sip_profile_setting_value', 'sip_profile_setting_enabled')})
    plan = db.one('SELECT dialplan_uuid,dialplan_xml FROM v_dialplans WHERE domain_uuid=' + literal(pbx.domain(db, c)) + ' AND dialplan_uuid=' + literal(s['route_uuid']))
    details = db.rows('SELECT dialplan_detail_uuid,dialplan_detail_data FROM v_dialplan_details WHERE dialplan_uuid=' + literal(s['route_uuid']) + " AND dialplan_detail_type='bridge' AND dialplan_detail_enabled=true")
    need(len(details) == 1, 'Select a route with one gateway bridge')
    if not baseline.exists():
        need(details[0]['dialplan_detail_data'].startswith('sofia/gateway/' + s['gateway_uuid'] + '/'), 'Unexpected route bridge; review before changing')
        rows = [('v_sip_profile_settings', 'sip_profile_setting_uuid', r) for r in selected]
        rows += [('v_gateways', 'gateway_uuid', gateway), ('v_dialplans', 'dialplan_uuid', plan), ('v_dialplan_details', 'dialplan_detail_uuid', details[0])]
        ch.file(baseline, json.dumps({'profile': c['external_profile'], 'gateway': s['gateway_uuid'], 'route': s['route_uuid'], 'rows': rows}, indent=2), 0o600)
    saved = json.loads(baseline.read_text())
    need((saved['gateway'], saved['route'], saved['profile']) == (s['gateway_uuid'], s['route_uuid'], c['external_profile']), 'Restore the previous carrier baseline before selecting another trunk')
    original_plan = next(r for t, k, r in saved['rows'] if t == 'v_dialplans')
    original_bridge = next(r['dialplan_detail_data'] for t, k, r in saved['rows'] if t == 'v_dialplan_details')
    tree = ET.fromstring(original_plan['dialplan_xml'])
    actions = [e for e in tree.iter('action') if e.get('application') == 'bridge' and e.get('data') == original_bridge]
    need(len(actions) == 1, 'Route XML and editable details disagree')
    data = '{rtp_secure_media_outbound=mandatory:AEAD_AES_256_GCM_8:AES_CM_128_HMAC_SHA1_80}' + original_bridge
    actions[0].set('data', data)
    for name, value in values.items(): pbx.setting(db, ch, profile, name, value)
    ch.row('v_gateways', 'gateway_uuid', {'gateway_uuid': s['gateway_uuid'], 'proxy': s['host'] + ':' + str(s['server_port']), 'register_transport': 'tls'})
    ch.row('v_dialplans', 'dialplan_uuid', {'dialplan_uuid': s['route_uuid'], 'dialplan_xml': ET.tostring(tree, encoding='unicode')})
    ch.row('v_dialplan_details', 'dialplan_detail_uuid', {**details[0], 'dialplan_detail_data': data})
    return {'restart_profile': c['external_profile'], 'next': 'Verify firewall/router TCP port and TLS/SRTP on incoming and outgoing calls; registration alone is insufficient.'}
