"""Health checks independent of the PBX database and optional change alerts."""
import datetime
import json
import shutil
from .common import ROOT, STATE, atomic, run
from .recovery import ARCHIVES

def inspect(c):
    issues=[]
    for name in ('postgresql','nginx','freeswitch','fail2ban'):
        if run(['systemctl','is-active',name],check=False).returncode:issues.append('Service unavailable: '+name)
    if (STATE/'transcription.json').exists() and c['transcription_enabled']:
        for name in ('pbx-whisper','pbxctl-transcribe.timer'):
            if run(['systemctl','is-active',name],check=False).returncode:issues.append('Service unavailable: '+name)
        try:
            attempts=json.loads((STATE/'transcribe/state.json').read_text()).get('attempts',{})
            if any(x.get('count',0)>=3 for x in attempts.values()):issues.append('Transcription retries exhausted')
        except (OSError,ValueError):issues.append('Transcription state unavailable')
    if shutil.disk_usage('/').free<c['backup_min_free_gib']*1024**3:issues.append('Free disk below backup reserve')
    now=datetime.datetime.now(datetime.timezone.utc)
    if (STATE/'backup.json').exists():
        if run(['systemctl','show','pbxctl-backup.service','-p','Result','--value'],check=False).stdout.strip() not in ('','success'):
            issues.append('Most recent scheduled backup failed')
        try:
            date=datetime.datetime.fromisoformat(json.loads((ARCHIVES/'last-success.json').read_text())['created_utc'])
            if (now-date).total_seconds()>36*3600:issues.append('Local backup older than 36 hours')
        except (OSError,ValueError,KeyError):issues.append('No verified local backup')
    if (STATE/'tls.json').exists():
        if run(['openssl','x509','-checkend',str(21*86400),'-noout','-in','/etc/letsencrypt/live/'+c['domain']+'/cert.pem'],check=False).returncode:issues.append('Certificate missing or expires within 21 days')
    check=run(['python3',ROOT/'assets/upgrade-check.py','--web-root',c['web_root']],check=False)
    if check.returncode:issues.append('Application source or adapter check needs attention')
    return {'checked_utc':now.isoformat(),'healthy':not issues,'issues':sorted(set(issues))}

def monitor(c):
    result=inspect(c);p=STATE/'health.json'
    try:previous=json.loads(p.read_text())
    except (OSError,ValueError):previous={}
    if (STATE/'alerts.json').exists() and result['issues']!=previous.get('notified_issues',[]):
        from .mail import send
        try:
            send(c,'PBX health: '+('recovered' if result['healthy'] else 'attention needed'), '\n'.join(result['issues']) or 'All configured checks passed.')
            result['notified_issues']=result['issues']
        except Exception:
            result['notification_failed']=True;result['notified_issues']=previous.get('notified_issues',[])
    else:result['notified_issues']=previous.get('notified_issues',[])
    atomic(p,json.dumps(result,indent=2));return result
