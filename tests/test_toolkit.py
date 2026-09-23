"""Offline safety and behavior tests; never invoke live host mutations."""
import copy
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from lib import common,config,firewall,mail,offsite,recovery,updates
if os.name=='nt':
    # These offline tests never perform Unix account lookups or service changes.
    with patch.dict(sys.modules,{'grp':Mock(),'pwd':Mock()}):from lib import operations
else:
    from lib import operations
import pbxctl

def sample():return config.load_config(ROOT/'site.example.json')
def ok(stdout=''):return subprocess.CompletedProcess([],0,stdout,'')

class ConfigTests(unittest.TestCase):
    def test_sample(self):self.assertEqual(sample()['domain'],'voip.example.com')
    def test_optional_transcription(self):
        self.assertNotIn('transcription',config.selected('backup,smtp'))
    def test_carrier_can_wait_until_hardening(self):self.assertEqual(sample()['provider_cidrs'],[])
    def test_public_management_refused(self):
        c=sample();c['management_cidrs']=['0.0.0.0/0']
        with self.assertRaises(common.Error):config.validate_all(c)
    def test_wide_carrier_refused(self):
        c=sample();c['provider_cidrs']=['192.0.2.0/24']
        with self.assertRaises(common.Error):config.validate_all(c)
    def test_secret_in_public_config_refused(self):
        c=sample();c['smtp_password']='fixture'
        with self.assertRaises(common.Error):config.validate_all(c)
    def test_plaintext_auth_refused(self):
        c=sample();c['smtp']['security']='none'
        with self.assertRaises(common.Error):config.validate_all(c)
    def test_unauthenticated_relay(self):
        c=sample();c['smtp']['security']='none';c['smtp']['auth']=False;config.validate_all(c)
    def test_header_injection_refused(self):
        c=sample();c['smtp']['from_name']='PBX\r\nBcc: person@example.com'
        with self.assertRaises(common.Error):config.validate_all(c)
    def test_credential_path_traversal_refused(self):
        with self.assertRaises(common.Error):config.private_path('/etc/pbxctl/secrets/../../shadow')
    def test_repository_argument_injection_refused(self):
        c=sample();c['remote_backup']['repository']='--password-command=anything'
        with self.assertRaises(common.Error):config.validate_all(c)
    def test_inline_s3_password_refused(self):
        c=sample();c['remote_backup']['repository']='s3:https://user:password@host/bucket'
        with self.assertRaises(common.Error):config.validate_all(c)
    def test_remote_skip(self):self.assertFalse(sample()['remote_backup']['enabled'])
    def test_duplicate_modules_refused(self):
        with self.assertRaises(common.Error):config.selected('smtp,smtp')
    def test_domain_command_injection_refused(self):
        c=sample();c['domain']='example.com;id'
        with self.assertRaises(common.Error):config.validate_all(c)
    def test_module_independence(self):
        a=sample();b=copy.deepcopy(a);b['smtp']['host']='smtp.example.com'
        self.assertEqual(config.module_config(a,'audio'),config.module_config(b,'audio'))

class MailTests(unittest.TestCase):
    def ready(self):
        c=sample();c['smtp'].update(host='smtp.example.com',username='sender@example.com',from_address='sender@example.com',recipient='recipient@example.com');return c
    def test_starttls_before_password(self):
        events=[];client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
        client.ehlo.side_effect=lambda:events.append('hello');client.starttls.side_effect=lambda **kw:events.append('tls');client.login.side_effect=lambda *x:events.append('login');client.send_message.return_value={}
        with patch.object(mail.smtplib,'SMTP',return_value=client),patch.object(mail,'read_secret',return_value='fixture'):
            mail.send(self.ready(),'Test','body')
        self.assertLess(events.index('tls'),events.index('login'))
    def test_implicit_tls(self):
        c=self.ready();c['smtp']['security']='tls';client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False);client.send_message.return_value={}
        with patch.object(mail.smtplib,'SMTP_SSL',return_value=client) as factory,patch.object(mail,'read_secret',return_value='fixture'):
            mail.send(c,'Test','body');self.assertTrue(factory.call_args.kwargs['context'].check_hostname)
        client.starttls.assert_not_called()
    def test_relay_never_reads_password(self):
        c=self.ready();c['smtp']['auth']=False;client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False);client.send_message.return_value={}
        with patch.object(mail.smtplib,'SMTP',return_value=client),patch.object(mail,'read_secret') as secret:
            mail.send(c,'Test','body');secret.assert_not_called();client.login.assert_not_called()

