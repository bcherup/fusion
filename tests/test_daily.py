"""Everyday flows, truthful current values, background job recovery and call inspection."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pbxctl
from lib import console, daily, job_control as jobs
from lib.common import Error
from lib.config import load_config
from lib.console_state import Current
from test_console import FakeUI, SOURCE
from test_diagnostics import sample_report


class FakeSystem:
    def __init__(self):
        self.data={name:{'Id':name,'LoadState':'loaded','ActiveState':'active' if name.endswith('.timer') or name=='pbx-whisper.service' else 'inactive','UnitFileState':'enabled'}
                   for name in ('fusionpbx-local-transcribe.timer','fusionpbx-local-transcribe.service','pbx-whisper.service')}
        self.calls=[];self.fail=None
    def run(self,args,**kwargs):
        self.calls.append(args)
        if args[1]=='show':
            names=[x for x in args[2:] if not x.startswith('--')]
            return Mock(stdout='\n\n'.join('\n'.join(k+'='+v for k,v in self.data.get(name,{'Id':name,'LoadState':'not-found','ActiveState':'inactive','UnitFileState':''}).items()) for name in names))
        unit=args[-1];verb=args[1]
        if self.fail and self.fail(args):raise Error('Synthetic service failure')
        if verb in ('enable','disable'):
            self.data[unit]['UnitFileState']='enabled' if verb=='enable' else 'disabled'
            if '--now' in args:self.data[unit]['ActiveState']='active' if verb=='enable' else 'inactive'
        elif verb in ('start','stop'):self.data[unit]['ActiveState']='active' if verb=='start' else 'inactive'
        else:raise AssertionError(args)
        return Mock(stdout='')


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state';p=patch.object(jobs,'STATE',self.path);p.start();self.addCleanup(p.stop)
        self.system=FakeSystem();p=patch.object(jobs,'run',side_effect=self.system.run);p.start();self.addCleanup(p.stop)
    def test_preview_reads_and_pause_resume_preserve_model_and_worker(self):
        before=copy.deepcopy(self.system.data);p=jobs.plan('transcription',False)
        self.assertFalse(self.path.exists());self.assertTrue(all(c[1]=='show' for c in self.system.calls))
        jobs.apply('transcription',False,p['token'])
        self.assertEqual(self.system.data['pbx-whisper.service'],before['pbx-whisper.service'])
        self.assertEqual(self.system.data['fusionpbx-local-transcribe.service'],before['fusionpbx-local-transcribe.service'])
        self.assertEqual(self.system.data['fusionpbx-local-transcribe.timer']['UnitFileState'],'disabled')
        p=jobs.plan('transcription',True);jobs.apply('transcription',True,p['token'])
        self.assertEqual(self.system.data,before)
        self.assertFalse(any('freeswitch' in ' '.join(c) for c in self.system.calls))
    def test_stale_confirmation_does_not_mutate(self):
        p=jobs.plan('transcription',False);self.system.data['fusionpbx-local-transcribe.timer']['ActiveState']='inactive'
        with self.assertRaisesRegex(Error,'changed since review'):jobs.apply('transcription',False,p['token'])
        self.assertTrue(all(c[1]=='show' for c in self.system.calls));self.assertFalse(self.path.exists())
    def test_masked_or_duplicate_jobs_are_refused(self):
        self.system.data['fusionpbx-local-transcribe.timer']['UnitFileState']='masked'
        with self.assertRaises(Error):jobs.plan('transcription',False)
        self.system.data['fusionpbx-local-transcribe.timer']['UnitFileState']='enabled'
        self.system.data['pbxctl-transcribe.timer']={**self.system.data['fusionpbx-local-transcribe.timer'],'Id':'pbxctl-transcribe.timer'}
        with self.assertRaises(Error):jobs.plan('transcription',False)
    def test_failed_resume_restores_prior_model_startup_and_timer(self):
        for name in ('fusionpbx-local-transcribe.timer','pbx-whisper.service'):
            self.system.data[name].update(ActiveState='inactive',UnitFileState='disabled')
        before=copy.deepcopy(self.system.data)
        self.system.fail=lambda args:args[1]=='enable' and '--now' in args and args[-1].endswith('.timer')
        p=jobs.plan('transcription',True)
        with self.assertRaisesRegex(Error,'Synthetic service failure'):jobs.apply('transcription',True,p['token'])
        self.assertEqual(self.system.data,before)
    def test_disabled_feature_must_be_enabled_in_configuration(self):
        self.path.mkdir();(self.path/'transcription.json').write_text(json.dumps({'enabled':False}))
        with self.assertRaisesRegex(Error,'disabled in its configuration'):jobs.plan('transcription',True)
    def test_cli_job_preview_does_not_load_site(self):
        with patch('lib.base.supported'),patch.object(pbxctl,'load_config',side_effect=AssertionError('Unexpected site read')):
            result=pbxctl.main(['job','--name','transcription','--disable'])
        self.assertEqual(result['after'],'Paused');self.assertNotIn('state',result)


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.site=self.root/'site.json';self.site.write_text((SOURCE/'site.example.json').read_text())
        self.report=sample_report()
        for name,value in [('DRAFTS',self.root/'drafts'),('ARCHIVES',self.root/'backups'),('STATE',self.root/'state'),('ROOT',self.root/'toolkit'),('CONFIG',self.root/'active.json')]:
            p=patch.object(daily,name,value);p.start();self.addCleanup(p.stop)
    def app(self,ui,runner=None):
        app=console.Console(ui,runner or Mock(return_value=self.report),SOURCE,config=self.site);app.report=self.report;return app
    def test_each_everyday_page_has_current_values_and_navigation_is_read_only(self):
        for area in ('audio','voicemail','email','backups','security','updates','system'):
            with self.subTest(area=area):
                ui=FakeUI([None]);runner=Mock();app=self.app(ui,runner);app.everyday(area)
                self.assertTrue(ui.pages[-1][1]['summary']);runner.assert_not_called()
    def test_existing_transcription_uses_job_control_without_install_or_config_editor(self):
        ui=FakeUI(['transcription',None],confirmations=[False]);runner=Mock(return_value={'before':'Running','after':'Paused','scope':'Existing job','note':'','token':'fixture'})
        app=self.app(ui,runner)
        with patch.object(app,'working',side_effect=AssertionError('No site setup for job pause')):app.everyday('voicemail')
        self.assertEqual(runner.call_args.args[0],['job','--name','transcription','--disable'])
        self.assertNotIn('--apply',runner.call_args.args[0])
    def test_duplicate_jobs_offer_review_instead_of_installing(self):
        services=next(s for s in self.report['sections'] if s['id']=='services')['rows']
        services.append({'item':'pbxctl-transcribe.timer','observed':'active','evidence':'fixture','saved':None,'state':'observed'})
        app=self.app(FakeUI(['transcription',None]));app.install_voice_feature=Mock();app.everyday('voicemail')
        app.install_voice_feature.assert_not_called()
    def test_cancel_preferences_creates_no_draft_or_backend_change(self):
        ui=FakeUI(confirmations=[False]);runner=Mock();app=self.app(ui,runner)
        self.assertFalse(app.apply_preferences(load_config(self.site),'audio','New audio',restart=True))
        self.assertFalse(daily.DRAFTS.exists());runner.assert_not_called()
    def test_first_apply_deploys_base_then_only_selected_settings(self):
        ui=FakeUI(confirmations=[True]);runner=Mock(side_effect=[{'deployed':True},[],self.report]);app=self.app(ui,runner)
        c=load_config(self.site);c['secure_calling']['enabled']=True
        app.apply_preferences(c,'secure-calling','Offer phone encryption')
        self.assertEqual(runner.call_args_list[0].args[0],['deploy','--config',str(self.site),'--apply'])
        applied=runner.call_args_list[1].args[0]
        self.assertEqual(applied[-3:],['--modules','secure-calling','--apply'])
        self.assertTrue(load_config(applied[2])['secure_calling']['enabled'])
        self.assertEqual(load_config(self.site)['secure_calling']['enabled'],False)
    def test_backup_picker_avoids_entering_paths_and_excludes_partial_copies(self):
        daily.ARCHIVES.mkdir();complete=daily.ARCHIVES/'20260101-complete';complete.mkdir();(complete/'manifest.json').write_text('{}')
        (daily.ARCHIVES/'partial').mkdir()
        app=self.app(FakeUI([str(complete)]));self.assertEqual(app.choose_backup(),str(complete))
        self.assertEqual(len(app.ui.pages[-1][1]['items']),1)
    def test_first_time_setup_rejects_example_network_and_cancel_writes_nothing(self):
        ui=FakeUI(prompts=['voip.example.com','192.0.2.4',None]);app=self.app(ui);app.config=None
        self.assertFalse(app.guided_setup());self.assertFalse(daily.DRAFTS.exists())
    def test_guided_setup_saves_without_applying_and_disables_optional_features(self):
        ui=FakeUI(['1000'],prompts=['voip.example.com','192.0.2.4','192.0.2.0/24','admin@example.com'],confirmations=[True]);runner=Mock();app=self.app(ui,runner);app.config=None
        self.assertTrue(app.guided_setup());c=load_config(app.config)
        self.assertFalse(c['transcription_enabled']);self.assertEqual(c['management_cidrs'],['192.0.2.0/24']);runner.assert_not_called()
        with patch.object(console,'CONFIG',self.root/'missing-active.json'):
            reopened=console.Console(FakeUI(),runner,SOURCE)
        self.assertEqual(reopened.config,app.config)
    def test_observed_state_does_not_turn_missing_evidence_into_off_or_secure(self):
        current=Current({});self.assertEqual(current.transcription(),'Not detected')
        self.assertEqual(current.policy(),'No supported rule found');self.assertEqual(current.music(),'Read current files to measure')
        text=str(Current(self.report).summary('security'));self.assertIn('Check a call',text)
    def test_active_call_check_filters_to_safe_media_fields(self):
        uid='11111111-1111-4111-8111-111111111111'
        with patch('lib.base.supported'),patch.object(pbxctl,'run',return_value=Mock(stdout=json.dumps({'rows':[{'uuid':uid,'secret':'PRIVATE'}]}))),patch.object(pbxctl,'media',return_value={'encrypted_audio_confirmed':True,'read_codec':'OPUS'}) as read:
            result=pbxctl.main(['active-media'])
        read.assert_called_once_with(uid);self.assertNotIn('PRIVATE',json.dumps(result));self.assertTrue(result[0]['encrypted_audio_confirmed'])

    def test_failed_service_probe_requires_review_instead_of_setup(self):
        self.report['findings'].append({'severity':'unknown','area':'Services','message':'Unavailable','recommendation':'Refresh'})
        ui=FakeUI(['transcription',None]);app=self.app(ui);app.install_voice_feature=Mock();app.everyday('voicemail')
        app.install_voice_feature.assert_not_called();self.assertIn('Not verified',str(ui.pages))

    def test_email_cancel_does_not_save_password_or_settings(self):
        ui=FakeUI(prompts=[None]);runner=Mock();app=self.app(ui,runner)
        before=self.site.read_bytes();app.email_setup()
        self.assertEqual(self.site.read_bytes(),before);self.assertFalse(daily.DRAFTS.exists());runner.assert_not_called();ui.external.assert_not_called()

    def test_update_prepares_validation_helper_before_stopping_services(self):
        runner=Mock(side_effect=[{'repositories':[]},{'deployed':True},{'status':'updated'},self.report])
        app=self.app(FakeUI(confirmations=[True]),runner)
        app.simple_operation('update','Install updates',['--fetch'],restart=True)
        actions=[c.args[0] for c in runner.call_args_list]
        self.assertEqual([a[0] for a in actions],['update','deploy','update','status'])
        self.assertNotIn('--apply',actions[0]);self.assertIn('--allow-restart',actions[2])


if __name__=='__main__':unittest.main(verbosity=2)
