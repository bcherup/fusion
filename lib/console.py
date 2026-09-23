"""Interactive terminal console; existing command handlers own all PBX changes."""
import datetime
import json
import os
from pathlib import Path
import sys
import textwrap
from .common import CONFIG, Error, atomic, need
from .config import MODULES, load_config, validate_all
from .diagnostics import clean, overview, render_html, render_text


def lines_for(value, prefix=''):
    """Readable operation results without a JSON dump."""
    if isinstance(value, dict):
        result = []
        for key, child in value.items():
            label = str(key).replace('_', ' ').capitalize()
            if isinstance(child, (dict, list)):
                result += [prefix + label] + lines_for(child, prefix + '  ')
            else: result.append(prefix + label + ': ' + clean(child))
        return result
    if isinstance(value, list):
        return [line for item in value for line in lines_for(item, prefix)]
    return [prefix + clean(value)]


def section_text(report, ids):
    result = []
    for section in report.get('sections', []):
        if section['id'] not in ids: continue
        result += [section['title'].upper(), '']
        for row in section['rows']:
            result += [row['item'] + ': ' + row['observed']]
            if row['saved'] is not None: result += ['  Saved preference: ' + row['saved']]
            result += ['  Evidence: ' + row['evidence'], '']
    return '\n'.join(result) or 'No verified information is available in this category. Refresh the scan or select a domain.'


def wrap_lines(text, width):
    return [part for line in str(text).splitlines() for part in (textwrap.wrap(line, max(10, width), replace_whitespace=False) or [''])]


