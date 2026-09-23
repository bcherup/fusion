"""Versioned recovery sets, archive validation, staged restore, and cutover."""
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import tarfile
import uuid
from .common import CONFIG, ROOT, STATE, Database, atomic, digest, identifier, literal, need, run

ARCHIVES=Path('/var/backups/pbxctl')
RESTORES=STATE/'restores'
QUIET_UNITS=('pbxctl-ai-summary.timer','pbxctl-ai-summary.service','pbxctl-ai-model.service','pbx-whisper.service','pbxctl-transcribe.timer','pbxctl-transcribe.service','pbxctl-health.timer','fusionpbx-local-transcribe.timer','fusionpbx-local-transcribe.service','fusionpbx-health.timer','email_queue.service','transcribe_queue.service','fax_queue.service','freeswitch.service','nginx.service')

def roots(c):
    return [c['web_root'],c['freeswitch_conf'],c['freeswitch_scripts'],'/etc/fusionpbx','/etc/freeswitch-tls','/etc/letsencrypt',
        '/etc/nginx','/etc/php','/etc/fail2ban','/etc/iptables','/etc/systemd/system','/etc/cron.d','/etc/cron.daily/fusionpbx-backup',
        '/usr/local/sbin','/usr/local/lib/fusionpbx-local','/var/lib/freeswitch/storage','/var/lib/freeswitch/recordings',
        '/usr/share/freeswitch/sounds','/opt/pbx-whisper/models','/opt/pbx-whisper/build/bin',str(ROOT),str(CONFIG.parent),str(STATE),
        '/opt/fusionpbx-personal','/etc/fusionpbx-personal','/var/lib/fusionpbx-personal','/var/lib/fusionpbx-local-transcribe','/opt/pbxctl-ai']

def inventory(c):
    return {'domain':c['domain'],'lan_ip':c['lan_ip'],'database':c['database'],'architecture':platform.machine(),
            'postgres':run(['pg_dump','--version']).stdout.strip(),
            'freeswitch':run(['freeswitch','-version']).stdout.strip(),
            'web_root':c['web_root'],'freeswitch_conf':c['freeswitch_conf'],'freeswitch_scripts':c['freeswitch_scripts'],
            'active_services':[name for name in QUIET_UNITS if run(['systemctl','is-active',name],check=False).returncode==0]}

