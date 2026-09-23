"""Read-only inventory, uncertainty, secret handling and drift regression tests."""
import datetime
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pbxctl
from lib import diagnostics as d
from lib.common import Error, digest

DOMAIN = '11111111-1111-4111-8111-111111111111'
GATEWAY = '22222222-2222-4222-8222-222222222222'


class FixtureDatabase:
    def __init__(self): self.queries = []
    def rows(self, query):
        self.queries.append(query)
        assert query.startswith('SELECT ') and 'SELECT *' not in query
        if 'FROM v_domains ' in query: return [{'domain_uuid': DOMAIN, 'domain_name': 'voip.example.com'}]
        if 'FROM v_sip_profiles ' in query: return [{'sip_profile_uuid': DOMAIN, 'sip_profile_name': 'internal'}]
        if 'FROM v_sip_profile_settings ' in query:
            return [{'sip_profile_setting_name': key, 'sip_profile_setting_value': value, 'sip_profile_setting_enabled': True}
                    for key, value in {'auth-calls': 'true', 'tls': 'true', 'aggressive-nat-detection': 'true', 'inbound-codec-prefs': 'OPUS,G722,PCMU,PCMA'}.items()]
        if 'FROM v_extensions ' in query: return [{'extension': '1000', 'enabled': 'true', 'hold_music': 'local_stream://voip.example.com/office'}]
        if 'FROM v_ring_groups ' in query: return [{'ring_group_extension': '600', 'ring_group_name': 'Office', 'music': 'local_stream://voip.example.com/office'}]
        if 'FROM v_dialplans ' in query: return [{'dialplan_name': 'Phone listening volume', 'dialplan_order': 88, 'dialplan_uuid': DOMAIN, 'dialplan_detail_type': 'set_audio_level', 'dialplan_detail_data': 'write -1'}]
        if 'FROM v_dialplan_details ' in query: return [{'dialplan_detail_type': 'caller_id_number', 'dialplan_detail_data': '^(1000)$'}]
        if 'FROM v_gateways ' in query: return [{'gateway_uuid': GATEWAY, 'gateway': 'Carrier', 'profile': 'external', 'enabled': True, 'register_transport': 'tls'}]
        if 'FROM v_domain_settings ' in query: return []
        if 'FROM v_default_settings ' in query: return [{'default_setting_category': 'email', 'default_setting_subcategory': 'smtp_host', 'default_setting_value': 'smtp.example.com'}]
        if 'FROM v_voicemails ' in query: return [{'voicemail_id': '1000', 'voicemail_transcription_enabled': True, 'email_recipient_set': True}]
        raise AssertionError(query)


def fixture_probe(args):
    if args[:2] == ['systemctl', 'list-unit-files']: return 'fusionpbx-local-transcribe.timer enabled enabled\n'
    if args[:2] == ['systemctl', 'show']:
        return '\n\n'.join('Id='+name+'\nLoadState=loaded\nActiveState=active\nUnitFileState=enabled\nResult=success' for name in (*d.CORE_UNITS, 'fusionpbx-local-transcribe.timer'))
    if args[:2] == ['fs_cli', '-x']:
        if args[2] == 'show calls count': return '0 total.\n'
        if args[2].startswith('sofia status profile'): return 'SIP-IP           \t192.0.2.4\nCODECS IN        \tOPUS,G722,PCMU,PCMA\nTLS-URL          \tsip:mod_sofia@192.0.2.4:5061\n'
        if args[2].startswith('sofia status gateway'): return 'State REGED\nContact sip:PRIVATE-GATEWAY-SECRET@example.com;transport=tls\n'
    if args[0] == 'openssl': return 'notAfter=Sep 23 12:00:00 2036 GMT\n'
    if args[0] == 'ss': return 'tcp LISTEN 0 128 0.0.0.0:5061 0.0.0.0:*\n'
    if args[0] in ('iptables', 'ip6tables'): return '-P INPUT DROP\n-A INPUT -j PBXCTL4\n'
    if args[0] == 'git':
        if args[-2:] == ['rev-parse', 'HEAD']: return 'a'*40+'\n'
        if 'status' in args: return ''
        if 'rev-list' in args: return '0\t0\n'
    raise AssertionError(args)


