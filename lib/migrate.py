"""Transfer one newly created, identified recovery set over verified SSH."""
import json
from pathlib import Path
import re
import shlex
from .common import need, run
from . import recovery

def pull(args,c):
    need(args.ssh_host and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*',args.ssh_host),'Specify the source --ssh-host')
    need(re.fullmatch(r'[a-z_][a-z0-9_-]*',args.ssh_user),'Invalid SSH user')
    need(1<=args.ssh_port<=65535,'Invalid SSH port')
    ssh=['ssh','-p',str(args.ssh_port),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes']
    target=args.ssh_user+'@'+args.ssh_host
    command=([] if args.ssh_user=='root' else ['sudo','-n'])+['/usr/local/sbin/pbxctl','backup','--apply']
    response=json.loads(run([*ssh,target,shlex.join(command)],timeout=3600).stdout)
    source=response['backup']
    need(re.fullmatch(r'/var/backups/pbxctl/[0-9TZ-]+[0-9a-f]{8}',source),'Unexpected source recovery directory')
    dest=recovery.ARCHIVES/('import-'+Path(source).name)
    need(not dest.exists(),'This recovery set has already been imported')
    dest.mkdir(parents=True,mode=0o700)
    rsync=['rsync','-a','-e',shlex.join(ssh)]
    if args.ssh_user!='root':rsync+=['--rsync-path=sudo -n rsync']
    for name in ('manifest.json','database.dump','files.tar.gz'):
        run([*rsync,'--',target+':'+source+'/'+name,str(dest/name)],timeout=7200)
    recovery.verify(dest,c);recovery.scratch_restore(dest,c)
    return {'imported':str(dest),'scratch_restore':'passed','next':'Review restore plan, then restore --archive '+str(dest)+' --apply',
            'cutover':'Source is still active; perform a final quiesced backup before retiring it.'}
