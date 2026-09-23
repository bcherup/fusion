"""Feature behavior tests with synthetic audio and isolated configuration."""
import copy
import io
import json
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import common, config, features, firewall, recovery
import pbxctl

ROOT = Path(__file__).resolve().parents[1]
def sample(): return config.load_config(ROOT / 'site.example.json')
def wav(values, rate=8000):
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        f.writeframes(struct.pack('<' + 'h' * len(values), *values))
    return out.getvalue()
def samples(data):
    with wave.open(io.BytesIO(data), 'rb') as f:
        return f.getparams(), struct.unpack('<' + 'h' * f.getnframes(), f.readframes(f.getnframes()))

class FileChange:
    def __init__(self, path): self.path = path; path.mkdir()
    def file(self, path, data, mode=0o644, owner=None):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)

class FeatureTests(unittest.TestCase):
    def test_old_config_gets_disabled_new_features(self):
        c = sample()
        for key in features.DEFAULTS: del c[key]
        loaded = config.validate_all(c)
        self.assertTrue(all(not loaded[k]['enabled'] for k in features.DEFAULTS))

    def test_gain_bounds_and_injection(self):
        for key, field, value in [('hold_music','gain_db',float('nan')), ('call_volume','read_level',5),
                                  ('hold_music','stream','music; shutdown'), ('secure_calling','destinations',['.*'])]:
            with self.subTest(field=field):
                c = sample(); c[key][field] = value
                with self.assertRaises(common.Error): config.validate_all(c)

    def test_port_collision(self):
        c = sample(); c['ai_summary']['port'] = c['whisper_port']
        with self.assertRaises(common.Error): config.validate_all(c)

    def test_certificate_renewal_verifies_each_enabled_tls_listener(self):
        c=sample();self.assertEqual(features.tls_listener_ports(c),[5061])
        c['carrier_tls']['enabled']=True
        self.assertEqual(features.tls_listener_ports(c),[5061,5081])
        del c['carrier_tls']
        self.assertEqual(features.tls_listener_ports(c),[5061])

    def test_feature_preview_does_not_mutate_host(self):
        with patch('lib.base.supported') as live:
            r = pbxctl.main(['feature','--config',str(ROOT/'site.example.json'),'--name','call-volume','--enable','--write-level','-1'])
        self.assertEqual(r['proposed']['call-volume']['call_volume']['write_level'], -1)
        self.assertTrue(r['apply_required']); live.assert_not_called()

    def test_wrong_gain_flag_refused(self):
        with self.assertRaises(common.Error):
            pbxctl.main(['feature','--config',str(ROOT/'site.example.json'),'--name','secure-calling','--enable','--gain-db','-8'])

    def test_wav_preserves_rate_frames_and_level(self):
        original = wav([12000, -12000, 0, 6000], 48000)
        rendered, rate = features.scale_wav(original, -6)
        params, output = samples(rendered)
        self.assertEqual((rate, params.framerate, params.nframes), (48000, 48000, 4))
        self.assertAlmostEqual(output[0] / 12000, 10 ** (-6 / 20), places=4)
        self.assertEqual(output[1], -output[0])

    def test_clipping_rejected(self):
        with self.assertRaises(common.Error): features.scale_wav(wav([32000]), 6)

    def test_repeated_music_gain_uses_original_and_disable_restores_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); music = root/'music'; folder = music/'tenant'/'8000'; folder.mkdir(parents=True)
            track = folder/'song.wav'; original = wav([14000, -13000, 6000]); track.write_bytes(original)
            c = sample(); c['hold_music'].update(enabled=True, directory=str(folder.parent), stream='tenant', gain_db=-6)
            with patch.object(features,'STATE',root/'state'), patch.object(features,'MUSIC_ROOT',music), patch.object(features,'refresh_music'):
                features.hold_music(None,c,FileChange(root/'first'))
                c['hold_music']['gain_db']=-8
                features.hold_music(None,c,FileChange(root/'second'))
                self.assertEqual(track.read_bytes(), features.scale_wav(original,-8)[0])
                features.hold_music(None,c,FileChange(root/'third'))
                self.assertEqual(track.read_bytes(), features.scale_wav(original,-8)[0])
                c['hold_music']['enabled']=False
                features.hold_music(None,c,FileChange(root/'fourth'))
                self.assertEqual(track.read_bytes(),original)

    def test_carrier_disabled_fresh_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(features,'STATE',Path(tmp)):
            db=Mock();ch=Mock()
            self.assertEqual(features.carrier_tls(db,sample(),ch)['status'],'already disabled')
            ch.row.assert_not_called();db.one.assert_not_called()

    def test_carrier_requires_portal_readiness_before_changes(self):
        c=sample();c['carrier_tls']['enabled']=True
        with tempfile.TemporaryDirectory() as tmp, patch.object(features,'STATE',Path(tmp)):
            ch=Mock()
            with self.assertRaises(common.Error):features.carrier_tls(Mock(),c,ch)
            ch.row.assert_not_called()

    def test_carrier_disable_restores_exact_selected_originals(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(features,'STATE',Path(tmp)):
            rows=[['v_gateways','gateway_uuid',{'gateway_uuid':'fixture','proxy':'old.example.com','register_transport':None}]]
            (Path(tmp)/'carrier-tls-baseline.json').write_text(json.dumps({'profile':'external','rows':rows}))
            ch=Mock();r=features.carrier_tls(Mock(),sample(),ch)
            ch.row.assert_called_once_with(*rows[0]);self.assertEqual(r['restart_profile'],'external')

    def test_security_rule_updates_same_uuid_and_disables_without_touching_native(self):
        db=Mock();db.rows.return_value=[];db.one.return_value={'domain_uuid':'11111111-1111-4111-8111-111111111111'}
        c=sample();ch=Mock();conditions=[('destination_number','^600$',[('export','nolocal:rtp_secure_media_outbound=optional')])]
        features.rule(db,c,ch,'fixture',conditions,True)
        first=ch.row.call_args_list[0].args[2]
        ch.reset_mock();features.rule(db,c,ch,'fixture',conditions,False)
        second=ch.row.call_args_list[0].args[2]
        self.assertEqual(first['dialplan_uuid'],second['dialplan_uuid']);self.assertFalse(second['dialplan_enabled'])
        self.assertEqual(first['dialplan_context'],c['domain'])

    def test_carrier_firewall_only_specific_sources_and_tcp(self):
        c=sample();c['provider_cidrs']=['192.0.2.1/32'];c['carrier_tls']['enabled']=True
        tls=[r for r in firewall.rules(c,4) if '5081' in r]
        self.assertEqual(len(tls),1);self.assertIn('192.0.2.1/32',tls[0]);self.assertIn('tcp',tls[0]);self.assertNotIn('ACCEPT',tls[0])

    def test_backups_and_updates_cover_summary_runtime_and_jobs(self):
        self.assertIn('/opt/pbxctl-ai',recovery.roots(sample()))
        for unit in ('pbxctl-ai-summary.timer','pbxctl-ai-summary.service','pbxctl-ai-model.service'):
            self.assertIn(unit,recovery.QUIET_UNITS)

if __name__=='__main__':unittest.main(verbosity=2)