class ArchiveTests(unittest.TestCase):
    def member(self,name,kind=tarfile.REGTYPE,link=''):
        m=tarfile.TarInfo(name);m.type=kind;m.linkname=link;return m
    def test_traversal_refused(self):
        with self.assertRaises(common.Error):recovery.safe_member(self.member('etc/../root/x'),['/etc'])
    def test_absolute_refused(self):
        with self.assertRaises(common.Error):recovery.safe_member(self.member('/etc/x'),['/etc'])
    def test_device_refused(self):
        with self.assertRaises(common.Error):recovery.safe_member(self.member('etc/x',tarfile.CHRTYPE),['/etc'])
    def test_link_escape_refused(self):
        with self.assertRaises(common.Error):recovery.safe_member(self.member('etc/pbx/x',tarfile.SYMTYPE,'../../../root/.ssh'),['/etc/pbx'])
    def test_link_within_roots(self):
        self.assertEqual(recovery.safe_member(self.member('etc/pbx/x',tarfile.SYMTYPE,'config'),['/etc/pbx']),'/etc/pbx/x')
    def test_destination_credentials_protected(self):
        for name in ('/etc/fusionpbx/config.conf','/etc/fusionpbx/config.php','/usr/share/freeswitch/scripts/resources/functions/config.lua'):
            self.assertTrue(recovery.protected(name,sample()))
    def test_scratch_restore_failure_cleans_owned_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'database.dump').write_bytes(b'fixture')
            with patch.object(recovery,'run',return_value=ok()) as command,patch.object(recovery.subprocess,'run',return_value=subprocess.CompletedProcess([],1)):
                with self.assertRaises(common.Error):recovery.scratch_restore(p,sample())
                calls=[x.args[0] for x in command.call_args_list]
                self.assertEqual(calls[0][-1],calls[-1][-1]);self.assertIn('dropdb',calls[-1]);self.assertTrue(calls[-1][-1].startswith('pbxctl_verify_'))

class FirewallTests(unittest.TestCase):
    def test_guard_never_accepts_around_bans(self):
        for family in (4,6):self.assertTrue(all('ACCEPT' not in x for x in firewall.rules(sample(),family)))
    def test_guard_final_drop(self):self.assertEqual(firewall.rules(sample(),4)[-1],['-j','DROP'])
    def test_fresh_baseline_uses_same_restrictions(self):
        a=firewall.rules(sample(),4);b=firewall.rules(sample(),4,True)
        self.assertEqual([[('ACCEPT' if t=='RETURN' else t) for t in r] for r in a],b)
    def test_ipv6_no_public_sip(self):
        for row in firewall.rules(sample(),6):
            if '--dport' in row or '--dports' in row:self.assertIn('-s',row)
    def test_dynamic_bans_excluded_only_from_boot(self):
        s='*filter\n:f2b-auth - [0:0]\n:sip-auth-ip - [0:0]\n-A INPUT -j f2b-auth\n-A f2b-auth -j RETURN\n-A INPUT -j sip-auth-ip\nCOMMIT\n'
        result=firewall.strip_dynamic(s);self.assertNotIn('f2b-auth',result);self.assertIn('sip-auth-ip',result)