def sample_report():
    with tempfile.TemporaryDirectory() as temp, patch.object(d, 'STATE', Path(temp)):
        return d.Scanner(db=FixtureDatabase(), probe=fixture_probe).collect()


class DiagnosticsTests(unittest.TestCase):
    def test_inventory_without_site_config_detects_legacy_features(self):
        report = sample_report(); text = d.render_text(report)
        self.assertTrue(report['read_only']); self.assertEqual(report['domain'], 'voip.example.com')
        for value in ('fusionpbx-local-transcribe.timer', 'OPUS,G722', 'smtp.example.com', 'caller_id_number: ^(1000)$', '-1 steps'):
            self.assertIn(value, text)
        self.assertIn('Unknown; no toolkit volume baseline', text)
        self.assertNotIn('PRIVATE-GATEWAY-SECRET', json.dumps(report))

    def test_runtime_parser_handles_real_spaces_and_tabs(self):
        value = d.profile_runtime(fixture_probe(['fs_cli','-x','sofia status profile internal']))
        self.assertEqual(value['TLS listener port'], 5061)
        self.assertEqual(value['CODECS IN'], 'OPUS,G722,PCMU,PCMA')

    def test_failed_probes_produce_unknown_not_clean_bill(self):
        scanner = d.Scanner(db=Mock(rows=Mock(side_effect=Error('PRIVATE-PASSWORD'))), probe=Mock(side_effect=subprocess.TimeoutExpired('private', 8)))
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'STATE', Path(temp)):
            report = scanner.collect()
        self.assertGreater(report['counts']['unknown'], 0)
        self.assertNotIn('PRIVATE-PASSWORD', json.dumps(report))
        self.assertNotEqual(report['result'], 'No issues detected by completed checks')

    def test_unsafe_domain_and_database_refused(self):
        for args in ({'domain':'../../etc/shadow'}, {'database':'db;DELETE'}):
            with self.assertRaises(Error): d.Scanner(**args)
        scanner=d.Scanner(db=Mock(rows=Mock(return_value=[{'domain_uuid':DOMAIN,'domain_name':'../../etc/shadow'}])))
        scanner.attempt('Scope', scanner.discover)
        self.assertIsNone(scanner.domain)

    def test_multiple_domains_do_not_guess_tenant(self):
        db=Mock(rows=Mock(return_value=[{'domain_uuid':DOMAIN,'domain_name':'one.example.com'},{'domain_uuid':GATEWAY,'domain_name':'two.example.com'}]))
        scanner=d.Scanner(db=db);scanner.discover()
        self.assertIsNone(scanner.domain_id);self.assertEqual(scanner.r['findings'][0]['severity'],'unknown')

    def test_sql_enforced_read_only_and_no_password_projection(self):
        with patch.object(d,'run',return_value=Mock(stdout='[]')) as call:
            d.ReadDatabase('fixture').rows('SELECT domain_name FROM v_domains')
        sql=call.call_args.kwargs['data'];self.assertTrue(sql.startswith('BEGIN READ ONLY;'));self.assertIn('statement_timeout',sql)
        db=FixtureDatabase();scanner=d.Scanner(db=db,probe=fixture_probe)
        with tempfile.TemporaryDirectory() as temp, patch.object(d,'STATE',Path(temp)):scanner.collect()
        for query in db.queries:
            for secret in ('smtp_password','sip_auth_password','password_file','sip_password','api_key'):self.assertNotIn(secret,query)

    def test_security_parser_does_not_return_sip_credentials(self):
        self.assertEqual(d.audio_action('bridge','{rtp_secure_media_outbound=mandatory:AES_CM_128_HMAC_SHA1_80}sofia/gateway/PRIVATE/$1'),('Carrier outbound SRTP policy','mandatory:AES_CM_128_HMAC_SHA1_80'))
        self.assertIsNone(d.audio_action('set','hold_music=https://user:PRIVATE@example.com/music'))
        self.assertIsNone(d.audio_action('set_audio_level','write 100'))

    def test_html_escapes_database_values_and_has_no_remote_dependencies(self):
        report=sample_report();report['domain']='<script>alert("x")</script>'
        rendered=d.render_html(report)
        self.assertIn('&lt;script&gt;',rendered);self.assertNotIn('<script',rendered)
        self.assertNotIn('src=',rendered);self.assertNotIn('href=',rendered)
        self.assertNotIn('\x1b',d.clean('\x1b[31mred\x00'))

    def test_saved_configuration_difference_is_not_silently_applied(self):
        db=FixtureDatabase();original=db.rows
        def rows(q):
            value=original(q)
            if 'FROM v_sip_profile_settings ' in q:value.append({'sip_profile_setting_name':'tls-sip-port','sip_profile_setting_value':'5061','sip_profile_setting_enabled':True})
            return value
        db.rows=rows
        scanner=d.Scanner({'tls_port':5091},db=db,probe=fixture_probe);scanner.profiles()
        self.assertTrue(any(f['area']=='Saved preferences' for f in scanner.r['findings']))

    def test_nat_warning_when_missing(self):
        db=FixtureDatabase();original=db.rows
        db.rows=lambda q:[r for r in original(q) if r.get('sip_profile_setting_name')!='aggressive-nat-detection']
        scanner=d.Scanner(db=db,probe=fixture_probe);scanner.profiles()
        self.assertTrue(any(f['area']=='NAT' for f in scanner.r['findings']))

    def test_music_verification_detects_edits_and_removed_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);state=root/'state';state.mkdir();music=root/'music'/'office';music.mkdir(parents=True)
            backups=root/'backups';backup=backups/'fixture';backup.mkdir(parents=True)
            files=[music/'one.wav',music/'two.wav']
            for f in files:f.write_bytes(b'original fixture')
            (backup/'managed.json').write_text(json.dumps([{'kind':'file','path':str(f),'after_sha256':digest(f)} for f in files]))
            (state/'hold-music.json').write_text(json.dumps({'backup':str(backup),'desired':{'hold_music':{'enabled':True,'gain_db':-8,'directory':str(music),'stream':'local_stream://office'}}}))
            with patch.object(d,'STATE',state),patch.object(d,'BACKUPS',backups),patch.object(d,'MUSIC_ROOT',root/'music'):
                scanner=d.Scanner();scanner.music();self.assertFalse(scanner.r['findings'])
                files[0].write_bytes(b'changed');scanner=d.Scanner();scanner.music();self.assertTrue(scanner.r['findings'])
                files[0].write_bytes(b'original fixture');files[1].unlink()
                scanner=d.Scanner();scanner.music();self.assertTrue(scanner.r['findings'])

    def test_read_only_cli_rejects_apply_before_host_probes(self):
        with patch('lib.base.supported') as supported:
            with self.assertRaises(Error):pbxctl.main(['doctor','--apply'])
        supported.assert_not_called()

    def test_cli_discovers_without_loading_example_or_locking(self):
        report=sample_report()
        with tempfile.TemporaryDirectory() as temp, patch.object(pbxctl,'CONFIG',Path(temp)/'absent'),patch('lib.base.supported'),patch.object(pbxctl,'load_config') as config,patch.object(d,'Scanner') as scanner:
            scanner.return_value.collect.return_value=report
            result=pbxctl.main(['status','--format','json'])
            self.assertTrue(result['read_only']);config.assert_not_called()
            scanner.assert_called_once_with(None,domain=None,database=None,source=None)

    def test_plain_text_output_is_not_json_quoted(self):
        with patch('sys.stdout',new_callable=io.StringIO) as out:pbxctl.print_result('status\nsecond line')
        self.assertEqual(out.getvalue(),'status\nsecond line\n')


if __name__=='__main__':unittest.main()
