"""Everyday workflows: observed status, plain choices, and one change review."""
import copy
import json
from pathlib import Path
import uuid
from .common import CONFIG, ROOT, STATE, Error, atomic, need
from .config import load_config, validate_all
from .console_state import Current
from .diagnostics import bounded_json

DRAFTS=Path('/root/.pbx-toolkit')
ARCHIVES=Path('/var/backups/pbxctl')


class Daily:
    def now(self):return Current(self.report)

    def save_candidate(self, candidate):
        path=DRAFTS/('settings-'+uuid.uuid4().hex+'.json')
        atomic(path,json.dumps(validate_all(candidate),indent=2)+'\n',0o600)
        return path

    def guided_setup(self):
        """One-time identity questions; no example site is ever silently applied."""
        c=load_config(self.source/'site.example.json');now=self.now()
        c['domain']=(self.report or {}).get('domain') or ''
        c['lan_ip']=now.value('profiles','internal / SIP-IP','')
        c['database']=self.database or 'fusionpbx';c['management_cidrs']=[];c['acme_email']=''
        c['transcription_enabled']=False
        for key in ('secure_calling','carrier_tls','hold_music','call_volume','ai_summary'):c[key]['enabled']=False
        fields=[('domain','PBX domain','The SIP domain already used by your phones.'),('lan_ip','PBX local IP','The server address on your local network.'),
                ('management_cidrs','Trusted admin networks','For later firewall setup. Example: 192.168.1.0/24. Enter your actual trusted network.'),
                ('acme_email','Administrator email','Used as the certificate contact. This does not configure email delivery.')]
        for key,title,note in fields:
            value=self.ui.prompt(title,', '.join(c[key]) if isinstance(c[key],list) else c[key],note)
            if value is None:return False
            c[key]=[x.strip() for x in value.split(',') if x.strip()] if key=='management_cidrs' else value
        c['nat_hostname']=c['domain']
        boxes=now.mailboxes()
        if boxes:
            selected=self.ui.select('Your voicemail box',[(x,'Mailbox '+x,'Use this mailbox for voicemail/email setup.') for x in boxes])
            if selected is None:return False
            c['mailbox']=selected
        # Preserve already-managed feature preferences when adding the console to a site.
        from .config import MODULES
        for path in (STATE/(name+'.json') for name in MODULES):
            if not path.is_file():continue
            record=bounded_json(path);desired=record.get('desired',{})
            if desired.get('database')!=c['database'] or desired.get('domain',c['domain'])!=c['domain']:continue
            for key,value in desired.items():
                if key in c and key not in ('domain','database','web_root'):c[key]=value
        c=validate_all(c)
        review='PBX: '+c['domain']+'\nLocal IP: '+c['lan_ip']+'\nTrusted admin networks: '+', '.join(c['management_cidrs'])+'\nContact: '+c['acme_email']+'\n\nSave these preferences for this console? No PBX services or firewall rules change.'
        if not self.ui.confirm('Remember this server?',review):return False
        self.config=DRAFTS/'server.json';atomic(self.config,json.dumps(c,indent=2)+'\n',0o600)
        return True

    def working(self):
        if not self.require_config():return None
        c=load_config(self.config)
        need(not self.domain or c['domain']==self.domain,'Choose the matching server before changing settings')
        need(not self.database or c['database']==self.database,'Choose the matching database before changing settings')
        return c

    def apply_preferences(self, candidate, module, description, restart=False, password=False):
        candidate=validate_all(candidate);base=self.working()
        if base is None:return False
        deploy=not (ROOT/'VERSION').is_file()
        note=description+'\n\nApplies to: '+candidate['domain']
        if deploy:note+='\nFirst use installs the toolkit and its administration dependencies alongside this PBX.'
        if restart:note+='\nRequires a brief restart; the backend first checks for zero active calls.'
        if password:note+='\nThe next prompt privately requests the SMTP password or app password.'
        if not self.ui.confirm('Apply these changes?',note):return False
        if password:
            # A failed configuration keeps the old site's credential intact.
            # Retain private credential versions for rollback instead of overwriting one in use.
            candidate=copy.deepcopy(candidate)
            candidate['smtp']['password_file']='/etc/pbxctl/secrets/smtp-'+uuid.uuid4().hex
        path=self.save_candidate(candidate)
        try:
            if deploy:self.ui.busy('Preparing administration tools');self.runner(['deploy','--config',str(self.config),'--apply'])
            if password:self.ui.external(lambda:self.runner(['smtp-credential','--config',str(path),'--apply']))
            self.ui.busy('Saving settings')
            args=['configure','--config',str(path),'--modules',module,'--apply']
            if restart:args+=['--allow-restart']
            self.runner(args)
            self.config=CONFIG if CONFIG.is_file() else path
            self.refresh();self.ui.view('Settings updated','The requested settings were applied. Current status has been read again.')
            return True
        finally:
            if self.config!=path:path.unlink(missing_ok=True)

    def guided_fields(self, title, candidate, fields, choices=None):
        from .console import field_value, set_field, LABELS, HELP
        choices=choices or {}
        for field in fields:
            old=field_value(candidate,field);label=LABELS.get(field,field.split('.')[-1].replace('_',' ').capitalize())
            if field in choices:
                value=self.ui.select(label,choices[field],note='Current saved preference: '+str(old))
            elif type(old) is bool:
                key=self.ui.select(label,[('on','On','Enable this setting.'),('off','Off','Disable this setting.')],note='Current saved preference: '+('On' if old else 'Off'))
                value=None if key is None else key=='on'
            else:value=self.ui.prompt(label,', '.join(old) if isinstance(old,list) else old,HELP.get(field,'Enter the value you want to use.'))
            if value is None:return None
            set_field(candidate,field,value)
        return candidate

    def job(self, name, running):
        args=['job','--name',name,'--disable' if running else '--enable']
        preview=self.runner(args)
        text='Current: '+preview['before']+'\nNew: '+preview['after']+'\nApplies to: '+preview['scope']+'\n\n'+preview['note']
        if not self.ui.confirm('Pause this job?' if running else 'Resume this job?',text):return
        self.ui.busy('Updating background job');self.runner(args+['--confirm',preview['token'],'--apply']);self.refresh()

    def install_voice_feature(self, name):
        c=self.working()
        if c is None:return
        c=copy.deepcopy(c)
        if name=='transcription':
            boxes=self.now().mailboxes()
            if boxes:
                selected=self.ui.select('Transcribe which mailbox?',[(x,'Mailbox '+x,'Transcribe new opted-in voicemail locally.') for x in boxes])
                if selected is None:return
                c['mailbox']=selected
            c['transcription_enabled']=True
            description='Turn on local voicemail transcription for mailbox '+c['mailbox']+'.\nDownloads and installs the local speech model. Existing custom workers are preserved and require migration before replacement.'
        else:
            c['ai_summary']['enabled']=True
            description='Turn on local voicemail summaries for '+c['domain']+'.\nDownloads and installs the local summary model. Toolkit transcription must already be configured; existing custom installations require migration.'
        self.apply_preferences(c,name,description)

    def email_setup(self):
        c=self.working()
        if c is None:return
        c=copy.deepcopy(c);now=self.now()
        host=now.value('voicemail','email / smtp_host','')
        if host:c['smtp']['host']=host
        port=now.value('voicemail','email / smtp_port','')
        if port.isdigit():c['smtp']['port']=int(port)
        secure=now.value('voicemail','email / smtp_secure','')
        if secure in ('tls','ssl','none'):c['smtp']['security']={'tls':'starttls','ssl':'tls','none':'none'}[secure]
        choices={'smtp.security':[('starttls','STARTTLS (usually port 587)','Encrypt the connection before authenticating.'),('tls','TLS (usually port 465)','Encrypt from the start.'),('none','Trusted relay without TLS','Only for an authorized relay; password authentication is prohibited.')]}
        c=self.guided_fields('Email',c,['smtp.host','smtp.port','smtp.security','smtp.auth'],choices)
        if c is None:return
        fields=(['smtp.username'] if c['smtp']['auth'] else [])+['smtp.from_address','smtp.from_name','smtp.recipient']
        c=self.guided_fields('Email',c,fields)
        if c is None:return
        boxes=now.mailboxes()
        if boxes:
            box=self.ui.select('Send voicemail from which mailbox?',[(x,'Mailbox '+x,'This mailbox will send to the recipient you entered.') for x in boxes])
            if box is None:return
            c['mailbox']=box
        from .mail import validate_ready
        validate_ready(c['smtp'])
        self.apply_preferences(c,'smtp','Current mail server: '+now.email()+'\nNew server: '+c['smtp']['host']+':'+str(c['smtp']['port'])+'\nSecurity: '+c['smtp']['security']+'\nFrom: '+c['smtp']['from_address']+'\nRecipient: '+c['smtp']['recipient']+'\nMailbox: '+c['mailbox'],password=c['smtp']['auth'])

    def simple_operation(self, action, label, extra=(), restart=False):
        c=self.working()
        if c is None:return
        args=[action,'--config',str(self.config),*extra]
        prepare=action=='update' and not (ROOT/'VERSION').is_file()
        note='Server: '+c['domain']
        if action=='backup':note+='\nCreate a private recovery copy of the database, configuration, and media.'+('\nAlso upload to '+c['remote_backup']['repository'] if c['remote_backup']['enabled'] else '\nKeep this copy on the PBX.')
        elif action=='test-email':note+='\nSend a test to '+c['smtp']['recipient']+' using '+c['smtp']['host']+'.'
        elif action=='verify-backup':note+='\nVerify the selected saved copy. No live PBX data is replaced.'
        elif action=='update':
            preview=self.runner(args)
            from .console import lines_for
            note+='\n'+'\n'.join(lines_for(preview))
        if restart:note+='\nBack up and briefly stop services after verifying zero active calls.'
        if prepare:note+='\nFirst install the toolkit and its administration dependencies alongside this PBX.'
        if not self.ui.confirm(label+'?',note):return
        if prepare:self.ui.busy('Preparing administration tools');self.runner(['deploy','--config',str(self.config),'--apply'])
        self.ui.busy(label)
        result=self.runner(args+['--apply']+(['--allow-restart'] if restart else []))
        from .console import lines_for
        self.ui.view('Completed','\n'.join(lines_for(result)))
        self.refresh()

    def choose_backup(self):
        paths=sorted((p for p in ARCHIVES.iterdir() if p.is_dir() and not p.is_symlink() and (p/'manifest.json').is_file()),reverse=True)[:50] if ARCHIVES.is_dir() else []
        if not paths:self.ui.view('No saved copies found','Choose Back up now to create your first toolkit recovery copy.');return None
        return self.ui.select('Choose a saved copy',[(str(p),p.name,'Verify this saved recovery copy. No server data is replaced.') for p in paths])

    def offsite_setup(self):
        c=self.working()
        if c is None:return
        c=copy.deepcopy(c)
        choice=self.ui.select('Off-server backups',[('on','Set up an off-server copy','Use an SFTP or S3 repository you control.'),('off','Keep backups on this PBX only','Existing remote copies are retained.')])
        if choice is None:return
        c['remote_backup']['enabled']=choice=='on'
        if choice=='on':
            value=self.ui.prompt('Backup destination',c['remote_backup']['repository'],'SFTP: sftp:user@host:/folder   S3: s3:https://host/bucket. Keep credentials out of this address.')
            if value is None:return
            c['remote_backup']['repository']=value
            self.ui.view('Remote storage access','Set up the repository login/key and encryption password first. This change verifies access before enabling uploads. Advanced contains repository initialization and credential setup.')
        self.apply_preferences(c,'offsite','Off-server backup: '+('On\nDestination: '+c['remote_backup']['repository'] if choice=='on' else 'Off; remote files retained'))

    def phone_security(self):
        c=self.working()
        if c is None:return
        c=copy.deepcopy(c)
        selected=self.ui.select('Phone audio encryption',[('optional','Offer encryption','Compatible phones can use SRTP; clear audio remains possible.'),('mandatory','Require encryption','Calls to selected destinations require phones with working SRTP.'),('off','Remove the toolkit encryption rule','Other phone and carrier settings remain independent.')],summary=self.now().summary('security'))
        if selected is None:return
        c['secure_calling']['enabled']=selected!='off'
        if selected!='off':c['secure_calling']['mode']=selected
        c=self.guided_fields('Phone encryption',c,['secure_calling.destinations'])
        if c is None:return
        self.apply_preferences(c,'secure-calling','Current policy: '+self.now().policy()+'\nNew toolkit policy: '+selected+'\nDestinations: '+', '.join(c['secure_calling']['destinations'])+'\nPhone TLS and SRTP must be configured on the devices. Verify both call directions afterward.')

    def inspect_calls(self):
        self.ui.busy('Reading active calls');calls=self.runner(['active-media'])
        if not calls:self.ui.view('No active calls','Connect a test call, then choose Check current calls again.');return
        text=[]
        for call in calls:
            text.append('Call leg '+call['uuid']+'\nProfile: '+str(call.get('sofia_profile_name'))+'\nCodec: '+str(call.get('read_codec'))+' / '+str(call.get('write_codec'))+'\nEncrypted audio: '+('Confirmed' if call['encrypted_audio_confirmed'] else 'Not confirmed'))
        self.ui.view('Current call audio','\n\n'.join(text))

    def everyday(self, area):
        titles={'audio':'Music and phone audio','voicemail':'Voicemail','email':'Email','backups':'Backups','security':'Call security','updates':'Updates','system':'System status'}
        while True:
            now=self.now()
            if area=='audio':items=[('music','Hold-music volume','Read the current file level and adjust it.'),('phone','Phone volume','Read current listening and microphone gain.'),('codecs','Change voice codecs','Use the preferred Opus, G.722 and G.711 settings.'),('calls','Check current calls','See the actual codec and audio-encryption result.')]
            elif area=='voicemail':
                verb=lambda status:'Pause' if status=='Running' else 'Resume' if status=='Paused' else 'Set up' if status=='Not detected' else 'Review'
                items=[('transcription',verb(now.transcription())+' transcription','Control the existing job; current messages and settings are retained.'),('ai-summary',verb(now.summaries())+' AI summaries','Control the existing summary job; previously generated text is retained.')]
            elif area=='email':items=[('email-setup','Change email settings','Answer the email-provider questions; enter the password privately.'),('test-email','Send a test email','Send to your configured recipient and confirm it arrives.'),('alerts','Email alerts','Pause/resume an existing alert job or set up alerts.')]
            elif area=='backups':items=[('backup','Back up now','Create a private recovery copy.'),('verify','Check a saved backup','Choose a saved copy from the list; no path to type.'),('schedule','Set up daily backups','Create the first backup and enable the daily job.'),('offsite','Off-server copy','Choose whether and where to keep a remote backup.')]
            elif area=='security':items=[('calls','Check current calls','Verify actual encryption separately for each active leg.'),('phone-security','Change phone encryption','Offer or require encryption for selected phones/groups.'),('carrier','Carrier connection settings','Provider-specific preparation and connection settings.')]
            elif area=='updates':items=[('check-updates','Check for updates','Fetch current upstream information without changing installed code.'),('update','Install PBX updates','Review changes, back up and update while no calls are active.')]
            else:items=[('overview','Full current status','Inspect observed values and their evidence.'),('findings','What needs attention','See findings and suggested next steps.'),('export','Save a report','Export a readable report for your records.')]
            items.append(('refresh','Refresh current status','Read the server again.'))
            key=self.ui.select(titles[area],items,summary=now.summary(area))
            if key is None:return
            try:
                if key=='refresh':self.refresh()
                elif key=='music':self.simple_volume('hold-music')
                elif key=='phone':self.simple_volume('call-volume')
                elif key=='calls':self.inspect_calls()
                elif key in ('transcription','ai-summary','alerts'):
                    status=now.transcription() if key=='transcription' else now.summaries() if key=='ai-summary' else now.service(('pbxctl-health.timer','fusionpbx-health.timer'))
                    if status=='Paused' and now.value('modules',key,'')=='Recorded enabled: false' and key!='alerts':self.install_voice_feature(key)
                    elif status in ('Running','Paused'):self.job(key,status=='Running')
                    elif status!='Not detected':self.ui.view('Review existing job',status+'\n\n'+self.current_service_details())
                    elif key=='alerts':
                        c=self.working()
                        if c:self.apply_preferences(c,'alerts','Turn on email alerts using your saved mail settings.')
                    else:self.install_voice_feature(key)
                elif key=='email-setup':self.email_setup()
                elif key in ('backup','test-email'):self.simple_operation(key,'Back up now' if key=='backup' else 'Send test email')
                elif key=='verify':
                    selected=self.choose_backup()
                    if selected:self.simple_operation('verify-backup','Verify saved copy',['--archive',selected])
                elif key in ('schedule','codecs'):
                    c=self.working()
                    if c:self.apply_preferences(c,'backup' if key=='schedule' else 'audio','Enable daily backups and create the first recovery copy.' if key=='schedule' else 'Set preferred voice codecs to Opus, G.722, PCMU, PCMA.',restart=key=='codecs')
                elif key=='offsite':self.offsite_setup()
                elif key=='phone-security':self.phone_security()
                elif key=='carrier':self.feature('carrier-tls')
                elif key=='check-updates':
                    c=self.working()
                    if c:
                        from .console import lines_for
                        self.ui.busy('Checking upstream');result=self.runner(['update','--config',str(self.config),'--fetch']);self.ui.view('Available updates','\n'.join(lines_for(result)));self.refresh()
                elif key=='update':self.simple_operation('update','Install updates',['--fetch'],restart=True)
                elif key=='findings':self.findings()
                elif key=='overview':self.show('Observed system settings',[s['id'] for s in (self.report or {}).get('sections',[])])
                elif key=='export':self.export()
            except (Error,OSError,ValueError,KeyError) as e:self.ui.view('Needs attention',str(e) if isinstance(e,Error) else 'This operation could not complete. Refresh the status and check the entered values; no automatic retry was made.')

    def current_service_details(self):
        from .console import section_text
        return section_text(self.report or {},['services','voicemail'])
