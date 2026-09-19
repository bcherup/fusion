"""Install systemd services and pinned local recognition runtime."""
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import time
from .common import *
from . import pbx

COMMIT='927cfce34f31707e17f2bff35c349632fb9e2c3a'
MODEL_SHA='a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002'
ASSETS=ROOT/'assets'

def unit(ch,name,text): ch.file('/etc/systemd/system/'+name,text)

def timer(ch,name,period):
    unit(ch,name+'.timer','[Unit]\nDescription=PBX '+name+' timer\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec='+period+'\nAccuracySec=15s\n[Install]\nWantedBy=timers.target\n')

def wrapper(ch,name,asset,interpreter='/usr/bin/python3'):
    ch.file('/usr/local/sbin/'+name,'#!/bin/sh\nexec '+interpreter+' '+str(ASSETS/asset)+' "$@"\n',0o755)

def backup(db,c,ch):
    wrapper(ch,'pbxctl-backup','backup.py')
    unit(ch,'pbxctl-backup.service','''[Unit]
Description=Private PBX recovery archive
After=postgresql.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pbxctl-backup
UMask=0077
Nice=15
IOSchedulingClass=idle
''')
    # Replace the official daily credential-bearing job with peer-authenticated backup.
    ch.file('/etc/cron.daily/fusionpbx-backup','#!/bin/sh\nexec systemctl start pbxctl-backup.service\n',0o750)
    for name in ('/var/backups/pbxctl',):
        p=Path(name); need(not p.is_symlink(),'Backup path must not be a symlink')
        p.mkdir(mode=0o700,exist_ok=True); p.chmod(0o700)
    run(['systemctl','daemon-reload'])
    run(['systemctl','start','pbxctl-backup.service'],timeout=1800)

def health(db,c,ch):
    wrapper(ch,'pbxctl-health','health.py')
    wrapper(ch,'pbxctl-upgrade-check','upgrade-check.py')
    wrapper(ch,'pbxctl-unban','unban.py')
    unit(ch,'pbxctl-health.service','''[Unit]
Description=PBX health check
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pbxctl-health
UMask=0077
''')
    timer(ch,'pbxctl-health','5min')
    run(['systemctl','daemon-reload'])
    run(['systemctl','enable','--now','pbxctl-health.timer'])

