"""Interactive navigation, configuration review and terminal regression tests."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pbxctl
from lib import console, config
from lib.common import Error
from test_diagnostics import sample_report

SOURCE=Path(__file__).resolve().parents[1]


class FakeUI:
    def __init__(self, selections=(), prompts=(), confirmations=()):
        self.selections=iter(selections);self.prompts=iter(prompts);self.confirmations=iter(confirmations)
        self.pages=[];self.context='';self.external=Mock(side_effect=lambda callback:callback())
    def select(self,title,items,note='',summary=()):
        self.pages.append((title,{'items':items,'summary':summary,'note':note}))
        key=next(self.selections)
        assert key is None or key in [x[0] for x in items],(title,key)
        return key
    def prompt(self,*args):return next(self.prompts)
    def confirm(self,*args):return next(self.confirmations)
    def busy(self,*args):pass
    def view(self,title,text):self.pages.append((title,text))


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'site.json';self.path.write_text((SOURCE/'site.example.json').read_text())
        self.report=sample_report()
    def app(self,ui,runner=None):
        return console.Console(ui,runner or Mock(return_value=self.report),SOURCE,config=self.path)

    def test_navigation_returns_to_menu_and_only_scans(self):
        ui=FakeUI(['advanced','audio','view',None,None,'system','overview','findings','refresh',None,'exit']);runner=Mock(return_value=self.report)
        self.app(ui,runner).run()
        self.assertEqual(len(runner.call_args_list),2)
        self.assertTrue(all(c.args[0][0]=='status' and '--apply' not in c.args[0] for c in runner.call_args_list))
        self.assertTrue(any('Audio' in p[0] for p in ui.pages));self.assertTrue(any(p[0]=='Observed system settings' for p in ui.pages))

    def test_cancel_after_preview_never_applies(self):
        ui=FakeUI(confirmations=[False]);runner=Mock(return_value={'action':'backup','apply_required':True})
        self.app(ui,runner).perform('backup')
        self.assertEqual(runner.call_count,1);self.assertNotIn('--apply',runner.call_args.args[0])

    def test_apply_uses_reviewed_config_and_refreshes(self):
        ui=FakeUI(confirmations=[True]);runner=Mock(side_effect=[{'action':'backup'},{'backup':'/private/recovery'},self.report])
        self.app(ui,runner).perform('backup')
        self.assertEqual(runner.call_args_list[1].args[0],['backup','--config',str(self.path),'--apply'])
        self.assertEqual(runner.call_args_list[2].args[0][0],'status')

    def test_restart_requires_separate_maintenance_acknowledgment(self):
        ui=FakeUI(confirmations=[True,False]);runner=Mock(return_value={'action':'configure'})
        self.app(ui,runner).perform('configure',['--modules','audio'],restart=True)
        self.assertEqual(runner.call_count,1)

    def test_credential_is_not_requested_as_a_preview(self):
        ui=FakeUI(confirmations=[False]);runner=Mock()
        self.app(ui,runner).perform('smtp-credential',preview=False)
        runner.assert_not_called();ui.external.assert_not_called()

    def test_confirmed_credential_entry_uses_private_terminal_prompt(self):
        ui=FakeUI(confirmations=[True]);runner=Mock(side_effect=[{'saved':True},self.report])
        self.app(ui,runner).perform('smtp-credential',preview=False)
        ui.external.assert_called_once();self.assertIn('--apply',runner.call_args_list[0].args[0])

    def test_editor_cancel_does_not_write_or_apply(self):
        before=self.path.read_bytes();ui=FakeUI(['call_volume.write_level',None],prompts=['-2']);runner=Mock()
        self.assertFalse(self.app(ui,runner).edit('call-volume'))
        self.assertEqual(self.path.read_bytes(),before);runner.assert_not_called()

    def test_editor_save_is_draft_only_with_fractional_music_gain(self):
        candidate=Path(self.temp.name)/'candidate.json';ui=FakeUI(['hold_music.gain_db','save'],prompts=['-8.5',str(candidate)]);runner=Mock()
        self.assertTrue(self.app(ui,runner).edit('hold-music'))
        self.assertEqual(config.load_config(candidate)['hold_music']['gain_db'],-8.5);runner.assert_not_called()
        self.assertEqual(config.load_config(self.path)['hold_music']['gain_db'],-8)

    def test_editor_does_not_overwrite_active_configuration(self):
        before=self.path.read_bytes();ui=FakeUI(['save',None],prompts=[str(self.path)])
        with patch.object(console,'CONFIG',self.path):self.assertFalse(self.app(ui).edit('site'))
        self.assertEqual(self.path.read_bytes(),before)

    def test_wrong_domain_blocks_mutating_operation(self):
        runner=Mock();app=self.app(FakeUI(),runner);app.domain='other.example.com'
        with self.assertRaises(Error):app.perform('backup')
        runner.assert_not_called()

    def test_export_refuses_existing_file(self):
        ui=FakeUI(['html'],prompts=[str(self.path)]);app=self.app(ui);app.report=self.report
        before=self.path.read_bytes()
        with self.assertRaises(Error):app.export()
        self.assertEqual(self.path.read_bytes(),before)

    def test_export_is_private_and_does_not_call_backend(self):
        target=Path(self.temp.name)/'report.html';ui=FakeUI(['html'],prompts=[str(target)]);runner=Mock();app=self.app(ui,runner);app.report=self.report
        app.export();self.assertIn('PBX Toolkit',target.read_text(encoding='utf-8'));runner.assert_not_called()
        if os.name=='posix':self.assertEqual(target.stat().st_mode&0o777,0o600)

    def test_cli_passes_site_scope_to_console(self):
        with patch.object(console,'launch') as launch:
            pbxctl.main(['menu','--domain','voip.example.com','--config',str(self.path),'--database','example'])
        launch.assert_called_once_with(pbxctl.main,pbxctl.SOURCE,str(self.path),'voip.example.com','example')

    def test_noninteractive_console_gives_actionable_error(self):
        with patch('lib.base.supported'),patch('sys.stdin.isatty',return_value=False):
            with self.assertRaisesRegex(Error,'interactive SSH terminal'):console.launch(Mock(),SOURCE)

    def test_modules_are_selected_before_review_and_apply(self):
        ui=FakeUI(['hold-music','ai-summary','review'],confirmations=[False]);runner=Mock(return_value={'proposed':{}})
        self.app(ui,runner).modules()
        self.assertEqual(runner.call_args.args[0][-2:],['--modules','hold-music,ai-summary'])
        self.assertNotIn('--apply',runner.call_args.args[0])

    def test_simple_music_change_shows_current_and_applies_one_review_without_site(self):
        current={'stream':'voip.example.com/Office','gain_db':-8,'recorded':True,'label':'-8 dB from preserved tracks','scope':'Office music'}
        preview={'before':current['label'],'after':'-9 dB from preserved tracks','scope':'Office music','baseline_note':'','token':'review-token'}
        ui=FakeUI(['down',None],confirmations=[True])
        runner=Mock(side_effect=[current,preview,{'current':preview['after'],'scope':'Office music'},self.report,{**current,'gain_db':-9,'label':preview['after']}])
        app=console.Console(ui,runner,SOURCE);app.report=self.report
        with patch.object(app,'edit',side_effect=AssertionError('Should not open a configuration editor')):app.simple_volume('hold-music')
        apply=runner.call_args_list[2].args[0]
        self.assertEqual(apply[-5:],['--gain-db','-9','--confirm','review-token','--apply'])
        self.assertNotIn('--config',apply);self.assertTrue(any(p[0]=='Volume updated' for p in ui.pages))

    def test_simple_music_cancel_only_reads_and_previews(self):
        current={'stream':'Office','gain_db':None,'recorded':False,'label':'Existing track level','scope':'Office music'}
        preview={'before':current['label'],'after':'-1 dB','scope':'Office music','baseline_note':'Preserve tracks','token':'review-token'}
        runner=Mock(side_effect=[current,preview,current]);ui=FakeUI(['down',None],confirmations=[False])
        self.app(ui,runner).simple_volume('hold-music')
        self.assertEqual(runner.call_count,3);self.assertFalse(any('--apply' in c.args[0] for c in runner.call_args_list))

    def test_simple_phone_listening_change_preserves_microphone_level(self):
        current={'read_level':-2,'write_level':0,'recorded':True,'label':'Microphone -2 / listening +0 steps','scope':'Phones 1000, 1001'}
        preview={'before':current['label'],'after':'Microphone -2 / listening -1 steps','scope':current['scope'],'baseline_note':'','token':'review-token'}
        runner=Mock(side_effect=[current,preview,current]);ui=FakeUI(['listen-down',None],confirmations=[False])
        self.app(ui,runner).simple_volume('call-volume')
        self.assertEqual(runner.call_args_list[1].args[0][-4:],['--read-level','-2','--write-level','-1'])

    def test_confirmation_is_readable_at_80_columns_and_defaults_to_cancel(self):
        constants={name:i+100 for i,name in enumerate(('KEY_UP','KEY_DOWN','KEY_LEFT','KEY_RIGHT','KEY_ENTER','KEY_NPAGE','KEY_PPAGE','KEY_HOME','KEY_END','A_DIM'))}
        curses=type('Keys',(),constants)();window=Mock();window.getch.side_effect=[13]
        screen=console.Screen.__new__(console.Screen);screen.w=window;screen.c=curses;screen.selected=1
        screen.frame=Mock(return_value=(24,80));screen.put=Mock()
        text='Current: -8 dB\nNew: -9 dB\nApplies to: Office music'
        self.assertFalse(screen.confirm('Change volume?',text))
        rendered=[call.args[2] for call in screen.put.call_args_list]
        self.assertIn('Current: -8 dB',rendered);self.assertIn('New: -9 dB',rendered);self.assertIn('Applies to: Office music',rendered)
        window.getch.side_effect=[curses.KEY_END,curses.KEY_RIGHT,13];screen.put.reset_mock()
        self.assertTrue(screen.confirm('Long review','\n'.join('Line '+str(i) for i in range(50))))
        self.assertIn('Line 49',[call.args[2] for call in screen.put.call_args_list])

    @unittest.skipUnless(os.name=='posix','Requires a Unix pseudo-terminal')
    def test_real_curses_arrow_navigation_and_terminal_restore(self):
        import fcntl
        import pty
        import select
        import struct
        import subprocess
        import termios
        import time
        master,slave=pty.openpty();self.addCleanup(os.close,master);self.addCleanup(os.close,slave)
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,110,0,0))
        before=termios.tcgetattr(slave)
        code="""import curses,sys