class Screen:
    """No terminal packages required beyond Debian's standard Python curses."""
    def __init__(self, window, curses):
        self.w = window; self.c = curses; self.context = 'Debian 13 | Select a category to get started'
        self.version = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()
        window.keypad(True)
        try: curses.curs_set(0)
        except curses.error: pass
        self.accent = curses.A_BOLD; self.selected = curses.A_REVERSE | curses.A_BOLD
        if curses.has_colors():
            curses.start_color()
            try: curses.use_default_colors(); background = -1
            except curses.error: background = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_CYAN, background)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
            self.accent |= curses.color_pair(1); self.selected = curses.color_pair(2) | curses.A_BOLD

    def put(self, y, x, text, style=0, width=None):
        h,w = self.w.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w-1: return
        try: self.w.addnstr(y,x,clean(text,10000),min(width or w-x-1,w-x-1),style)
        except self.c.error: pass

    def frame(self, title, footer):
        self.w.erase(); h,w = self.w.getmaxyx()
        self.put(1,2,'PBX TOOLKIT',self.accent)
        self.put(1,max(18,w-21),'CONSOLE  '+self.version,self.c.A_DIM)
        self.put(2,2,self.context,self.c.A_DIM)
        self.put(3,2,'-'*(w-4),self.accent)
        self.put(4,2,title,self.c.A_BOLD)
        self.put(h-3,2,'-'*(w-4),self.accent)
        self.put(h-2,2,footer,self.c.A_DIM)
        return h,w

    def busy(self, title):
        self.frame(title,'Working locally. Please wait for this operation to finish.')
        self.put(7,4,'Reading configuration...' if 'scan' in title.lower() else 'Operation in progress...',self.accent)
        self.w.refresh()

    def select(self, title, items, note=''):
        """Items are (key, label, explanatory text); Escape always goes back."""
        position = 0
        while True:
            h,w = self.frame(title,'Up/Down select  |  Enter open  |  Esc or Q back  |  1-9 shortcuts')
            if h < 18 or w < 60:
                self.put(6,2,'Resize the terminal to at least 60 columns and 18 rows.')
                self.w.refresh(); key = self.w.getch()
                if key in (27,ord('q'),ord('Q')): return None
                continue
            size = max(1,h-12); start = max(0,min(position-size+1,len(items)-size))
            split = w >= 100; left = min(48,w//2) if split else w-5
            for n in range(start,min(len(items),start+size)):
                label = f'{n+1:2}. '+items[n][1]
                self.put(6+n-start,3,label.ljust(left),self.selected if n==position else 0,left)
            if split:
                x = left+6
                detail = wrap_lines(items[position][2]+'\n\n'+note,w-x-3)
                for n,line in enumerate(detail[:h-11]): self.put(6+n,x,line,self.c.A_DIM)
            else:
                self.put(h-5,3,items[position][2],self.c.A_DIM)
                self.put(h-4,3,note,self.c.A_DIM)
            self.w.refresh(); key = self.w.getch()
            if key in (27,ord('q'),ord('Q')): return None
            if key in (self.c.KEY_UP,ord('k')): position = (position-1)%len(items)
            elif key in (self.c.KEY_DOWN,ord('j')): position = (position+1)%len(items)
            elif key == self.c.KEY_HOME: position = 0
            elif key == self.c.KEY_END: position = len(items)-1
            elif key == self.c.KEY_NPAGE: position = min(len(items)-1,position+size)
            elif key == self.c.KEY_PPAGE: position = max(0,position-size)
            elif key in (10,13,self.c.KEY_ENTER): return items[position][0]
            elif ord('1') <= key <= ord('9') and key-ord('1') < len(items):
                position = key-ord('1')

    def view(self, title, text):
        offset = 0
        while True:
            h,w = self.frame(title,'Up/Down scroll  |  PgUp/PgDn page  |  Home/End  |  Enter or Esc back')
            lines = wrap_lines(text,w-6); size = max(1,h-10)
            offset = max(0,min(offset,len(lines)-size))
            for n,line in enumerate(lines[offset:offset+size]): self.put(6+n,3,line)
            self.put(h-4,3,f'Lines {offset+1}-{min(len(lines),offset+size)} of {len(lines)}',self.c.A_DIM)
            self.w.refresh(); key = self.w.getch()
            if key in (27,10,13,self.c.KEY_ENTER,ord('q'),ord('Q')): return
            if key in (self.c.KEY_DOWN,ord('j')): offset += 1
            elif key in (self.c.KEY_UP,ord('k')): offset -= 1
            elif key in (self.c.KEY_NPAGE,ord(' ')): offset += size
            elif key == self.c.KEY_PPAGE: offset -= size
            elif key == self.c.KEY_HOME: offset = 0
            elif key == self.c.KEY_END: offset = len(lines)

    def prompt(self, title, default='', note=''):
        value = str(default); cursor = len(value)
        try: self.c.curs_set(1)
        except self.c.error: pass
        try:
            while True:
                h,w = self.frame(title,'Type to edit  |  Ctrl+U clear  |  Enter accept  |  Esc cancel')
                for n,line in enumerate(wrap_lines(note,w-8)[:max(1,h-13)]): self.put(6+n,4,line,self.c.A_DIM)
                y=h-6; width=max(1,w-9); start=max(0,cursor-width+1)
                self.put(y,3,'> '+value[start:start+width],self.accent)
                try: self.w.move(y,5+cursor-start)
                except self.c.error: pass
                self.w.refresh(); key=self.w.get_wch()
                if key == '\x1b': return None
                if key in ('\n','\r',self.c.KEY_ENTER): return value
                if key in ('\b','\x7f',self.c.KEY_BACKSPACE):
                    if cursor: value=value[:cursor-1]+value[cursor:];cursor-=1
                elif key=='\x15':value='';cursor=0
                elif key==self.c.KEY_LEFT:cursor=max(0,cursor-1)
                elif key==self.c.KEY_RIGHT:cursor=min(len(value),cursor+1)
                elif key==self.c.KEY_HOME:cursor=0
                elif key==self.c.KEY_END:cursor=len(value)
                elif key==self.c.KEY_DC:value=value[:cursor]+value[cursor+1:]
                elif isinstance(key,str) and key.isprintable() and len(value)<2048:
                    value=value[:cursor]+key+value[cursor:];cursor+=1
        finally:
            try:self.c.curs_set(0)
            except self.c.error:pass

    def confirm(self, title, text):
        selected=False;offset=0
        while True:
            h,w=self.frame(title,'Left/Right choose  |  Enter confirm  |  Up/Down scroll  |  Esc cancel')
            if h<18 or w<60:
                self.put(6,2,'Resize the terminal to at least 60 columns and 18 rows.')
                self.w.refresh();key=self.w.getch()
                if key in (27,ord('q'),ord('Q')):return False
                continue
            lines=wrap_lines(text,w-8);size=max(1,h-13)
            offset=max(0,min(offset,len(lines)-size))
            for n,line in enumerate(lines[offset:offset+size]):self.put(6+n,4,line)
            self.put(h-6,4,f'Review {offset+1}-{min(len(lines),offset+size)} of {len(lines)} lines',self.c.A_DIM)
            self.put(h-5,4,'  Cancel  ',self.selected if not selected else 0)
            self.put(h-5,19,'  Apply  ',self.selected if selected else 0)
            self.w.refresh();key=self.w.getch()
            if key in (27,ord('q'),ord('Q')):return False
            if key in (10,13,self.c.KEY_ENTER):return selected
            if key in (self.c.KEY_LEFT,self.c.KEY_RIGHT,9):selected=not selected
            elif key==self.c.KEY_DOWN:offset+=1
            elif key==self.c.KEY_UP:offset-=1
            elif key==self.c.KEY_NPAGE:offset+=size
            elif key==self.c.KEY_PPAGE:offset-=size
            elif key==self.c.KEY_HOME:offset=0
            elif key==self.c.KEY_END:offset=len(lines)

    def external(self, operation):
        self.c.def_prog_mode();self.c.endwin()
        try:return operation()
        finally:self.c.reset_prog_mode();self.w.clear();self.w.refresh()


# Editors change a reviewed site draft; they do not call the PBX backend.
EDITORS = {
    'site':('Site settings', ['domain','nat_hostname','lan_ip','management_cidrs','lan_ipv6_cidrs','provider_cidrs','provider_acl','acme_email','mailbox']),
    'hold-music':('Hold music', ['hold_music.enabled','hold_music.gain_db','hold_music.directory','hold_music.stream']),
    'call-volume':('Phone volume', ['call_volume.enabled','call_volume.read_level','call_volume.write_level','call_volume.extensions','call_volume.destinations']),
    'secure-calling':('Phone encryption', ['secure_calling.enabled','secure_calling.mode','secure_calling.destinations']),
    'carrier-tls':('Carrier encryption', ['carrier_tls.enabled','carrier_tls.gateway_uuid','carrier_tls.route_uuid','carrier_tls.host','carrier_tls.server_port','carrier_tls.listen_port','carrier_tls.portal_ready','provider_cidrs']),
    'transcription':('Voicemail transcription', ['transcription_enabled','mailbox','whisper_port','whisper_cpu_percent','whisper_memory_mb']),
    'ai-summary':('Voicemail summaries', ['ai_summary.enabled','ai_summary.port','ai_summary.cpu_percent','ai_summary.memory_mb']),
    'smtp':('Email delivery', ['smtp.host','smtp.port','smtp.security','smtp.auth','smtp.username','smtp.from_address','smtp.from_name','smtp.recipient']),
    'backup':('Backup preferences', ['backup_min_free_gib','remote_backup.enabled','remote_backup.repository','remote_backup.keep_daily','remote_backup.keep_weekly','remote_backup.keep_monthly']),
}
LABELS = {
    'domain':'SIP domain','nat_hostname':'Public NAT hostname','lan_ip':'PBX LAN IPv4',
    'management_cidrs':'Trusted management networks','lan_ipv6_cidrs':'Trusted IPv6 networks',
    'provider_cidrs':'Verified carrier IPv4 /32 addresses','acme_email':'Certificate contact email',
    'hold_music.gain_db':'Music adjustment (dB)','hold_music.directory':'Music folder','hold_music.stream':'Stream name',
    'call_volume.read_level':'Microphone gain (steps)','call_volume.write_level':'Listening gain (steps)',
    'call_volume.extensions':'Originating extensions','call_volume.destinations':'Answering extensions / groups',
    'secure_calling.destinations':'Extensions / groups','secure_calling.mode':'SRTP policy',
    'carrier_tls.portal_ready':'Provider portal is prepared','smtp.auth':'SMTP authentication',
    'smtp.security':'SMTP security','smtp.from_address':'Sender address','smtp.recipient':'Recipient address',
    'transcription_enabled':'Enable transcription','backup_min_free_gib':'Minimum free disk (GiB)',
}
HELP = {
    'management_cidrs':'Comma-separated trusted management networks, for example 192.168.10.0/24. Verify before applying a firewall.',
    'provider_cidrs':'Only verified carrier IPv4 /32 addresses. Review these before changing carrier security or the firewall.',
    'hold_music.gain_db':'-30 to +6 dB relative to the preserved originals. Negative is quieter. Existing manual adjustments are part of the initial baseline.',
    'hold_music.directory':'One existing folder under /usr/share/freeswitch/sounds/music/. Shared tracks affect every tenant using them.',
    'hold_music.stream':'Existing stream name, for example voip.example.com/office. Omit local_stream:// here.',
    'call_volume.read_level':'-4 to +4 steps. 0 leaves the signal unchanged. Read is phone microphone to PBX.',
    'call_volume.write_level':'-4 to +4 steps. 0 leaves the signal unchanged. Write is PBX audio to the phone.',
    'secure_calling.mode':'Optional allows clear-audio fallback. Mandatory requires compatible SRTP. Verify each leg with an actual call.',
    'carrier_tls.portal_ready':'Confirm the provider media policy and inbound TLS routing are already prepared before enabling this feature.',
    'remote_backup.repository':'SFTP or HTTPS S3 repository, without inline passwords. Credentials stay in private files.',
}


def field_value(config, path):
    value=config
    for part in path.split('.'):value=value[part]
    return value


def set_field(config, path, value):
    keys=path.split('.');parent=config
    for key in keys[:-1]:parent=parent[key]
    old=parent[keys[-1]]
    if path=='hold_music.gain_db':value=float(value)
    elif type(old) is int:value=int(value)
    elif type(old) is float:value=float(value)
    elif isinstance(old,list):value=[x.strip() for x in value.split(',') if x.strip()]
    elif type(old) is bool:need(type(value) is bool,'Choose enabled or disabled')
    parent[keys[-1]]=value


class Console:
    def __init__(self, ui, runner, source, config=None, domain=None, database=None):
        self.ui=ui;self.runner=runner;self.source=Path(source)
        self.config=Path(config) if config else CONFIG if CONFIG.exists() else None
        self.domain=domain;self.database=database;self.report=None

    def scan_args(self):
        args=['status','--format','json']
        if self.config:args+=['--config',str(self.config)]
        if self.domain:args+=['--domain',self.domain]
        if self.database:args+=['--database',self.database]
        return args

    def refresh(self):
        self.ui.busy('Refreshing live scan')
        self.report=self.runner(self.scan_args())
        counts=self.report['counts']
        self.ui.context=(self.report.get('domain') or 'Choose a SIP domain')+' | '+str(counts['error'])+' errors / '+str(counts['warning'])+' warnings | Scan '+self.report['scanned_at'][11:19]+' UTC'

    def show(self, title, ids):self.ui.view(title,section_text(self.report or {},ids))

    def findings(self):
        text='\n\n'.join('['+f['severity'].upper()+'] '+f['area']+'\n'+f['message']+'\nNext: '+f['recommendation'] for f in self.report['findings'])
        self.ui.view('Recommendations',text or 'No issues detected by the completed checks. Review verification limits before relying on this snapshot.')

    def require_config(self):
        if self.config and self.config.is_file():return True
        if not self.ui.confirm('Site configuration required','Create or select a site file before changing settings. Live scanning works without one.'):return False
        return self.edit('site',create=True)

    def edit(self, group, create=False):
        if not create and not self.require_config():return False
        if self.config and self.config.is_file():draft=load_config(self.config)
        else:
            draft=load_config(self.source/'site.example.json')
            draft['domain']=(self.report or {}).get('domain') or ''
            draft['nat_hostname']=draft['domain'];draft['lan_ip']='';draft['management_cidrs']=[];draft['acme_email']=''
            draft['transcription_enabled']=False
            for row in next((s['rows'] for s in (self.report or {}).get('sections',[]) if s['id']=='profiles'),[]):
                if row['item']=='internal / SIP-IP':draft['lan_ip']=row['observed']
        title,fields=EDITORS[group]
        while True:
            items=[]
            for field in fields:
                value=field_value(draft,field)
                display=('On' if value else 'Off') if type(value) is bool else ', '.join(value) if isinstance(value,list) else str(value)
                label=LABELS.get(field,field.split('.')[-1].replace('_',' ').capitalize())
                items.append((field,label+': '+display,HELP.get(field,'Edit the saved preference for this setting.')))
            items += [('save','Save draft','Save the reviewed site file. Changes become active only after a separate apply operation.')]
            key=self.ui.select(title+' / draft',items,'Saved preferences only. Esc discards this edit session.')
            if key is None:return False
            if key=='save':
                try:
                    draft=validate_all(draft)
                    default=str(self.config) if self.config and self.config.resolve()!=CONFIG.resolve() else '/root/pbxctl-site.json'
                    value=self.ui.prompt('Save site draft',default,'Save beside the active configuration; the live configuration is updated only by a successful apply.')
                    if value is None:continue
                    path=Path(value).expanduser();need(path.is_absolute(),'Choose an absolute file path')
                    need(path.resolve()!=CONFIG.resolve(),'Save a candidate file instead of the active site configuration')
                    need(not path.is_symlink(),'Choose a regular file, not a symbolic link')
                    if path.exists() and not self.ui.confirm('Replace saved draft?',str(path)+' already exists. Replace its saved preferences?'):continue
                    atomic(path,json.dumps(draft,indent=2)+'\n',0o600);self.config=path
                    self.ui.view('Draft saved',str(path)+'\n\nThese preferences have not been applied to the PBX.');return True
                except (Error,OSError,ValueError) as e:
                    self.ui.view('Check the draft',str(e) if isinstance(e,Error) else 'Invalid value or file path. Review the fields and try again.')
                continue
            old=field_value(draft,key)
            if type(old) is bool:
                value=self.ui.select('Set '+LABELS.get(key,key),[('on','Enabled','Enable this feature in the saved preferences.'),('off','Disabled','Disable this feature in the saved preferences.')])
                if value is None:continue
                value=value=='on'
            elif key in ('secure_calling.mode','smtp.security'):
                choices=('optional','mandatory') if key=='secure_calling.mode' else ('starttls','tls','none')
                value=self.ui.select(LABELS[key],[(x,x.upper(),HELP.get(key,'Select the transport supported by your mail provider.')) for x in choices])
                if value is None:continue
            else:
                value=self.ui.prompt(LABELS.get(key,key),', '.join(old) if isinstance(old,list) else old,HELP.get(key,''))
                if value is None:continue
            try:set_field(draft,key,value)
            except (Error,ValueError):self.ui.view('Invalid value','Enter a value matching the field type and documented range.')

    def perform(self, action, extra=(), mutate=True, preview=True, restart=False, note=''):
        if not self.require_config():return
        args=[action,'--config',str(self.config),*extra]
        # A scanner override is not permission to act on a different saved site.
        config=load_config(self.config)
        need(not self.domain or self.domain==config['domain'],'Selected scan domain differs from the site file; choose the intended site before changing settings')
        need(not self.database or self.database==config['database'],'Selected scan database differs from the site file')
        self.ui.busy('Reviewing '+action)
        result=self.runner(args) if preview else {'operation':action,'domain':config['domain'],'site_file':str(self.config)}
        details={}
        if action=='firewall' and '--confirm' not in extra:
            details={'management_networks':config['management_cidrs']+config['lan_ipv6_cidrs'],
                     'carrier_addresses':config['provider_cidrs'],'phone_tls_port':config['tls_port'],
                     'rtp_udp_range':str(config['rtp_start'])+'-'+str(config['rtp_end']),
                     'automatic_rollback':'10 minutes unless confirmed from a new SSH connection'}
        if action=='test-email':details={'recipient':config['smtp']['recipient'],'smtp_host':config['smtp']['host']}
        if action=='smtp-credential':details={'credential_file':config['smtp']['password_file']}
        if action in ('offsite-init','retention','backup') and config['remote_backup']['enabled']:
            details['remote_repository']=config['remote_backup']['repository']
        if '--archive' in extra:details['recovery_directory']=extra[list(extra).index('--archive')+1]
        body='\n'.join(lines_for(result)+lines_for(details))+'\n\n'+note
        if not mutate:self.ui.view(action.replace('-',' ').title(),body);return
        self.ui.view('Review / '+action,body)
        if not self.ui.confirm('Apply / '+action,'Apply this operation to '+config['domain']+' using '+str(self.config)+'? '+note):return
        if restart:
            if not self.ui.confirm('Idle maintenance window','Allow required service restarts only after the backend verifies zero active calls?'):return
            args+=['--allow-restart']
        self.ui.busy('Applying '+action)
        call=lambda:self.runner(args+['--apply'])
        result=self.ui.external(call) if action in ('smtp-credential','offsite-init') else call()
        self.ui.view('Operation completed','\n'.join(lines_for(result)))
        self.refresh()

    def feature(self, name):
        if self.edit(name):self.perform('configure',['--modules',name],restart=name in ('carrier-tls',))

    def volume_args(self, name):
        args=['volume','--name',name]
        domain=self.domain or (self.report or {}).get('domain')
        database=self.database
        if not database and self.config and self.config.is_file():database=load_config(self.config)['database']
        if domain:args+=['--domain',domain]
        if database:args+=['--database',database]
        return args

    def simple_volume(self, name):
        """Current level -> change -> one confirmation. No site draft or deployment wizard."""
        args=self.volume_args(name);music=name=='hold-music'
        title='Hold-music volume' if music else 'Phone volume'
        while True:
            self.ui.busy('Reading current volume')
            state=self.runner(args)
            if state.get('selection_required'):
                choices=state['choices']
                if not choices:self.ui.view(title,'No supported music folder was found. Advanced > Audio settings has the detailed inventory.');return
                selected=self.ui.select('Choose music',[(x['stream'],x['stream'].split('/')[-1].replace('_',' ')+(' (in use)' if x['in_use'] else ''),'Adjust this music collection; every phone using these tracks is affected.') for x in choices])
                if selected is None:return
                args+=['--stream',selected];continue
            if music and '--stream' not in args:args+=['--stream',state['stream']]
            note='Current: '+state['label']+'\n'+state['scope']
            if music and not state['recorded']:note+='\nYour current tracks will be saved before the first change.'
            options=[('down','A little quieter (-1 dB)','Reduce the current music adjustment by 1 dB.'),('down3','Noticeably quieter (-3 dB)','Reduce the current music adjustment by 3 dB.'),('up','A little louder (+1 dB)','Increase by 1 dB; settings that would distort are refused.'),('set','Set a specific level','Enter an adjustment in dB relative to the preserved tracks.')] if music else [
                ('listen-down','Lower listening volume','Reduce PBX-to-phone audio by one step.'),('listen-up','Raise listening volume','Increase PBX-to-phone audio by one step.'),
                ('mic-down','Lower microphone volume','Reduce phone-to-PBX audio by one step.'),('mic-up','Raise microphone volume','Increase phone-to-PBX audio by one step.'),('set','Set microphone and listening levels','Choose -4 to +4 steps; zero is unchanged.')]
            if state['recorded']:options.append(('restore','Restore original level','Music: restore the exact preserved tracks. Phones: disable the toolkit gain adjustment.'))
            key=self.ui.select(title+' | '+state['label'],options,note)
            if key is None:return
            changes=[]
            try:
                if key=='restore':changes=['--disable']
                elif music:
                    current=state['gain_db'] if state['gain_db'] is not None else 0
                    if key=='set':
                        value=self.ui.prompt('Music level in dB',current,'Negative is quieter. This adjustment is relative to the preserved tracks, or to your current tracks on the first change.')
                        if value is None:continue
                        value=float(value)
                    else:value=current+{'down':-1,'down3':-3,'up':1}[key]
                    changes=['--gain-db',str(value)]
                else:
                    read,write=state['read_level'],state['write_level']
                    if key=='set':
                        value=self.ui.prompt('Microphone level',read,'-4 to +4 steps. This changes phone-to-PBX audio; zero is unchanged.')
                        if value is None:continue
                        read=int(value)
                        value=self.ui.prompt('Listening level',write,'-4 to +4 steps. This changes PBX-to-phone audio; zero is unchanged.')
                        if value is None:continue
                        write=int(value)
                    elif key=='listen-down':write-=1
                    elif key=='listen-up':write+=1
                    elif key=='mic-down':read-=1
                    elif key=='mic-up':read+=1
                    changes=['--read-level',str(read),'--write-level',str(write)]
                self.ui.busy('Checking volume change')
                preview=self.runner(args+changes)
                text='Current: '+preview['before']+'\nNew: '+preview['after']+'\nApplies to: '+preview['scope']
                if preview['baseline_note']:text+='\n'+preview['baseline_note']
                if not self.ui.confirm('Change volume?',text):continue
                self.ui.busy('Applying volume change')
                result=self.runner(args+changes+['--confirm',preview['token'],'--apply'])
                self.ui.view('Volume updated',result['current']+'\n\n'+result['scope']+'\n\nNo service restart was needed. Try a new call to listen.')
                self.refresh()
            except (Error,ValueError) as e:self.ui.view('Volume needs attention',str(e) if isinstance(e,Error) else 'Enter a numeric volume within the displayed range.')

    def advanced(self):
        items=[('audio','Detailed audio settings','Codec settings, music paths and exact volume scope.'),('security','Security and connectivity','TLS, SRTP, carrier routing and firewall.'),
               ('maintenance','Updates and maintenance','Update the application or toolkit, inspect health, or roll back.'),('setup','Install and site configuration','Site files, deployment and module selection.'),
               ('findings','Detailed recommendations','All findings with their evidence and suggested next steps.'),('export','Export a report','HTML, text or structured JSON.'),('domain','Choose domain','Inspect another existing SIP domain.')]
        while True:
            key=self.ui.select('Advanced',items,'Everyday volume controls are on the main menu.')
            if key is None:return
            if key=='findings':self.findings()
            elif key=='export':need('result' in self.report,'Refresh the scan before exporting');self.export()
            elif key=='domain':
                domain=self.ui.prompt('SIP domain',self.report.get('domain') or '','Enter an existing domain, or clear to discover a single domain automatically.')
                if domain is not None:self.domain=domain or None;self.refresh()
            else:self.submenu(key)

    def modules(self):
        chosen=set()
        while True:
            items=[(m,('[x] ' if m in chosen else '[ ] ')+m,'Select independent modules. Saved site preferences determine their values.') for m in MODULES]
            items.append(('review','Review selected modules',', '.join(sorted(chosen)) or 'Select at least one module first.'))
            key=self.ui.select('Choose modules',items,'Space is not required: press Enter to toggle a module.')
            if key is None:return
            if key=='review':
                if not chosen:continue
                self.perform('configure',['--modules',','.join(m for m in MODULES if m in chosen)],restart=bool(chosen & {'audio','hardening','carrier-tls'}));return
            if key in chosen:chosen.remove(key)
            else:chosen.add(key)

    def export(self):
        kind=self.ui.select('Export current scan',[('html','Printable HTML','Standalone report with overview, findings and evidence.'),('text','Text report','Readable plain text for your records.'),('json','JSON data','Structured data for automation.')])
        if kind is None:return
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
        extension={'text':'txt'}.get(kind,kind)
        value=self.ui.prompt('Save report','/root/pbx-report-'+stamp+'.'+extension,'Contains internal addresses and extensions. Stored privately; no PBX configuration changes.')
        if value is None:return
        path=Path(value).expanduser();need(path.is_absolute(),'Choose an absolute report path')
        need(not path.exists() and not path.is_symlink(),'Choose a new report filename')
        content=render_html(self.report) if kind=='html' else render_text(self.report,True) if kind=='text' else json.dumps(self.report,indent=2)
        # Exclusive creation prevents clobbering a file or symlink after the prompt.
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as out:out.write(content+'\n')
        self.ui.view('Report exported',str(path)+'\n\nSnapshot: '+self.report['scanned_at'])

    def archive_operation(self, action, mutate=True):
        archive=self.ui.prompt('Recovery directory','','Existing local recovery set, not a compressed archive filename. Verify it before a restore.')
        if not archive:return
        self.perform(action,['--archive',archive],mutate=mutate,note='Restore replaces PBX data and stops services. Activation is a separate cutover step.' if action=='restore' else 'An isolated database verification temporarily creates and removes a scratch database.' if mutate and action=='verify-backup' else '')

    def submenu(self, group):
        menus={
            'audio':('Audio and hold music', [('view','Current audio settings','View codecs, music selections, gain rules and volume evidence.'),('hold-music','Adjust hold-music volume','Edit attenuation relative to preserved original tracks.'),('call-volume','Adjust phone volume','Set microphone and listening gain for selected extensions.'),('audio-preset','Apply preferred voice codecs','Configure Opus, G.722 and G.711; requires an idle service restart.')]),
            'security':('Security and connectivity', [('view','Current security and NAT','View TLS, NAT, carrier, firewall and media-security evidence.'),('secure-calling','Phone encryption','Configure optional or mandatory SRTP for selected destinations.'),('carrier-tls','Carrier encryption','Configure TLS and SRTP after preparing the carrier portal.'),('tls','Configure TLS listener','Use the certificate and listener settings in your site file.'),('hardening','Apply authentication and NAT settings','Apply the reviewed hardening module during an idle window.'),('firewall','Deploy firewall policy','Apply the site policy with automatic rollback until confirmed.'),('confirm-firewall','Confirm firewall from a new SSH connection','Use the confirmation token after verifying SSH access.'),('media','Verify an active call leg','Inspect codec and negotiated audio encryption by call UUID.')]),
            'voicemail':('Voicemail and email', [('view','Current voicemail services','View recognition, summaries, mailbox and email settings.'),('transcription','Configure local transcription','Enable or disable the local speech recognition worker.'),('ai-summary','Configure local summaries','Enable or disable local voicemail summaries.'),('smtp','Configure SMTP delivery','Use any compatible third-party SMTP provider.'),('credential','Enter SMTP credential','Private password entry outside the report and site draft.'),('test-email','Send a test email','Send to the recipient in the selected site file.'),('alerts','Configure email alerts','Enable change alerts after validating email delivery.')]),
            'backups':('Backups and recovery', [('view','Backup readiness','View recorded local backup age and verification limits.'),('backup','Create a backup','Create a recovery set; upload too when off-server backup is enabled.'),('verify','Verify backup checksums','Read the recovery set and verify its files.'),('verify-db','Test database recovery','Restore into an isolated scratch database and clean up afterward.'),('preferences','Edit backup preferences','Review free-space reserve, remote repository and retention.'),('schedule','Configure local backup schedule','Install the local daily backup job.'),('offsite','Configure off-server backups','Apply the saved repository preference after verifying access.'),('offsite-init','Initialize remote repository','Create an encrypted repository using private credential files.'),('restore','Restore server data','Destructive recovery operation; services stay isolated until activation.'),('activate','Activate a restored server','Cut over only after the source server is stopped.'),('retention','Apply remote retention','Review and prune remote snapshots according to saved retention.')]),
            'maintenance':('Updates and maintenance', [('view','Current update readiness','Inspect tracked edits and cached upstream differences.'),('check','Run health checks','Check installed toolkit services and integration health.'),('update-review','Check for PBX updates','Fetch official tracking data; keep working files unchanged.'),('update','Update PBX application','Back up and perform a fast-forward update in an idle window.'),('tool','Update this toolkit','Select an extracted, verified toolkit release directory.'),('rollback','Roll back a module','Restore an exact current module snapshot, refusing intervening edits.')]),
            'setup':('Install and configure', [('choose','Select site file','Choose an existing reviewed site configuration.'),('site','Edit site settings','Review domain, addressing, trusted networks and mailbox.'),('modules','Choose optional modules','Select modules with checkboxes and review before applying.'),('deploy','Deploy toolkit beside an existing PBX','Install toolkit code without creating phone accounts or routing.'),('install','Install a fresh Debian 13 PBX','Run the pinned base installer; existing installations are refused.')]),
        }
        title,items=menus[group]
        while True:
            key=self.ui.select(title,items,'Site file: '+str(self.config or 'Not selected. Scanning is available.'))
            if key is None:return
            try:
                if key=='view':self.show(title,{'audio':['profiles','phones','audio','music'],'security':['profiles','audio','gateways','certificate','network'],'voicemail':['voicemail','services','modules'],'backups':['backups','services'],'maintenance':['updates']}[group])
                elif key in ('hold-music','call-volume','secure-calling','carrier-tls','transcription','ai-summary','smtp'):self.feature(key)
                elif key=='audio-preset':self.perform('configure',['--modules','audio'],restart=True)
                elif key in ('tls','hardening','alerts'):self.perform('configure',['--modules',key],restart=key=='hardening')
                elif key=='media':
                    uuid=self.ui.prompt('Active call UUID','','Inspect one leg only. Test phone and carrier legs separately.')
                    if uuid:self.ui.view('Active call media','\n'.join(lines_for(self.runner(['check-media','--uuid',uuid]))))
                elif key=='credential':self.perform('smtp-credential',preview=False,note='The next screen asks privately for the provider password or app password.')
                elif key=='test-email':self.perform('test-email',preview=False,note='This sends an actual email to the configured recipient.')
                elif key=='verify':self.archive_operation('verify-backup',False)
                elif key=='verify-db':self.archive_operation('verify-backup')
                elif key in ('restore','rollback'):self.archive_operation(key)
                elif key=='preferences':self.edit('backup')
                elif key=='schedule':self.perform('configure',['--modules','backup'])
                elif key=='offsite':self.perform('configure',['--modules','offsite'])
                elif key=='activate':
                    if self.ui.confirm('Source server stopped?','Confirm the original PBX is stopped before activating this restored server.'):self.perform('activate',['--source-stopped'])
                elif key=='update-review':self.perform('update',['--fetch'],mutate=False)
                elif key=='update':self.perform('update',['--fetch'],restart=True)
                elif key=='tool':
                    source=self.ui.prompt('Extracted toolkit release','','Full path to the downloaded release directory containing MANIFEST.json.')
                    if source:self.perform('update',['--target','tool','--source',source])
                elif key=='choose':
                    path=self.ui.prompt('Select site file',str(self.config or '/root/pbxctl-site.json'),'An existing configuration file. Selecting it does not apply settings.')
                    if path:load_config(path);self.config=Path(path);self.domain=None;self.database=None;self.refresh()
                elif key=='site':self.edit('site',create=True)
                elif key=='modules':self.modules()
                elif key=='confirm-firewall':
                    token=self.ui.prompt('Firewall confirmation token','','Use a new SSH connection to verify management access before confirming.')
                    if token:self.perform('firewall',['--confirm',token],preview=False)
                elif key=='firewall':self.perform('firewall',preview=False,note='Verify SSH from a new connection and confirm the returned token within ten minutes.')
                else:self.perform(key,mutate=key!='check',note='Verify SSH from a new connection and confirm the returned token within ten minutes.' if key=='firewall' else '')
            except (Error,OSError,ValueError,KeyError) as e:self.ui.view('Operation needs attention',str(e) if isinstance(e,Error) else 'Check the selected values and local prerequisites. No automatic retry was attempted.')

    def run(self):
        try:self.refresh()
        except (Error,OSError,ValueError):self.ui.view('Scan unavailable','The scan could not start. Select a valid site/domain or use install and configure.');self.report={'counts':{},'findings':[],'sections':[],'domain':None,'scanned_at':'Not scanned'}
        choices=[('overview','System status','A quick overview of your phones, services and settings.'),('music','Hold-music volume','See the current level, make it quieter or louder, or restore it.'),('phone','Phone volume','See microphone and listening gain, then adjust directly.'),
                 ('voicemail','Voicemail and email','Transcription, local summaries and email delivery.'),('backups','Backups and recovery','Create or verify a backup; recovery and scheduling options.'),
                 ('advanced','Advanced settings','Detailed reports, security, updates, setup and configuration files.'),('refresh','Refresh status','Scan the server again.'),('exit','Exit','Return to the SSH shell.')]
        while True:
            key=self.ui.select('Main menu',choices,'Inspecting settings is read-only. Changes are reviewed before applying.')
            if key in (None,'exit'):return
            try:
                if key=='overview':self.ui.view('System overview','\n\n'.join(k+': '+v for k,v in overview(self.report))+'\n\nVERIFICATION LIMITS\n'+'\n'.join(self.report.get('limitations',[])))
                elif key=='music':self.simple_volume('hold-music')
                elif key=='phone':self.simple_volume('call-volume')
                elif key=='advanced':self.advanced()
                elif key=='refresh':self.refresh()
                else:self.submenu(key)
            except (Error,OSError,ValueError,KeyError) as e:self.ui.view('Operation needs attention',str(e) if isinstance(e,Error) else 'Check the selected values and local prerequisites; then refresh the scan.')


def launch(runner,source,config=None,domain=None,database=None):
    from .base import supported
    supported()
    need(sys.stdin.isatty() and sys.stdout.isatty(),'Open an interactive SSH terminal for the console. Use status --format text for noninteractive output.')
    import curses
    if hasattr(curses,'set_escdelay'):curses.set_escdelay(150)
    try:
        curses.wrapper(lambda window:Console(Screen(window,curses),runner,source,config,domain,database).run())
    except KeyboardInterrupt:pass
    except curses.error as e:raise Error('Terminal display unavailable. Use TERM=xterm-256color in a compatible SSH terminal, or use status for plain output.') from e
