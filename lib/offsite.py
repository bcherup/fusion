"""Optional encrypted off-server recovery copies and explicit retention."""
import json
import os
from pathlib import Path
import subprocess
from .common import need, run
from .config import private_path, read_secret
from . import recovery

def environment(c):
    r=c['remote_backup'];need(r['enabled'] and r['repository'],'Remote backups are disabled')
    read_secret(r['password_file'])
    env={**os.environ,'RESTIC_REPOSITORY':r['repository'],'RESTIC_PASSWORD_FILE':r['password_file']}
    p=private_path(r['credentials_file'])
    if p.exists():
        values=json.loads(read_secret(p))
        need(set(values)<= {'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_DEFAULT_REGION'},'Unsupported storage credential fields')
        need(all(isinstance(v,str) and '\n' not in v for v in values.values()),'Invalid storage credentials')
        env.update(values)
    return env

def execute(c,args):
    p=subprocess.run(['restic',*args],env=environment(c),capture_output=True,text=True,timeout=7200)
    need(p.returncode==0,'Remote backup command failed; private provider output withheld')
    return p.stdout

def upload(c,directory):
    recovery.verify(directory,c)
    execute(c,['backup','--tag','pbxctl','--',str(Path(directory).resolve())])
    execute(c,['check'])
    return {'uploaded':str(directory),'repository_check':'passed','restore_test':'Run offsite-restore into a new directory before relying on recovery'}

def retention(c,apply=False):
    r=c['remote_backup'];args=['forget','--tag','pbxctl','--group-by','host,tags']
    for key in ('keep_daily','keep_weekly','keep_monthly'):
        if r[key]:args+=['--'+key.replace('_','-'),str(r[key])]
    need(len(args)>5,'No retention policy selected')
    args+=['--prune'] if apply else ['--dry-run']
    execute(c,args)
