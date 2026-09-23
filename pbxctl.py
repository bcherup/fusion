#!/usr/bin/env python3
"""PBX installation, configuration, recovery, and updates for Debian 13."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.dont_write_bytecode=True
from lib.common import CONFIG, ROOT, STATE, Error, atomic, idle, need, run
from lib.config import MODULES, load_config, prompt_secret, selected, wizard, validate_all
from lib.features import KEYS

SOURCE=Path(__file__).resolve().parent

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',nargs='?',default='menu',choices=['menu','setup','plan','install','deploy','configure','feature','certificate','smtp-credential','test-email','backup','verify-backup','restore','activate','migrate','check','check-media','firewall','update','offsite-init','offsite-upload','offsite-restore','retention','rollback','restore-summary-text'])
    p.add_argument('--config',help='Site configuration; setup saves a candidate before configure applies it')
    p.add_argument('--modules',help='Comma-separated optional modules: '+','.join(MODULES))
    p.add_argument('--apply',action='store_true',help='Perform the displayed action; otherwise show a plan')
    p.add_argument('--allow-restart',action='store_true');p.add_argument('--source-stopped',action='store_true')
    p.add_argument('--archive');p.add_argument('--destination');p.add_argument('--source');p.add_argument('--uuid')
    p.add_argument('--target',choices=['pbx','tool'],default='pbx');p.add_argument('--fetch',action='store_true')
    p.add_argument('--confirm');p.add_argument('--snapshot',default='latest');p.add_argument('--agree-acme-tos',action='store_true')
    p.add_argument('--ssh-user',default='root');p.add_argument('--ssh-host');p.add_argument('--ssh-port',type=int,default=22)
    p.add_argument('--name',choices=[*KEYS,'transcription'],help='Feature to change')
    toggle=p.add_mutually_exclusive_group();toggle.add_argument('--enable',action='store_true');toggle.add_argument('--disable',action='store_true')
    p.add_argument('--gain-db',type=float);p.add_argument('--read-level',type=int);p.add_argument('--write-level',type=int)
    return p

def menu():
    print('PBX maintenance — Debian 13 Trixie')
    choices=['setup','install','configure','feature','backup','restore','check','update','firewall']
    for n,x in enumerate(choices,1):print(str(n)+'. '+x)
    answer=input('Choose an operation (Enter exits): ').strip()
    if not answer:return
    need(answer.isdigit() and 1<=int(answer)<=len(choices),'Invalid choice')
    action=choices[int(answer)-1];args=[action]
    candidate=Path('/root/pbxctl-site.json') if CONFIG.exists() else Path('site.json')
    default=candidate if action=='setup' or candidate.exists() else CONFIG if CONFIG.exists() else SOURCE/'site.example.json'
    args+=['--config',input('Site configuration file ['+str(default)+']: ').strip() or str(default)]
    if action in ('configure','install'):
        print('Optional modules: '+', '.join(MODULES));mods=input('Modules to configure (empty skips): ').strip()
        if mods:args+=['--modules',mods]
    if action=='feature':
        print('Features: '+', '.join([*KEYS,'transcription']))
        name=input('Feature: ').strip();args+=['--name',name]
        args+=['--enable' if input('Enable or disable? [enable]: ').strip()!='disable' else '--disable']
        if name=='hold-music':
            value=input('Gain dB (Enter keeps configured value): ').strip()
            if value:args+=['--gain-db',value]
        if name=='call-volume':
            for flag in ('--read-level','--write-level'):
                value=input(flag+' -4 to 4 (Enter keeps configured value): ').strip()
                if value:args+=[flag,value]
    if action=='restore':args+=['--archive',input('Recovery directory: ').strip()]
    if action=='update' and input('Update PBX or toolkit? [pbx]: ').strip()=='tool':
        args+=['--target','tool','--source',input('Downloaded toolkit release directory: ').strip()]
    result=main(args)
    if result is not None:print(json.dumps(result,indent=2))
    if action not in ('setup','check') and input('Apply this operation? Type APPLY: ').strip()=='APPLY':
        if action in ('configure','feature','update') and input('Allow an idle service restart? y/N: ').lower()=='y':args+=['--allow-restart']
        result=main(args+['--apply'])
        if result is not None:print(json.dumps(result,indent=2))

def media(uuid):
    import re
    need(uuid and re.fullmatch(r'[0-9a-fA-F-]{36}',uuid),'Provide the active test-call UUID')
    result={}
    need(run(['fs_cli','-x','uuid_exists '+uuid]).stdout.strip()=='true','Call is no longer active')
    for key in ('rtp_secure_audio_confirmed','rtp_has_crypto','sofia_profile_name','read_codec','write_codec'):
        value=run(['fs_cli','-x','uuid_getvar '+uuid+' '+key]).stdout.strip()
        need(len(value)<120 and '\n' not in value,'Unexpected channel metadata')
        result[key]=None if value in ('_undef_','') else value
    result['encrypted_audio_confirmed']=result['rtp_secure_audio_confirmed']=='true'
    result['scope']='Selected call leg only; test incoming/push and carrier legs separately'
    return result

def main(argv=None):
    a=parser().parse_args(argv)
    if a.action=='menu':return menu()
    if a.action=='setup':
        candidate=Path(a.config) if a.config else Path('/root/pbxctl-site.json') if CONFIG.exists() else Path('site.json')
        need(candidate.resolve()!=CONFIG.resolve(),'Save a candidate file, then apply it with configure --config PATH')
        return wizard(candidate,CONFIG if CONFIG.exists() else SOURCE/'site.example.json')
    if not a.config:a.config=str(CONFIG if CONFIG.exists() else Path('site.json') if Path('site.json').exists() else SOURCE/'site.example.json')
    c=load_config(a.config);mods=selected(a.modules) if a.modules else []
    if a.action=='feature':
        need(a.name and (a.enable or a.disable),'Choose --name and --enable or --disable')
        need(a.gain_db is None or a.name=='hold-music','--gain-db requires hold-music')
        need((a.read_level is None and a.write_level is None) or a.name=='call-volume','Call gain flags require call-volume')
        if a.name=='transcription':c['transcription_enabled']=a.enable
        else:c[KEYS[a.name]]['enabled']=a.enable
        if a.gain_db is not None:c['hold_music']['gain_db']=a.gain_db
        for attr in ('read_level','write_level'):
            if getattr(a,attr) is not None:c['call_volume'][attr]=getattr(a,attr)
        c=validate_all(c);mods=[a.name];a.action='configure'
    if a.action=='plan':return {'os':'Debian 13 Trixie','selected':mods,'available':MODULES,'smtp':'Generic STARTTLS/implicit TLS or authorized relay','transcription':'Optional; no Whisper download when skipped','update':'Explicit PBX fast-forward or separately verified toolkit release','changes':False}
    if a.action in ('install','deploy','configure','certificate','restore','activate','migrate','offsite-init','offsite-upload','offsite-restore','retention','rollback','restore-summary-text') and not a.apply:
        result={'action':a.action,'modules':mods,'domain':c['domain'],'apply_required':True}
        if a.action=='configure':result['proposed']={m:__import__('lib.config',fromlist=['module_config']).module_config(c,m) for m in mods}
        if a.action in ('restore','verify-backup'):result['archive']=a.archive
        return result
    from lib import base
    base.supported()
    # All mutating actions use a shared lock; standalone backup/firewall have their own locks too.
    import fcntl
    with open('/run/lock/pbxctl.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return dispatch(a,c,mods)

def dispatch(a,c,mods):
    from lib import base, operations, recovery, updates, offsite
    if a.action=='install':
        result=base.install(c);operations.deploy(SOURCE,c)
        if mods:result['modules']=operations.configure(c,mods,a.allow_restart)
        return result
    if a.action=='deploy':return operations.deploy(SOURCE,c)
    if a.action=='configure':need(mods,'Select at least one --modules value');return operations.configure(c,mods,a.allow_restart)
    if a.action=='restore-summary-text':
        need(not c['ai_summary']['enabled'],'Disable AI summaries before restoring original transcripts')
        run(['runuser','-u',c['service_user'],'--','php',ROOT/'assets/ai/restore-summaries.php'])
        return {'restored':'Only unchanged generated summaries; manual edits preserved'}
    if a.action=='smtp-credential':prompt_secret(c['smtp']['password_file'],'SMTP password or provider app password');return {'saved':True}
    if a.action=='test-email':
        need(a.apply,'Use --apply to send a test email to the configured recipient')
        from lib.mail import send
        send(c,'PBX email test','SMTP transport test. Confirm receipt in your inbox.');return {'submitted':True,'delivery':'Confirm receipt in the inbox'}
    if a.action=='certificate':
        need(a.agree_acme_tos,'Accept the ACME terms with --agree-acme-tos')
        from lib.config import read_secret
        p=Path(c['cloudflare_credentials_file']);need(p.is_file() and p.stat().st_uid==0 and not p.stat().st_mode&0o077,'Private DNS credential file required')
        run(['apt-get','install','-y','certbot','python3-certbot-dns-cloudflare'],timeout=600)
        run(['certbot','certonly','--non-interactive','--agree-tos','--email',c['acme_email'],'--dns-cloudflare','--dns-cloudflare-credentials',p,'--dns-cloudflare-propagation-seconds','60','--cert-name',c['domain'],'-d',c['domain']],timeout=600)
        return {'issued':True,'next':'configure --modules tls --apply'}
    if a.action=='backup':
        if not a.apply:return {'action':'backup','apply_required':True}
        directory=recovery.backup(c)
        if c['remote_backup']['enabled']:offsite.upload(c,directory)
        return {'backup':str(directory)}
    if a.action=='verify-backup':
        need(a.archive,'Provide --archive');m=recovery.verify(a.archive,c)
        if a.apply:recovery.scratch_restore(a.archive,c)
        return {'checksums':'passed','scratch_restore':'passed' if a.apply else 'not requested','manifest':m}
    if a.action=='restore':
        need(a.archive,'Provide --archive')
        if run(['systemctl','is-active','freeswitch'],check=False).returncode==0:idle()
        return recovery.restore(a.archive,c)
    if a.action=='activate':need(a.source_stopped,'Stop the original PBX, then pass --source-stopped');return recovery.activate()
    if a.action=='migrate':
        from lib.migrate import pull
        return pull(a,c)
    if a.action=='update':
        if a.target=='tool':need(a.source,'Select a downloaded release with --source');return updates.tool(a.source,a.apply)
        if not a.apply:return {'repositories':updates.plan(c,a.fetch),'apply_required':True}
        need(a.allow_restart,'PBX updates require --allow-restart for an idle maintenance window');return updates.apply(c)
    if a.action=='check-media':return media(a.uuid)
    if a.action=='check':
        from lib.health import inspect
        return inspect(c)
    if a.action=='firewall':
        from lib import firewall
        if a.archive:
            need(not a.confirm,'Choose confirmation or rollback')
            if not a.apply:return {'rollback':a.archive,'apply_required':True}
            return firewall.rollback(a.archive,automatic=False)
        return firewall.confirm(a.confirm) if a.confirm else firewall.apply(c) if a.apply else firewall.check(c)
    if a.action=='offsite-init':
        from lib.config import read_secret
        need(c['remote_backup']['enabled'] and c['remote_backup']['repository'],'Configure a remote backup repository first')
        run(['apt-get','install','-y','restic'],timeout=600)
        try:read_secret(c['remote_backup']['password_file'])
        except (Error,OSError):prompt_secret(c['remote_backup']['password_file'],'New encrypted backup repository password')
        offsite.execute(c,['init']);return {'initialized':True}
    if a.action=='offsite-upload':need(a.archive,'Provide --archive');return offsite.upload(c,a.archive)
    if a.action=='offsite-restore':
        need(a.destination and not Path(a.destination).exists(),'Choose a new empty destination path')
        need(a.snapshot and not a.snapshot.startswith('-'),'Invalid snapshot')
        offsite.execute(c,['restore',a.snapshot,'--target',str(Path(a.destination).resolve())]);return {'restored_files':a.destination,'next':'verify-backup --archive RECOVERED_DIRECTORY --apply'}
    if a.action=='retention':offsite.retention(c,a.apply);return {'pruning_applied':a.apply}
    if a.action=='rollback':
        from lib.common import Database,rollback
        need(a.archive,'Provide the exact module snapshot using --archive');idle()
        markers=[p for p in STATE.glob('*.json') if isinstance((m:=json.loads(p.read_text())),dict) and m.get('backup')==str(Path(a.archive))]
        need(len(markers)==1,'Select the current installed module snapshot')
        from types import SimpleNamespace
        change=SimpleNamespace(path=Path(a.archive),rollback=lambda:rollback(a.archive,Database(c['database'])))
        operations.rollback_change(change);markers[0].unlink()
        run(['systemctl','daemon-reload'])
        if markers[0].stem=='hardening':run(['fail2ban-client','reload'])
        return {'rolled_back':a.archive,'next':'Review required service activation before restarting'}

if __name__=='__main__':
    try:
        result=main()
        if result is not None:print(json.dumps(result,indent=2))
    except (Error,OSError,ValueError,KeyError,RuntimeError) as e:
        print('Stopped: '+(str(e) if isinstance(e,Error) else type(e).__name__+'; inspect local prerequisites'),file=sys.stderr);sys.exit(1)
