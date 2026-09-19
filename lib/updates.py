"""Explicit fast-forward application updates and verified toolkit replacement."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
from .common import ROOT, STATE, atomic, digest, idle, need, run
from . import recovery

def git(path,*args):return run(['git','-c','safe.directory='+str(path),'-C',path,*args]).stdout.strip()

def repositories(c):
    web=Path(c['web_root']);result=[web]
    if (web/'app').exists():result += [p for p in sorted((web/'app').iterdir()) if not p.is_symlink() and (p/'.git').exists()]
    return result

def plan(c,fetch=False):
    result=[]
    for p in repositories(c):
        dirty=git(p,'status','--porcelain','--untracked-files=no')
        need(not dirty,'Tracked local changes in '+str(p)+'; preserve and review them before updating')
        branch=git(p,'symbolic-ref','--quiet','--short','HEAD')
        remote=git(p,'config','branch.'+branch+'.remote');ref=git(p,'config','branch.'+branch+'.merge')
        need(remote and remote!='.' and ref.startswith('refs/heads/'),'An explicit remote tracking branch is required')
        url=git(p,'remote','get-url',remote)
        need(url.startswith('https://github.com/fusionpbx/'),'Only official application remotes are supported')
        if fetch:git(p,'fetch','--no-tags',remote,ref)
        target=git(p,'rev-parse','FETCH_HEAD' if fetch else '@{upstream}')
        head=git(p,'rev-parse','HEAD')
        r=run(['git','-c','safe.directory='+str(p),'-C',p,'merge-base','--is-ancestor',head,target],check=False)
        need(r.returncode==0,'Local commits or diverged history in '+str(p)+'; automatic update refused')
        result.append({'path':str(p),'branch':branch,'before':head,'target':target,'changes':head!=target})
    return result

def apply(c):
    need(not (STATE/'update-in-progress').exists(),'An earlier update needs recovery; inspect its private transaction record')
    planned=plan(c,True);need(any(r['changes'] for r in planned),'Already at the fetched upstream revisions')
    idle();snapshot=recovery.backup(c);idle();active=recovery.stop_services()
    journal=STATE/('update-'+uuid.uuid4().hex+'.json')
    record={'backup':str(snapshot),'repositories':planned,'active_before':active,'phase':'source'}
    atomic(journal,json.dumps(record,indent=2))
    for name in recovery.QUIET_UNITS:
        atomic('/etc/systemd/system/'+name+'.d/98-pbxctl-update.conf','[Unit]\nConditionPathExists=!/var/lib/pbxctl/update-in-progress\n',0o644)
    atomic(STATE/'update-in-progress',str(journal),0o600)
    run(['systemctl','daemon-reload'])
    try:
        for row in planned:
            p=Path(row['path'])
            need(git(p,'rev-parse','HEAD')==row['before'],'Repository changed during update')
            need(not git(p,'status','--porcelain','--untracked-files=no'),'Files changed during update')
            git(p,'merge','--ff-only',row['target'])
        record['phase']='schema';atomic(journal,json.dumps(record,indent=2))
        upgrade=Path(c['web_root'])/'core/upgrade/upgrade.php'
        import re
        for action in ('--schema','--defaults'):
            result=run(['php',upgrade,action],timeout=600)
            need(not re.search(r'PHP (?:Fatal|Parse) error|SQLSTATE\[|ERROR:',result.stdout+result.stderr,re.I),'Application upgrade reported an error; services remain stopped')
        # Set ownership only on application-managed trees; do not traverse custom integration links.
        import pwd,grp
        owner=pwd.getpwnam(c['service_user']).pw_uid;group=grp.getgrnam(c['service_group']).gr_gid
        for top,dirs,files in os.walk(c['web_root'],followlinks=False):
            for name in [top,*[str(Path(top)/x) for x in files]]:
                if not Path(name).is_symlink():os.chown(name,owner,group)
        run(['python3',ROOT/'assets/upgrade-check.py','--web-root',c['web_root']])
        (STATE/'update-in-progress').unlink()
        for name in active:run(['systemctl','start',name])
        run(['fs_cli','-x','reloadxml'])
        record['phase']='complete';atomic(journal,json.dumps(record,indent=2))
        return {'status':'updated','backup':str(snapshot),'follow_up':'Test registration, inbound/outbound calls, and a new voicemail.'}
    except BaseException:
        atomic(STATE/'update-in-progress',str(journal),0o600)
        record['phase']='failed';atomic(journal,json.dumps(record,indent=2))
        # A schema downgrade cannot safely be achieved by reverting Git alone.
        recovery.stop_services()
        raise

def validate_release(source):
    source=Path(source).resolve();m=json.loads((source/'MANIFEST.json').read_text())
    need(isinstance(m.get('sha256'),dict),'Missing release checksums')
    need((source/'VERSION').is_file() and (source/'pbxctl.py').is_file(),'Not a toolkit release')
    for relative,h in m['sha256'].items():
        p=Path(relative)
        need(not p.is_absolute() and '..' not in p.parts and p.parts,'Unsafe release path')
        f=source/p
        need(not any(x.is_symlink() for x in [f,*f.parents] if x!=source and source in x.parents),'Linked release path')
        need(f.is_file() and not f.is_symlink() and digest(f)==h,'Release checksum mismatch')
    need('pbxctl.py' in m['sha256'] and 'VERSION' in m['sha256'],'Incomplete release manifest')
    return m

def tool(source,apply=False):
    source=Path(source).resolve();m=validate_release(source)
    need(not ROOT.is_symlink() and not any(p.is_symlink() for p in ROOT.parents),'Linked installation path')
    if ROOT.exists():validate_release(ROOT)
    summary={'current':(ROOT/'VERSION').read_text().strip() if (ROOT/'VERSION').exists() else None,
             'candidate':(source/'VERSION').read_text().strip(),'files':len(m['sha256'])}
    if not apply:return summary
    need(source!=ROOT.resolve() and ROOT.resolve() not in source.parents,'Select a separately downloaded release directory')
    ROOT.parent.mkdir(parents=True,exist_ok=True)
    backup=ROOT.parent/('.pbxctl-before-'+uuid.uuid4().hex)
    staged=Path(tempfile.mkdtemp(prefix='.pbxctl-release-',dir=ROOT.parent))
    previous_mode=ROOT.stat().st_mode&0o777 if ROOT.exists() else None
    try:
        for relative in m['sha256']:
            atomic(staged/relative,(source/relative).read_bytes(),0o755 if relative=='pbxctl.py' or relative.endswith('.sh') else 0o644)
        atomic(staged/'MANIFEST.json',(source/'MANIFEST.json').read_bytes(),0o644)
        for top,dirs,files in os.walk(staged):os.chmod(top,0o755)
        validate_release(staged)
        if ROOT.exists():ROOT.rename(backup)
        try:staged.rename(ROOT)
        except BaseException:
            if backup.exists():backup.rename(ROOT)
            raise
        if backup.exists():backup.chmod(0o700)
    except BaseException:
        if backup.exists() and not ROOT.exists():
            backup.chmod(previous_mode);backup.rename(ROOT)
        raise
    finally:
        if staged.exists():shutil.rmtree(staged)
    return {**summary,'previous_toolkit':str(backup) if backup.exists() else None,'status':'installed'}