sys.path.insert(0,'tests')
from test_diagnostics import sample_report
from lib.console import Console,Screen
report=sample_report()
def runner(args):
 assert args[0]=='status' and '--apply' not in args
 return report
def app(w):
 screen=Screen(w,curses)
 Console(screen,runner,'.').run()
 assert screen.confirm('Volume review','Current: -8 dB\\nNew: -9 dB\\nApplies to: Office music')
curses.wrapper(app)
print('CONSOLE_EXIT_OK',flush=True)
"""
        process=subprocess.Popen([sys.executable,'-B','-c',code],stdin=slave,stdout=slave,stderr=slave,cwd=SOURCE,env={**os.environ,'TERM':'xterm-256color'})
        self.addCleanup(lambda:process.kill() if process.poll() is None else None)
        data=bytearray()
        def until(needle):
            deadline=time.monotonic()+12
            while needle not in data and time.monotonic()<deadline:
                if select.select([master],[],[],0.1)[0]:data.extend(os.read(master,65536))
            self.assertIn(needle,data,bytes(data[-2000:]))
        until(b'Main menu')
        os.write(master,b'\r');until(b'Change voice codecs')
        until(b'Music adjustment:')
        os.write(master,b'q');time.sleep(.15)
        # Arrow keys in xterm application-cursor mode, then Enter.
        os.write(master,b'\x1bOB'*7+b'\r');until(b'Detailed audio settings')
        os.write(master,b'\r');until(b'Current audio settings')
        os.write(master,b'\r');until(b'Evidence:')
        os.write(master,b'q');time.sleep(.15)
        os.write(master,b'q');time.sleep(.15)
        os.write(master,b'q');time.sleep(.15)
        os.write(master,b'q');until(b'New: -9 dB')
        os.write(master,b'\x1bOC\r');until(b'CONSOLE_EXIT_OK')
        self.assertEqual(process.wait(timeout=3),0)
        after=termios.tcgetattr(slave)
        self.assertEqual(after[3]&(termios.ECHO|termios.ICANON),before[3]&(termios.ECHO|termios.ICANON))


if __name__=='__main__':unittest.main()