class UpdateTests(unittest.TestCase):
    def release(self,path,text='original'):
        path.mkdir(parents=True,exist_ok=True)
        for name,value in {'VERSION':text,'pbxctl.py':text,'lib/runtime.py':text}.items():
            f=path/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_text(value)
        files={str(p.relative_to(path)).replace('\\','/'):common.digest(p) for p in path.rglob('*') if p.is_file()}
        (path/'MANIFEST.json').write_text(json.dumps({'sha256':files}))
    def test_tool_update_preserves_previous_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);old=p/'installed';new=p/'candidate';self.release(old);self.release(new,'candidate')
            with patch.object(updates,'ROOT',old):result=updates.tool(new,True)
            self.assertEqual((old/'VERSION').read_text(),'candidate')
            self.assertEqual((Path(result['previous_toolkit'])/'VERSION').read_text(),'original')
    def test_tool_update_refuses_local_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);old=p/'installed';new=p/'candidate';self.release(old);self.release(new,'candidate')
            (old/'pbxctl.py').write_text('local edit')
            with patch.object(updates,'ROOT',old),self.assertRaises(common.Error):updates.tool(new,True)
            self.assertEqual((old/'pbxctl.py').read_text(),'local edit')
    def test_incomplete_candidate_leaves_installed_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);old=p/'installed';new=p/'candidate';self.release(old);self.release(new,'candidate')
            (new/'lib/runtime.py').unlink()
            with patch.object(updates,'ROOT',old),self.assertRaises(common.Error):updates.tool(new,True)
            self.assertEqual((old/'VERSION').read_text(),'original')
    def test_failed_directory_swap_restores_previous_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);old=p/'installed';new=p/'candidate';self.release(old);self.release(new,'candidate')
            rename=Path.rename
            def fail_candidate(path,target):
                if path.name.startswith('.pbxctl-release-'):raise OSError('fixture failure')
                return rename(path,target)
            with patch.object(updates,'ROOT',old),patch.object(Path,'rename',fail_candidate),self.assertRaises(OSError):updates.tool(new,True)
            self.assertEqual((old/'VERSION').read_text(),'original')
    def test_automatic_rollback_keeps_confirmed_firewall(self):
        with patch.object(firewall,'transaction',return_value=(Path('/fixture'),{'confirmed':True})),patch.object(firewall,'run') as command:
            firewall.rollback('/fixture');command.assert_not_called()
    def test_manual_firewall_rollback_checks_drift(self):
        with patch.object(firewall,'transaction',return_value=(Path('/fixture'),{'confirmed':True,'config':sample()})),patch.object(firewall,'check',side_effect=common.Error('drift')) as check:
            with self.assertRaises(common.Error):firewall.rollback('/fixture',automatic=False)
            check.assert_called_once()
    def test_dirty_repo_stops_before_fetch(self):
        with patch.object(updates,'repositories',return_value=[Path('/fixture')]),patch.object(updates,'git',return_value=' M file') as g:
            with self.assertRaises(common.Error):updates.plan(sample(),True)
            self.assertEqual(g.call_count,1)
    def test_diverged_history_refused(self):
        answers=['','5.5','origin','refs/heads/5.5','https://github.com/fusionpbx/fusionpbx.git','target','head']
        with patch.object(updates,'repositories',return_value=[Path('/fixture')]),patch.object(updates,'git',side_effect=answers),patch.object(updates,'run',return_value=subprocess.CompletedProcess([],1)):
            with self.assertRaises(common.Error):updates.plan(sample())
    def test_foreign_remote_refused(self):
        answers=['','5.5','origin','refs/heads/5.5','https://example.invalid/untrusted.git']
        with patch.object(updates,'repositories',return_value=[Path('/fixture')]),patch.object(updates,'git',side_effect=answers):
            with self.assertRaises(common.Error):updates.plan(sample(),True)
    def test_release_traversal_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'VERSION').write_text('1');(p/'pbxctl.py').write_text('')
            (p/'MANIFEST.json').write_text(json.dumps({'sha256':{'../outside':'bad'}}))
            with self.assertRaises(common.Error):updates.validate_release(p)