def backup(c):
    os.umask(0o077);ARCHIVES.mkdir(parents=True,exist_ok=True,mode=0o700)
    need(not ARCHIVES.is_symlink(),'Backup directory cannot be linked')
    need(shutil.disk_usage(ARCHIVES).free>=c['backup_min_free_gib']*1024**3,'Free disk below backup reserve')
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    stage=ARCHIVES/(stamp+'.partial');stage.mkdir(mode=0o700)
    Database(c['database']).dump(stage/'database.dump')
    names=[x for x in roots(c) if Path(x).exists()]
    # Renewal credentials are recovery data; keep the entire archive private.
    for file in Path('/etc/letsencrypt/renewal').glob('*.conf'):
        for line in file.read_text().splitlines():
            if line.strip().startswith('dns_cloudflare_credentials') and '=' in line:
                p=Path(line.split('=',1)[1].strip())
                if p.is_absolute() and p.is_file():names.append(str(p))
    def include(info):
        # Never recursively archive recovery sets or incomplete restore staging.
        if info.name==str(RESTORES).lstrip('/') or info.name.startswith(str(RESTORES).lstrip('/')+'/'):return None
        return info
    with tarfile.open(stage/'files.tar.gz','w:gz',compresslevel=1) as tar:
        for name in sorted(set(names)):tar.add(name,arcname=name.lstrip('/'),filter=include)
    m={'format':'pbxctl-recovery-1','created_utc':stamp,'inventory':inventory(c),'roots':sorted(set(names)),
       'sha256':{n:digest(stage/n) for n in ('database.dump','files.tar.gz')},
       'consistency':'PostgreSQL consistent dump; live media copy. Quiesce source for final cutover.'}
    atomic(stage/'manifest.json',json.dumps(m,indent=2));verify(stage,c)
    final=ARCHIVES/stamp;stage.rename(final)
    atomic(ARCHIVES/'last-success.json',json.dumps({'created_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'path':str(final)}))
    return final

def safe_member(member,allowed):
    p=PurePosixPath(member.name)
    need(not p.is_absolute() and '..' not in p.parts and '\\' not in member.name and p.parts,'Unsafe archive member')
    absolute='/'+str(p)
    need(any(absolute==r or absolute.startswith(r+'/') for r in allowed),'Archive member outside declared roots')
    need(member.isfile() or member.isdir() or member.issym() or member.islnk(),'Special archive members are refused')
    if member.issym() or member.islnk():
        need('\\' not in member.linkname,'Unsafe archive link')
        if member.islnk():target=PurePosixPath('/'+member.linkname.lstrip('/'))
        elif member.linkname.startswith('/'):target=PurePosixPath(member.linkname)
        else:target=PurePosixPath(absolute).parent/member.linkname
        normalized=os.path.normpath(str(target)).replace('\\','/')
        link_roots=allowed+['/lib/systemd/system','/usr/lib/systemd/system','/dev/null']
        need(any(normalized==r or normalized.startswith(r+'/') for r in link_roots),'Archive link leaves permitted roots')
    return absolute

def verify(directory,c):
    directory=Path(directory).resolve();need(directory.is_dir(),'Recovery directory missing')
    m=json.loads((directory/'manifest.json').read_text())
    need(m['format']=='pbxctl-recovery-1','Unsupported recovery format; import legacy backups explicitly')
    need(set(m['sha256'])=={'database.dump','files.tar.gz'},'Unexpected recovery files')
    for name,h in m['sha256'].items():
        need(not (directory/name).is_symlink() and digest(directory/name)==h,'Recovery checksum mismatch: '+name)
    allowed=roots(c)
    # DNS credential recovery paths are permitted only under the standard secret directory.
    allowed+=['/root/.secrets/certbot']
    need(set(m['roots'])<=set(allowed) or all(any(p==r or p.startswith(r+'/') for r in allowed) for p in m['roots']), 'Unsupported recovery root')
    seen=set();links=[]
    with tarfile.open(directory/'files.tar.gz','r:gz') as tar:
        for member in tar:
            name=safe_member(member,allowed);need(name not in seen,'Duplicate archive member');seen.add(name)
            if member.issym():links.append(name)
        need(not any(n.startswith(link+'/') for link in links for n in seen),'Archive traverses a symlink')
    run(['pg_restore','--list',directory/'database.dump'])
    return m

def scratch_restore(directory,c):
    name='pbxctl_verify_'+uuid.uuid4().hex[:16];created=False
    try:
        run(['runuser','-u','postgres','--','createdb',name]);created=True
        with (Path(directory)/'database.dump').open('rb') as f:
            p=subprocess.run(['runuser','-u','postgres','--','pg_restore','--exit-on-error','--no-owner','--no-privileges','-d',name],stdin=f,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=600)
        need(p.returncode==0,'Scratch database restore failed; destination untouched')
        need(Database(name).rows('SELECT domain_name FROM v_domains WHERE domain_name='+literal(c['domain'])),'Requested domain absent from restored data')
    finally:
        if created:run(['runuser','-u','postgres','--','dropdb','--force',name])

def stop_services():
    active=[]
    for name in QUIET_UNITS:
        if run(['systemctl','is-active',name],check=False).returncode==0:
            active.append(name);run(['systemctl','stop',name])
    return active

def restore_plan(directory,c):
    m=verify(directory,c);i=m['inventory']
    need(i['domain']==c['domain'],'Restore must preserve the source SIP domain')
    for k in ('database','web_root','freeswitch_conf','freeswitch_scripts'):need(i[k]==c[k],'Restore layout differs: '+k)
    need(i['architecture']==platform.machine(),'Runtime architecture differs')
    return {'source':str(Path(directory).resolve()),'source_identity':i,'destination_lan_ip':c['lan_ip'],
            'preserve':'Destination database credentials, host identity, network configuration, firewall rules',
            'activation':'Services remain stopped and masked until explicit activate --source-stopped'}

def protected(name,c):
    return name in ('/etc/fusionpbx/config.conf','/etc/fusionpbx/config.php','/etc/fusionpbx/config.lua',
                    c['web_root']+'/resources/config.php',c['freeswitch_scripts']+'/resources/functions/config.lua',str(CONFIG)) or name.startswith('/etc/iptables/')

def restore(directory,c):
    plan=restore_plan(directory,c);scratch_restore(directory,c)
    need(run(['freeswitch','-version']).stdout.strip()==plan['source_identity']['freeswitch'],'FreeSWITCH version differs; install matching runtime before restoring')
    import pwd,grp
    with tarfile.open(Path(directory)/'files.tar.gz','r:gz') as tar:
        user_names={m.uname for m in tar if m.uname}
        group_names={m.gname for m in tar if m.gname}
    if 'pbx-whisper' in user_names and run(['id','-u','pbx-whisper'],check=False).returncode:
        run(['useradd','--system','--home-dir','/nonexistent','--no-create-home','--shell','/usr/sbin/nologin','pbx-whisper'])
        run(['apt-get','install','-y','ffmpeg','php-curl'],timeout=600)
    if 'pbxctl-ai' in user_names|group_names and run(['id','-u','pbxctl-ai'],check=False).returncode:
        run(['useradd','--system','--home-dir','/nonexistent','--no-create-home','--shell','/usr/sbin/nologin','pbxctl-ai'])
        run(['apt-get','install','-y','libgomp1','php-curl'],timeout=600)
    for name in user_names:pwd.getpwnam(name)
    snapshot=backup(c);active=stop_services()
    stage=RESTORES/uuid.uuid4().hex;stage.mkdir(parents=True,mode=0o700)
    atomic(stage/'transaction.json',json.dumps({'backup':str(snapshot),'active_before':active,'source':str(directory),'phase':'replacing'},indent=2))
    # Block boot/service activation before replacing any data, including a failed restore.
    (STATE/'cutover-approved').unlink(missing_ok=True)
    for name in QUIET_UNITS:
        atomic('/etc/systemd/system/'+name+'.d/99-pbxctl-stage.conf','[Unit]\nConditionPathExists=/var/lib/pbxctl/cutover-approved\n',0o644)
    run(['systemctl','daemon-reload'])
    web=Path(c['web_root']);need(web.is_dir() and not web.is_symlink(),'Destination application root must be a real directory')
    saved_web_credentials={}
    local_config=web/'resources/config.php'
    if local_config.is_file() and not local_config.is_symlink():
        st=local_config.stat();saved_web_credentials[str(local_config)]=(local_config.read_bytes(),st.st_mode&0o777,st.st_uid,st.st_gid)
    previous_web=web.with_name(web.name+'.before-restore-'+uuid.uuid4().hex[:8])
    web.rename(previous_web)
    # Stage files manually: never extract directly over / or follow archive links.
    with tarfile.open(Path(directory)/'files.tar.gz','r:gz') as tar:
        for member in tar:
            name='/'+member.name
            if protected(name,c) or name in (str(STATE/'cutover-approved'),str(STATE/'update-in-progress')) or name.endswith('/99-pbxctl-stage.conf'):continue
            # Restore only toolkit-owned system units/wrappers. Base services stay from the matching destination installation.
            if name.startswith('/etc/systemd/system/') and not Path(name).name.startswith(('pbxctl-','pbx-whisper.','fusionpbx-local-','fusionpbx-health.')):continue
            if name.startswith('/usr/local/sbin/') and not Path(name).name.startswith(('pbxctl','fusionpbx-')):continue
            if name.startswith('/etc/cron.d/'):continue
            if member.isdir():
                p=Path(name);need(not p.is_symlink() and not any(parent.is_symlink() for parent in p.parents),'Linked restore directory')
                p.mkdir(parents=True,exist_ok=True)
                p.chmod(member.mode&0o777)
                import pwd,grp
                os.chown(p,pwd.getpwnam(member.uname).pw_uid if member.uname else member.uid,grp.getgrnam(member.gname).gr_gid if member.gname else member.gid)
                continue
            p=Path(name)
            need(not any(parent.is_symlink() for parent in p.parents),'Destination parent is linked: '+str(p.parent))
            if member.issym():
                need(not p.exists() or p.is_symlink(),'Restore link would replace a regular file')
                p.parent.mkdir(parents=True,exist_ok=True)
                if p.is_symlink():p.unlink()
                p.symlink_to(member.linkname);continue
            with tar.extractfile(member) as f:atomic(p,f.read(),member.mode&0o777)
            import pwd,grp
            uid=pwd.getpwnam(member.uname).pw_uid if member.uname else member.uid
            gid=grp.getgrnam(member.gname).gr_gid if member.gname else member.gid
            os.chown(p,uid,gid)
    for name,(content,mode,uid,gid) in saved_web_credentials.items():atomic(name,content,mode,(uid,gid))
    # Destination connection configuration is retained. Restore objects as its application role.
    role=identifier(c['database']);db=Database(c['database'])
    db.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION '+role+';')
    with (Path(directory)/'database.dump').open('rb') as f:
        p=subprocess.run(['runuser','-u','postgres','--','pg_restore','--exit-on-error','--no-owner','--no-privileges','--role='+role,'-d',c['database']],stdin=f,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=600)
    need(p.returncode==0,'Destination restore failed; services remain stopped. Use the recorded recovery backup.')
    db.execute("UPDATE v_sip_profile_settings SET sip_profile_setting_value="+literal(c['lan_ip'])+" WHERE sip_profile_setting_name IN ('sip-ip','rtp-ip') AND sip_profile_setting_value="+literal(plan['source_identity']['lan_ip'])+';')
    run(['systemctl','daemon-reload'])
    # Reassert drop-ins after the archived service files have been restored.
    for name in QUIET_UNITS:
        if run(['systemctl','show',name,'-p','LoadState','--value'],check=False).stdout.strip()!='not-found':
            atomic('/etc/systemd/system/'+name+'.d/99-pbxctl-stage.conf','[Unit]\nConditionPathExists=/var/lib/pbxctl/cutover-approved\n',0o644)
    (STATE/'cutover-approved').unlink(missing_ok=True)
    run(['systemctl','daemon-reload']);run(['nginx','-t'])
    atomic(stage/'transaction.json',json.dumps({'backup':str(snapshot),'active_before':active,'source':str(directory),'phase':'staged'},indent=2))
    services=plan['source_identity'].get('active_services',active)
    need(set(services)<=set(QUIET_UNITS),'Unexpected archived service names')
    for config in [CONFIG,Path('/etc/fusionpbx-personal/site.json')]:
        if config.exists():
            data=json.loads(config.read_text());data['lan_ip']=c['lan_ip'];atomic(config,json.dumps(data,indent=2),0o644)
    atomic(STATE/'staged-restore.json',json.dumps({'transaction':str(stage),'services':services}))
    (STATE/'update-in-progress').unlink(missing_ok=True)
    return {'status':'staged','recovery_backup':str(snapshot),'previous_application':str(previous_web),'activation':'pbxctl activate --source-stopped --apply'}

def activate():
    p=STATE/'staged-restore.json';need(p.is_file(),'No staged restore')
    m=json.loads(p.read_text());atomic(STATE/'cutover-approved','approved\n',0o600)
    run(['systemctl','daemon-reload'])
    for name in m['services']:run(['systemctl','start',name])
    run(['fs_cli','-x','status']);p.unlink()
