"""Fresh Debian 13 base installation using a pinned upstream installer."""
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from .common import STATE, atomic, need, run

PIN='eddc685f83fbe9bdf8eb050c80e50d8f51e505e1'
URL='https://github.com/fusionpbx/fusionpbx-install.sh.git'

def supported():
    need(platform.system()=='Linux' and os.geteuid()==0,'Run this action as root on Debian 13 Trixie')
    release=Path('/etc/os-release').read_text()
    need(re.search(r'^ID=debian$',release,re.M) and re.search(r'^VERSION_ID="?13"?$',release,re.M), 'Only Debian 13 Trixie is supported')

def install(c):
    supported();need(not Path(c['web_root']).exists() and not Path('/etc/fusionpbx').exists(),'Existing PBX detected; use configure')
    need(c['web_root']=='/var/www/fusionpbx' and c['database']=='fusionpbx','Fresh install uses the upstream standard layout')
    need(not Path('/var/lib/postgresql').exists(),'Use a fresh server without an existing PostgreSQL cluster')
    os.umask(0o077);STATE.mkdir(mode=0o755,parents=True,exist_ok=True)
    run(['apt-get','update'],timeout=600);run(['apt-get','install','-y','git','ca-certificates'],timeout=600)
    source=STATE/'base-installer'
    need(not source.exists(),'An earlier base install is present; inspect its private log before retrying')
    run(['git','clone',URL,source],timeout=600);run(['git','-C',source,'checkout','--detach',PIN])
    need(run(['git','-C',source,'rev-parse','HEAD']).stdout.strip()==PIN,'Base installer pin differs')
    p=source/'debian/resources/config.sh';s=p.read_text()
    need(re.search(r'^domain_name=',s,re.M),'Upstream configuration changed')
    s=re.sub(r'^domain_name=.*$',"domain_name='"+c['domain']+"'",s,flags=re.M)
    atomic(p,s,0o600)
    log=STATE/'base-install.log'
    # The upstream installer prints initial credentials; capture them only in this root-only log.
    with log.open('x') as f:
        os.chmod(log,0o600)
        r=subprocess.run(['bash',source/'debian/install.sh'],stdout=f,stderr=subprocess.STDOUT,timeout=14400)
    need(r.returncode==0,'Base installation failed; inspect /var/lib/pbxctl/base-install.log locally')
    for name in ('postgresql','nginx','freeswitch'):run(['systemctl','is-active',name])
    need(Path(c['web_root']+'/resources/require.php').is_file(),'Base application missing')
    # Upstream enables SNMP with a community string; this toolkit does not need it.
    run(['systemctl','disable','--now','snmpd'],check=False)
    return {'status':'base installed','credentials':'/var/lib/pbxctl/base-install.log','next':'Configure carrier/mailbox in the PBX, then apply selected toolkit modules. Apply the firewall before exposure.'}