class NatUpgradeTests(unittest.TestCase):
    def exercise(self,revision=None,module='hardening',fail_restart=False):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);state=root/'state';state.mkdir();backup=root/'change';backup.mkdir()
            c=sample();c['provider_cidrs']=['192.0.2.10/32']
            active=root/'active.json';active.write_text(json.dumps(c))
            previous={'module':'hardening','desired':config.module_config(c,'hardening'),'backup':str(backup)}
            if revision is not None:previous['revision']=revision
            marker=state/'hardening.json';marker.write_text(json.dumps(previous));original=marker.read_bytes()
            change=Mock(path=backup,actions=[])
            for obj,name,value in [(operations.base,'supported',Mock()),(operations,'Database',Mock()),
                    (operations.pbx,'domain',Mock()),(operations,'ROOT',ROOT),(operations,'CONFIG',active),
                    (operations,'STATE',state),(operations,'Change',Mock(return_value=change)),
                    (operations,'check_managed',Mock(return_value=previous)),(operations,'save_managed',Mock()),
                    (operations,'idle',Mock()),(operations.services,'backup',Mock())]:
                stack.enter_context(patch.object(obj,name,value))
            hardening=stack.enter_context(patch.object(operations.pbx,'hardening'))
            invalidate=stack.enter_context(patch.object(operations,'invalidate'))
            rollback=stack.enter_context(patch.object(operations,'rollback_change'))
            restarts=[]
            def command(args,**kwargs):
                if args==['systemctl','restart','freeswitch']:
                    restarts.append(args)
                    if fail_restart and len(restarts)==1:raise common.Error('Synthetic activation failure')
                return ok()
            stack.enter_context(patch.object(operations,'run',side_effect=command))
            if fail_restart:
                with self.assertRaises(common.Error):operations.configure(c,[module],True)
                self.assertEqual(marker.read_bytes(),original)
                self.assertEqual(json.loads(active.read_text()),c)
                rollback.assert_called_once_with(change)
                self.assertEqual(len(restarts),2)
                self.assertEqual(invalidate.call_count,2)
                return
            result=operations.configure(c,[module],True)
            if module!='hardening':
                hardening.assert_not_called();self.assertEqual(marker.read_bytes(),original)
            elif revision==operations.MODULE_REVISIONS['hardening']:
                self.assertEqual(result[0]['status'],'unchanged');hardening.assert_not_called()
                self.assertFalse(restarts)
            else:
                self.assertEqual(result[0]['status'],'configured');hardening.assert_called_once()
                self.assertEqual(json.loads(marker.read_text())['revision'],operations.MODULE_REVISIONS['hardening'])
                self.assertEqual(len(restarts),1)

    def test_old_hardening_marker_gets_new_defaults(self):self.exercise()
    def test_current_hardening_revision_is_unchanged(self):self.exercise(operations.MODULE_REVISIONS['hardening'])
    def test_unselected_hardening_is_not_upgraded(self):self.exercise(module='backup')
    def test_failed_activation_restores_and_reloads_prior_settings(self):self.exercise(fail_restart=True)

    def test_hostname_specific_profile_cache_is_invalidated(self):
        with tempfile.TemporaryDirectory() as tmp:
            mkstemp=tempfile.mkstemp;scripts=[]
            def command(args,**kwargs):
                if args==['fs_cli','-x','hostname']:return ok('pbx.example.test\n')
                if args[2].startswith('lua '):
                    scripts.append(Path(args[2][4:]).read_text());return ok('cache invalidated')
                if args[2]=='reloadxml':return ok('+OK')
                raise AssertionError(args)
            with patch.object(common.tempfile,'mkstemp',side_effect=lambda **kw:mkstemp(prefix=kw['prefix'],suffix=kw['suffix'],dir=tmp)),patch.object(common,'run',side_effect=command):
                common.invalidate(sample(),['configuration:sofia.conf','configuration:acl.conf','directory:1000@voip.example.com'])
            self.assertIn('c.del("pbx.example.test:configuration:sofia.conf")',scripts[0])
            self.assertIn('c.del("configuration:acl.conf")',scripts[0])
            self.assertIn('c.del("directory:1000@voip.example.com")',scripts[0])
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_missing_hostname_stops_cache_activation(self):
        with patch.object(common,'run',return_value=ok('-ERR unavailable')) as command:
            with self.assertRaises(common.Error):common.invalidate(sample(),['configuration:sofia.conf'])
            command.assert_called_once()

class CliTests(unittest.TestCase):
    def test_setup_refuses_active_config_overwrite(self):
        with patch.object(pbxctl,'wizard') as wizard:
            with self.assertRaises(common.Error):pbxctl.main(['setup','--config',str(pbxctl.CONFIG)])
            wizard.assert_not_called()
    def test_new_install_uses_saved_local_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=os.getcwd()
            try:
                os.chdir(tmp);Path('site.json').write_text(json.dumps(sample()))
                with patch.object(pbxctl,'load_config',wraps=pbxctl.load_config) as load:
                    pbxctl.main(['plan'])
                    self.assertEqual(load.call_args.args[0],'site.json')
            finally:os.chdir(old)
    def test_configure_defaults_to_plan(self):
        with patch('lib.base.supported') as privileged:
            result=pbxctl.main(['configure','--config',str(ROOT/'site.example.json'),'--modules','smtp'])
            self.assertTrue(result['apply_required']);privileged.assert_not_called()
    def test_update_option_available(self):self.assertEqual(pbxctl.parser().parse_args(['update']).target,'pbx')
    def test_media_only_reads_nonsecret_fields(self):
        with patch.object(pbxctl,'run',side_effect=[ok('true'),ok('true'),ok('AES_CM_128_HMAC_SHA1_80'),ok('internal'),ok('OPUS'),ok('OPUS')]) as r:
            result=pbxctl.media('12345678-1234-1234-1234-123456789012')
            self.assertTrue(result['encrypted_audio_confirmed'])
            self.assertTrue(all('uuid_dump' not in str(x) for x in r.call_args_list))
    def test_media_policy_not_treated_as_confirmation(self):
        with patch.object(pbxctl,'run',side_effect=[ok('true'),ok('_undef_'),ok('_undef_'),ok('internal'),ok('OPUS'),ok('OPUS')]):
            self.assertFalse(pbxctl.media('12345678-1234-1234-1234-123456789012')['encrypted_audio_confirmed'])
    def test_active_uuid_required(self):
        with self.assertRaises(common.Error):pbxctl.media('anything; shutdown')

if __name__=='__main__':unittest.main(verbosity=2)