def transcription(db,c,ch):
    if not c['transcription_enabled']:
        need((STATE/'transcription.json').exists(),'No toolkit-managed transcription to disable')
        for name in ('pbxctl-transcribe.timer','pbxctl-transcribe.service','pbx-whisper.service'):
            run(['systemctl','disable','--now',name],check=False)
        d=pbx.domain(db,c);pbx.domain_setting(db,ch,d,'transcribe','enabled','false','boolean')
        box=db.one('SELECT voicemail_uuid FROM v_voicemails WHERE domain_uuid='+literal(d)+' AND voicemail_id='+literal(c['mailbox']))
        ch.row('v_voicemails','voicemail_uuid',{**box,'voicemail_transcription_enabled':False})
        return
    run(['apt-get','install','-y','cmake','build-essential','ffmpeg','curl','php-curl'],timeout=1800)
    app=Path(c['web_root'])/'app/transcribe'
    if not app.exists():
        run(['git','clone','https://github.com/fusionpbx/fusionpbx-app-transcribe.git',app],timeout=600)
        upstream=run(['git','-C',app,'symbolic-ref','--short','refs/remotes/origin/HEAD']).stdout.strip()
        run(['git','-C',app,'checkout','-B','local-stable','d0c5420e945a1303f7f9bc49c027820d5b72deda'])
        run(['git','-C',app,'branch','--set-upstream-to='+upstream])
        for top,dirs,files in os.walk(app):
            os.chmod(top,0o755)
            for name in files:
                f=Path(top)/name
                if not f.is_symlink():f.chmod(0o755 if f.stat().st_mode&0o111 else 0o644)
        run(['php',Path(c['web_root'])/'core/upgrade/upgrade.php','--schema'],timeout=600)
        run(['php',Path(c['web_root'])/'core/upgrade/upgrade.php','--defaults'],timeout=600)
    need((Path(c['web_root'])/'app/transcribe').is_dir(),
         'Install the official Transcribe app and run FusionPBX schema/default upgrades first (README)')
    # Never coexist with the old worker: migration needs preservation of its cutoff/retry state.
    need(not Path('/etc/systemd/system/fusionpbx-local-transcribe.timer').exists(),
         'Existing custom transcription worker detected; use a reviewed migration, not a fresh install')
    need(not Path('/etc/systemd/system/pbx-whisper.service').exists() or (STATE/'transcription.json').exists(),
         'An existing Whisper service needs a reviewed migration, not a fresh install')
    if run(['id','-u','pbx-whisper'],check=False).returncode:
        run(['useradd','--system','--home-dir','/nonexistent','--no-create-home','--shell','/usr/sbin/nologin','pbx-whisper'])
    base=Path('/opt/pbx-whisper'); base.mkdir(mode=0o755,exist_ok=True); base.chmod(0o755)
    source=base/'source'; build=base/'build'; model=base/'models/ggml-base.en.bin'
    need(not source.is_symlink() and not build.is_symlink() and not model.is_symlink(),'Whisper paths cannot be symlinks')
    if not source.exists():
        run(['git','clone','--branch','v1.9.4','--depth','1','https://github.com/ggml-org/whisper.cpp.git',source],timeout=600)
    need(run(['git','-c','safe.directory='+str(source),'-C',source,'rev-parse','HEAD']).stdout.strip()==COMMIT,'Whisper source pin differs')
    need(not run(['git','-c','safe.directory='+str(source),'-C',source,'status','--porcelain']).stdout.strip(),'Whisper source has local changes')
    # Change snapshots use umask 077, so make only public source/build assets
    # traversable for the unprivileged compiler/runtime; leave secrets private.
    for top,dirs,files in os.walk(source):
        os.chmod(top,0o755)
        for name in files:
            p=Path(top)/name
            if not p.is_symlink(): p.chmod(0o755 if p.stat().st_mode&0o111 else 0o644)
    model.parent.mkdir(mode=0o755,exist_ok=True)
    model.parent.chmod(0o755)
    if not model.exists():
        temporary=model.with_suffix('.download')
        need(not temporary.is_symlink(),'Unsafe model download path')
        run(['curl','--fail','--location','--proto','=https','--tlsv1.2','--output',temporary,
             'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin'],timeout=1800)
        need(digest(temporary)==MODEL_SHA,'Model checksum mismatch; download was not activated')
        temporary.replace(model)
    need(digest(model)==MODEL_SHA,'Existing model checksum differs')
    model.chmod(0o644)
    build.mkdir(mode=0o755,exist_ok=True)
    build.chmod(0o755)
    owner=pwd.getpwnam('pbx-whisper')
    for top,dirs,files in os.walk(build):
        os.chown(top,owner.pw_uid,owner.pw_gid)
        for n in files:
            p=Path(top)/n
            if not p.is_symlink(): os.chown(p,owner.pw_uid,owner.pw_gid)
    try:
        run(['runuser','-u','pbx-whisper','--','cmake','-S',source,'-B',build,'-DCMAKE_BUILD_TYPE=Release',
             '-DBUILD_SHARED_LIBS=OFF','-DWHISPER_BUILD_TESTS=OFF','-DWHISPER_CURL=OFF'],timeout=300)
        run(['runuser','-u','pbx-whisper','--','cmake','--build',build,'--target','whisper-server','-j','1'],timeout=3600)
    finally:
        for top,dirs,files in os.walk(build):
            os.chown(top,0,0)
            os.chmod(top,0o755)
            for n in files:
                p=Path(top)/n
                if not p.is_symlink():
                    os.chown(p,0,0)
                    p.chmod(0o755 if p.stat().st_mode&0o111 else 0o644)
    unit(ch,'pbx-whisper.service',f'''[Unit]
Description=Local voicemail speech recognition
After=network.target
[Service]
User=pbx-whisper
Group=pbx-whisper
ExecStart={build}/bin/whisper-server -m {model} --host 127.0.0.1 --port {c['whisper_port']} -t 1 -p 1 -l en --convert --no-gpu
Restart=on-failure
RestartSec=10
CPUQuota={c['whisper_cpu_percent']}%
MemoryHigh={int(c['whisper_memory_mb']*0.78)}M
MemoryMax={c['whisper_memory_mb']}M
Nice=15
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_UNIX
IPAddressDeny=any
IPAddressAllow=localhost
StandardOutput=null
StandardError=null
[Install]
WantedBy=multi-user.target
''')
    run(['systemctl','daemon-reload']); run(['systemctl','enable','pbx-whisper.service'])
    run(['systemctl','restart','pbx-whisper.service'])
    ready=False
    for _ in range(45):
        response=run(['curl','--noproxy','*','--fail','--silent','--max-time','2','http://127.0.0.1:'+str(c['whisper_port'])+'/'],check=False)
        if response.returncode==0: ready=True; break
        time.sleep(1)
    need(ready,'Whisper did not become ready; inspect systemctl status')
    owner=pwd.getpwnam(c['service_user']); group=grp.getgrnam(c['service_group'])
    state=STATE/'transcribe'; state.mkdir(mode=0o700,exist_ok=True); os.chown(state,owner.pw_uid,group.gr_gid)
    p=state/'state.json'
    if not p.exists():atomic(p,json.dumps({'domain_uuid':pbx.domain(db,c),'since':int(time.time()),'attempts':{}}),0o600,(owner.pw_uid,group.gr_gid))
    else:need(json.loads(p.read_text())['domain_uuid']==pbx.domain(db,c),'Existing transcription belongs to another domain')
    link=Path(c['web_root'])/'app/pbxctl'
    if link.is_symlink():need(link.resolve()==(ROOT/'assets/app').resolve(),'Custom app link differs')
    else:ch.symlink(link,ROOT/'assets/app')
    # Autoload cache falls back to scanning on a class miss; CLI probe validates the real interface.
    probe="require "+json.dumps(c['web_root']+'/resources/require.php')+"; exit(class_exists('transcribe_pbxctl_local') ? 0 : 1);"
    run(['runuser','-u',c['service_user'],'--','php','-r',probe])
    pbx.transcription_settings(db,c,ch)
    # Refresh long-lived PHP APCu settings after changing this domain's engine.
    for p in Path('/etc/php').glob('*/fpm'):
        service='php'+p.parent.name+'-fpm'
        if run(['systemctl','is-active',service],check=False).returncode==0:
            run(['systemctl','reload',service])
    unit(ch,'pbxctl-transcribe.service',f'''[Unit]
Description=Transcribe new opted-in voicemail locally
After=postgresql.service pbx-whisper.service
[Service]
Type=oneshot
User={c['service_user']}
Group={c['service_group']}
ExecStart=/usr/bin/php {ASSETS}/voicemail-transcribe.php
TimeoutStartSec=660
CPUQuota=20%
MemoryMax=256M
Nice=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
''')
    timer(ch,'pbxctl-transcribe','60s')
    run(['systemctl','daemon-reload']); run(['systemctl','enable','--now','pbxctl-transcribe.timer'])

def tls(db,c,ch):
    need((Path('/etc/letsencrypt/live')/c['domain']/'fullchain.pem').exists(),'Issue a certificate first: pbxctl certificate --apply --agree-acme-tos')
    wrapper(ch,'pbxctl-cert-deploy','cert-deploy.py')
    ch.file('/etc/letsencrypt/renewal-hooks/deploy/50-pbxctl','#!/bin/sh\nexec /usr/local/sbin/pbxctl-cert-deploy\n',0o750)
    run(['/usr/local/sbin/pbxctl-cert-deploy','--prepare'])
    run(['systemctl','enable','--now','certbot.timer'])
