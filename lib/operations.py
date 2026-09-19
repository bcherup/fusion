"""Independent, recorded module configuration."""
import hashlib
import json
import os
from pathlib import Path
import shutil
from .common import *
from .config import module_config, selected, validate_all
from . import base, mail, pbx, recovery, services

MODULE_UNITS={
    'backup':['pbxctl-backup.service'],
    'tls':['certbot.timer'],
    'alerts':['pbxctl-health.timer','pbxctl-health.service'],
    'transcription':['pbxctl-transcribe.timer','pbxctl-transcribe.service','pbx-whisper.service'],
}

def capture_runtime(module):
    return {name:{'active':run(['systemctl','is-active',name],check=False).returncode==0,
                  'enabled':run(['systemctl','is-enabled',name],check=False).stdout.strip()}
            for name in MODULE_UNITS.get(module,[])}

def rollback_change(ch):
    path=ch.path/'runtime.json'
    states=json.loads(path.read_text()) if path.exists() else {}
    for name in states:run(['systemctl','stop',name],check=False)
    ch.rollback()
    run(['systemctl','daemon-reload'])
    for name,state in reversed(list(states.items())):
        if name.endswith('.timer') or name=='pbx-whisper.service':
            if state['enabled'] in ('enabled','enabled-runtime'):
                args=['systemctl','enable']
                if state['enabled']=='enabled-runtime':args+=['--runtime']
                run(args+[name])
            elif state['enabled'] in ('disabled','not-found',''):
                run(['systemctl','disable',name],check=False)
        if state['active']:run(['systemctl','start',name])

def check_managed(marker,db):
    previous=json.loads(marker.read_text())
    for a in json.loads((Path(previous['backup'])/'undo.json').read_text()):
        if a['kind']=='file':
            p=Path(a['path'])
            # Worker progress is live data, never desired configuration.
            if p==STATE/'transcribe/state.json':continue
            need(p.is_file() and not p.is_symlink() and digest(p)==a['after_sha256'],'Managed file changed; review before reconfiguration: '+str(p))
        elif a['kind']=='row':
            rows=db.rows('SELECT * FROM '+identifier(a['table'])+' WHERE '+identifier(a['key'])+'='+literal(a['id']))
            need(len(rows)==1 and all(str(rows[0].get(k)).lower()==str(v).lower() for k,v in a['after_fields'].items()),'Managed database setting changed; review before reconfiguration')
    return previous

def configure(c,modules,allow_restart=False):
    base.supported();validate_all(c);db=Database(c['database'])
    need((ROOT/'VERSION').exists(),'Deploy the toolkit first')
    for p in (STATE,CONFIG.parent):p.mkdir(mode=0o755,parents=True,exist_ok=True)
    pbx.domain(db,c)
    if any(m in modules for m in ('hardening','audio')):
        need(allow_restart,'These modules require --allow-restart during an idle window');idle()
    if 'hardening' in modules:need(c['provider_cidrs'],'Enter verified carrier /32 addresses before hardening')
    previous_config=json.loads(CONFIG.read_text()) if CONFIG.exists() else None
    # Configuration of shared runtime fields must not silently alter unselected modules.
    if previous_config:
        for m in ('tls','hardening','audio','transcription','smtp','alerts','offsite','backup'):
            if m not in modules and (STATE/(m+'.json')).exists():
                desired=json.loads((STATE/(m+'.json')).read_text())['desired']
                desired.pop('credential_digest',None)
                need(module_config(c,m)==desired,'Also select '+m+' because its configuration changes')
    atomic(CONFIG,json.dumps(c,indent=2)+'\n',0o644)
    results=[];restart=False;completed=[]
    try:
        for m in modules:
            marker=STATE/(m+'.json');desired=module_config(c,m)
            # Only a digest of the credential participates in change detection.
            if m=='smtp' and c['smtp']['auth']:
                from .config import read_secret
                desired={**desired,'credential_digest':hashlib.sha256(read_secret(c['smtp']['password_file']).encode()).hexdigest()}
            previous=check_managed(marker,db) if marker.exists() else None
            if previous and previous.get('desired')==desired:
                results.append({'module':m,'status':'unchanged'});continue
            ch=Change(db,m)
            atomic(ch.path/'runtime.json',json.dumps(capture_runtime(m)))
            before_marker=marker.read_bytes() if marker.exists() else None
            try:
                if m in ('audio','hardening'):getattr(pbx,m)(db,c,ch);restart=True
                elif m in ('backup','tls','transcription'):getattr(services,m)(db,c,ch)
                elif m=='smtp':mail.configure(db,c,ch)
                elif m=='alerts':mail.validate_ready(c['smtp']);services.health(db,c,ch)
                elif m=='offsite':
                    if c['remote_backup']['enabled']:
                        from .offsite import environment,execute
                        environment(c);run(['apt-get','install','-y','restic'],timeout=600)
                        execute(c,['snapshots','--json'])
                    # Verify repository access before enabling unattended uploads.
                atomic(marker,json.dumps({'module':m,'desired':desired,'backup':str(ch.path),'enabled':c['transcription_enabled'] if m=='transcription' else True},indent=2))
                results.append({'module':m,'status':'configured','rollback':str(ch.path)})
                completed.append((ch,marker,before_marker))
            except BaseException:
                if previous_config is not None:atomic(CONFIG,json.dumps(previous_config,indent=2)+'\n',0o644)
                else:CONFIG.unlink(missing_ok=True)
                rollback_change(ch);raise
        if any(m in modules for m in ('audio','hardening','smtp','transcription')):
            invalidate(c,['configuration:sofia.conf','configuration:acl.conf','settings:'+c['domain'],'directory:'+c['mailbox']+'@'+c['domain']])
        if 'hardening' in modules:run(['fail2ban-client','reload'])
        if restart:idle();run(['systemctl','restart','freeswitch'],timeout=120)
        for p in Path('/etc/php').glob('*/fpm'):
            run(['systemctl','reload','php'+p.parent.name+'-fpm'],check=False)
        return results
    except BaseException:
        if previous_config is not None:atomic(CONFIG,json.dumps(previous_config,indent=2)+'\n',0o644)
        else:CONFIG.unlink(missing_ok=True)
        for ch,marker,before_marker in reversed(completed):
            rollback_change(ch)
            if before_marker is None:marker.unlink(missing_ok=True)
            else:atomic(marker,before_marker)
        run(['systemctl','daemon-reload'],check=False)
        if 'hardening' in modules:run(['fail2ban-client','reload'],check=False)
        raise

def deploy(source,c):
    base.supported()
    if CONFIG.exists():need(json.loads(CONFIG.read_text())==c,'An installed site differs; use configure to change settings')
    from .updates import tool
    result=tool(source,True)
    CONFIG.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
    if CONFIG.exists():need(json.loads(CONFIG.read_text())==c,'An installed site differs; use configure to change settings')
    else:atomic(CONFIG,json.dumps(c,indent=2)+'\n',0o644)
    STATE.mkdir(mode=0o755,parents=True,exist_ok=True);STATE.chmod(0o755)
    for top,dirs,files in os.walk(ROOT):os.chmod(top,0o755)
    atomic('/usr/local/sbin/pbxctl','#!/bin/sh\nexec /usr/bin/python3 /opt/pbxctl/pbxctl.py "$@"\n',0o755)
    run(['apt-get','install','-y','python3-cryptography','ca-certificates','curl','rsync','openssh-client','iptables','iptables-persistent','netfilter-persistent'],timeout=600)
    return result
