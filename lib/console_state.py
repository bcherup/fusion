"""Plain-language current-state summaries from observed scanner evidence."""
import re


class Current:
    def __init__(self, report):
        self.report=report or {}
        self.sections={s['id']:s['rows'] for s in self.report.get('sections',[])}

    def values(self, section, match):
        return list(dict.fromkeys(r['observed'] for r in self.sections.get(section,[]) if match(r)))

    def value(self, section, item, fallback='Not verified'):
        values=self.values(section,lambda r:r['item']==item)
        return ' / '.join(values) if values else fallback

    def failed(self, area):
        return any(f.get('severity')=='unknown' and f.get('area')==area for f in self.report.get('findings',[]))

    def service(self, names):
        if self.failed('Services'):return 'Not verified; refresh needed'
        rows=[r for r in self.sections.get('services',[]) if r['item'] in names]
        if not rows:return 'Not detected'
        if len(rows)>1:return 'Multiple jobs detected; review needed'
        value=rows[0]['observed']
        return {'active':'Running','inactive':'Paused','failed':'Needs attention'}.get(value,'Not verified')

    def transcription(self):
        status=self.service(('pbxctl-transcribe.timer','fusionpbx-local-transcribe.timer'))
        enabled=self.value('voicemail','transcribe / enabled','Unknown')
        if status=='Running' and enabled=='false':return 'Job running; transcription setting off'
        return status

    def summaries(self):return self.service(('pbxctl-ai-summary.timer','pbx-ai-summary.timer'))

    def email(self):
        if self.failed('Voicemail'):return 'Not verified; refresh needed'
        host=self.value('voicemail','email / smtp_host','')
        return host or 'No server found in inspected settings'

    def gain(self, kind):
        matches=self.values('audio',lambda r:r['item'].endswith('/ '+kind+' gain'))
        if matches:return matches[0] if len(matches)==1 else 'Varies by call rule'
        answers=self.values('audio',lambda r:r['item'].endswith('/ Answering phone gain'))
        if answers:return answers[0]+' (answering calls)'
        return 'No explicit gain found'

    def music(self):
        match=self.value('music','Current music files','')
        gain=self.value('music','Recorded music gain','')
        if gain and match=='Match recorded output':return gain
        if gain:return 'Files changed; re-read required'
        return 'Read current files to measure'

    def policy(self):
        if self.failed('Call rules'):return 'Not verified; refresh needed'
        values=self.values('audio',lambda r:'SRTP' in r['item'] or 'rtp_secure_media' in r['item'])
        modes={m for value in values for m in ('optional','mandatory') if m in value}
        if modes=={'optional'}:return 'Optional; clear audio allowed'
        if modes=={'mandatory'}:return 'Required by inspected call rules'
        if modes:return 'Varies by call route'
        return 'No supported rule found'

    def summary(self, area):
        if area=='audio':
            codecs=self.values('profiles',lambda r:r['item'].endswith('/ CODECS IN'))
            return [('Music adjustment',self.music()),('Phone gain',self.gain('Listening')),('Codecs',', '.join(codecs) or 'Not verified')]
        if area=='voicemail':return [('Transcription',self.transcription()),('AI summaries',self.summaries()),('Recognition model',self.value('voicemail','transcribe / api_model'))]
        if area=='email':return [('Mail server',self.email()),('Port',self.value('voicemail','email / smtp_port')),('Security',self.value('voicemail','email / smtp_secure')),('Delivery','Send a test to confirm inbox delivery')]
        if area=='backups':return [('Last toolkit backup',self.value('backups','Last recorded local backup','No recorded backup')),('Off-server copy',self.value('backups','Off-server backup preference','Not configured')+' (saved preference)'),('Recovery test','Full server recovery not verified')]
        if area=='security':
            tls=self.values('profiles',lambda r:r['item'].endswith('/ TLS listener port'))
            return [('TLS listeners',', '.join(tls) or 'Not verified'),('Audio policy',self.policy()),('Active-call encryption','Check a call to verify each leg')]
        if area=='updates':return [('Installed revision',self.value('updates','Application revision')),('Local edits',self.value('updates','Tracked local changes')),('Upstream comparison',self.value('updates','Cached upstream comparison')),('Freshness','Cached result; choose Check for updates')]
        return [('Active calls',self.value('services','Active calls')),('Free disk',self.value('services','Free disk')),('Detected issues',str(self.report.get('counts',{}).get('error','?'))+' errors / '+str(self.report.get('counts',{}).get('warning','?'))+' warnings')]

    def mailboxes(self):
        return sorted({r['item'].removeprefix('Mailbox ') for r in self.sections.get('voicemail',[]) if re.fullmatch(r'Mailbox [0-9]{2,8}',r['item'])})
