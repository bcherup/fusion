"""Direct volume controls against isolated music, state and recovery files."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pbxctl
from lib import common, features
if os.name=='nt':
    # Unix account operations are never exercised by this offline file fixture.
    sys.modules.setdefault('grp',Mock());sys.modules.setdefault('pwd',Mock())
from lib import quick_volume as quick
from test_features import wav, sample


class Inventory:
    def __init__(self, directory):
        self.domains=[{'domain_uuid':'11111111-1111-4111-8111-111111111111','domain_name':'voip.example.com'}]
        self.music=[{'domain_uuid':self.domains[0]['domain_uuid'],'music_on_hold_name':'Office',
                     'music_on_hold_path':str(directory/'8000'),'music_on_hold_rate':8000}]
        self.used=[{'selection':'local_stream://voip.example.com/Office'}]
        self.custom=[];self.execute=Mock();self.dump=Mock(side_effect=lambda path:path.write_bytes(b'synthetic dump'))
    def rows(self, sql):
        if 'FROM v_domains' in sql:return copy.deepcopy(self.domains)
        if 'FROM v_music_on_hold' in sql:return copy.deepcopy(self.music)
        if 'AS selection' in sql:return copy.deepcopy(self.used)
        if 'SELECT extension FROM' in sql:return [{'extension':'1000'},{'extension':'1001'}]
        if 'SELECT ring_group_extension' in sql:return [{'ring_group_extension':'600'}]
        if 'FROM v_sip_profiles' in sql:return [{'sip_profile_uuid':'profile'}]
        if 'SELECT DISTINCT p.dialplan_uuid' in sql:return self.custom
        raise AssertionError(sql)


class QuickVolumeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.music=self.root/'music';self.folder=self.music/'tenant'/'Office'
        (self.folder/'8000').mkdir(parents=True)
        self.track=self.folder/'8000'/'song.wav';self.original=wav([12000,-14000,0,9000]);self.track.write_bytes(self.original)
        self.state=self.root/'state';self.config=self.root/'site.json';self.db=Inventory(self.folder)
        old_umask=os.umask(0o022);self.addCleanup(os.umask,old_umask)
        for module,key,value in [(quick,'STATE',self.state),(quick,'CONFIG',self.config),(features,'STATE',self.state),
                                  (features,'MUSIC_ROOT',self.music),(common,'BACKUPS',self.root/'backups')]:
            p=patch.object(module,key,value);p.start();self.addCleanup(p.stop)
        p=patch.object(features,'refresh_music');self.refresh=p.start();self.addCleanup(p.stop)
        if os.name!='posix':
            p=patch.object(os,'chown',create=True);p.start();self.addCleanup(p.stop)
    def volume(self):return quick.Volume(db=self.db)
    def apply(self, **kwargs):
        v=self.volume();plan=v.plan('hold-music',**kwargs);return v.apply(plan,plan['token'])

    def test_current_and_preview_do_not_create_site_state_or_backup(self):
        v=self.volume();state=v.current('hold-music');plan=v.plan('hold-music',gain=-1)
        self.assertIsNone(state['gain_db']);self.assertIn('not yet tracked',state['label'])
        self.assertIn('current tracks',plan['baseline_note']);self.assertEqual(plan['config']['hold_music']['gain_db'],-1)
        self.assertFalse(self.state.exists());self.assertFalse(self.config.exists());self.db.dump.assert_not_called();self.db.execute.assert_not_called()

    def test_first_change_repeat_and_restore_preserve_exact_originals(self):
        self.apply(gain=-1)
        self.assertEqual(self.track.read_bytes(),features.scale_wav(self.original,-1)[0])
        self.assertEqual(self.volume().current('hold-music')['gain_db'],-1)
        self.apply(gain=-3);self.apply(gain=-3)
        self.assertEqual(self.track.read_bytes(),features.scale_wav(self.original,-3)[0])
        self.apply(restore=True)
        self.assertEqual(self.track.read_bytes(),self.original)
        self.assertEqual(self.volume().current('hold-music')['gain_db'],0)
        self.assertFalse(self.config.exists());self.assertEqual(self.db.dump.call_count,4)

    def test_stale_review_refused_before_backup_or_change(self):
        token=self.volume().plan('hold-music',gain=-1)['token']
        edited=wav([5000]);self.track.write_bytes(edited)
        v=self.volume()
        with self.assertRaisesRegex(common.Error,'changed since review'):v.apply(v.plan('hold-music',gain=-1),token)
        self.db.dump.assert_not_called();self.assertEqual(self.track.read_bytes(),edited)

    def test_external_edit_and_new_track_both_require_review(self):
        self.apply(gain=-1);changed=self.track.read_bytes();self.track.write_bytes(wav([2000]))
        with self.assertRaisesRegex(common.Error,'Managed file changed'):self.volume().current('hold-music')
        self.track.write_bytes(changed);(self.track.parent/'extra.wav').write_bytes(self.original)
        with self.assertRaisesRegex(common.Error,'track set changed'):self.volume().current('hold-music')

    def test_invalid_gain_and_clipping_never_replace_live_audio(self):
        for value in (float('nan'),float('inf'),-31,7):
            with self.subTest(value=value),self.assertRaises(common.Error):self.volume().plan('hold-music',gain=value)
        self.track.write_bytes(wav([32000]));before=self.track.read_bytes()
        with self.assertRaisesRegex(common.Error,'clip'):self.apply(gain=6)
        self.assertEqual(self.track.read_bytes(),before);self.assertFalse((self.state/'hold-music.json').exists())
        self.assertFalse((self.state/'hold-music-originals/manifest.json').exists())

    def test_failed_stream_refresh_restores_audio_marker_and_site(self):
        c=sample();self.config.write_text(json.dumps(c));before_config=self.config.read_bytes()
        self.refresh.side_effect=[common.Error('Synthetic stream failure'),None]
        with self.assertRaisesRegex(common.Error,'Synthetic stream failure'):self.apply(gain=-2)
        self.assertEqual(self.track.read_bytes(),self.original);self.assertEqual(self.config.read_bytes(),before_config)
        self.assertFalse((self.state/'hold-music.json').exists());self.assertEqual(self.refresh.call_count,2)

    def test_failure_after_existing_adjustment_restores_previous_record(self):
        self.apply(gain=-1);before=self.track.read_bytes();record=(self.state/'hold-music.json').read_bytes()
        self.refresh.side_effect=[common.Error('Synthetic stream failure'),None]
        with self.assertRaises(common.Error):self.apply(gain=-2)
        self.assertEqual(self.track.read_bytes(),before);self.assertEqual((self.state/'hold-music.json').read_bytes(),record)
        self.assertEqual(self.volume().current('hold-music')['gain_db'],-1)

    def test_recovery_failure_is_reported_with_saved_location(self):
        self.refresh.side_effect=common.Error('Synthetic stream failure')
        with self.assertRaisesRegex(common.Error,'recovery needs attention.*backups'):self.apply(gain=-2)
        self.assertEqual(self.track.read_bytes(),self.original)

    def test_single_used_collection_selected_and_ambiguous_streams_need_choice(self):
        second=self.music/'tenant'/'Second';(second/'8000').mkdir(parents=True);(second/'8000'/'song.wav').write_bytes(self.original)
        self.db.music.append({**self.db.music[0],'music_on_hold_name':'Second','music_on_hold_path':str(second/'8000')})
        self.assertEqual(self.volume().current('hold-music')['stream'],'voip.example.com/Office')
        self.db.used=[]
        self.assertTrue(self.volume().current('hold-music')['selection_required'])
        self.assertEqual(len(self.volume().current('hold-music')['choices']),2)

    def test_unsafe_or_incomplete_catalog_path_not_offered(self):
        self.db.music[0]['music_on_hold_path']=str(self.root)
        self.assertFalse(self.volume().music())
        self.db.music[0]['music_on_hold_path']=str(self.folder/'8000')
        (self.folder/'16000').mkdir();(self.folder/'16000'/'orphan.wav').write_bytes(wav([1000],16000))
        self.assertFalse(self.volume().music())

    def test_multiple_domains_require_explicit_choice(self):
        self.db.domains.append({'domain_uuid':'another','domain_name':'other.example.com'})
        with self.assertRaisesRegex(common.Error,'Choose one'):self.volume()
        self.assertEqual(quick.Volume('voip.example.com',db=self.db).domain,'voip.example.com')

    def test_phone_discovery_scope_limits_and_custom_gain_guard(self):
        v=self.volume();state=v.current('call-volume')
        self.assertEqual(state['extensions'],['1000','1001']);self.assertIn('600',state['destinations'])
        self.assertEqual(v.plan('call-volume',write=-1)['config']['call_volume']['read_level'],0)
        with self.assertRaises(common.Error):v.plan('call-volume',read=5)
        self.db.custom=[{'dialplan_uuid':'custom'}]
        with self.assertRaisesRegex(common.Error,'custom volume rules'):v.current('call-volume')
        self.db.dump.assert_not_called()

    def test_cli_discovers_without_loading_example_site(self):
        with patch('lib.base.supported'),patch.object(quick,'Database',return_value=self.db),patch.object(pbxctl,'load_config',side_effect=AssertionError('Unexpected site file')):
            result=pbxctl.main(['volume','--name','hold-music'])
            plan=pbxctl.main(['volume','--name','hold-music','--gain-db','-1'])
        self.assertIsNone(result['gain_db']);self.assertNotIn('record',result)
        self.assertNotIn('config',plan);self.assertNotIn('state',plan);self.assertIn('token',plan)
        self.db.dump.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
