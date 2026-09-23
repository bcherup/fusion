"""Owned ingress guards with timed rollback; preserve existing ban chains."""
import datetime
import ipaddress
import json
import os
from pathlib import Path
import shlex
from .common import BACKUPS, ROOT, atomic, digest, need, run

FAMILIES=[('iptables','iptables-restore','PBXCTL4','PBXCTLBASE4',Path('/etc/iptables/rules.v4')),('ip6tables','ip6tables-restore','PBXCTL6','PBXCTLBASE6',Path('/etc/iptables/rules.v6'))]

def rules(c,family,accept=False):
    verdict='ACCEPT' if accept else 'RETURN'
    out=[['-i','lo','-j',verdict],['-m','conntrack','--ctstate','INVALID','-j','DROP'],['-m','conntrack','--ctstate','RELATED,ESTABLISHED','-j',verdict],['-p','icmp' if family==4 else 'ipv6-icmp','-j',verdict]]
    for network in sorted(set(c['management_cidrs']+c['lan_ipv6_cidrs'])):
        if ipaddress.ip_network(network).version!=family:continue
        out+=[['-s',network,'-p','tcp','-m','multiport','--dports','22,80,443','-j',verdict]]
        for proto in ('tcp','udp'):out+=[['-s',network,'-p',proto,'-m','multiport','--dports',f"5060,{c['tls_port']},{c['trunk_port']}",'-j',verdict]]
        if family==6:out+=[['-s',network,'-p','udp','--dport',f"{c['rtp_start']}:{c['rtp_end']}",'-j',verdict]]
    if family==4:
        for network in c['provider_cidrs']:
            for proto in ('tcp','udp'):out+=[['-s',network,'-p',proto,'--dport',str(c['trunk_port']),'-j',verdict]]
            if c.get('carrier_tls',{}).get('enabled'):
                out+=[['-s',network,'-p','tcp','--dport',str(c['carrier_tls']['listen_port']),'-j',verdict]]
        out+=[['-p','tcp','--dport',str(c['tls_port']),'-j',verdict],['-p','udp','--dport',f"{c['rtp_start']}:{c['rtp_end']}",'-j',verdict]]
    return out+[['-j','DROP']]

def peers():return [' '.join(s.split()[-2:]) for s in run(['ss','-H','-tn','state','established','( sport = :22 )']).stdout.splitlines()]

def payload(c,family,chain,base=None):
    rows=['*filter',':'+chain+' - [0:0]']+['-A '+chain+' '+shlex.join(x) for x in rules(c,family)]
    if base:rows+=[':'+base+' - [0:0]']+['-A '+base+' '+shlex.join(x) for x in rules(c,family,True)]+['-A INPUT -j '+base]
    return '\n'.join(rows+['-I INPUT 1 -j '+chain,'COMMIT',''])

def preflight(c):
    need(peers(),'Keep an SSH connection open from an allowed management network')
    for peer in peers():
        addr=ipaddress.ip_address(peer.split()[-1].rsplit(':',1)[0].strip('[]'))
        need(addr.is_loopback or any(addr in ipaddress.ip_network(n) for n in c['management_cidrs']+c['lan_ipv6_cidrs']),'Current SSH peer is outside management networks')
    fresh=[]
    for tool,restore,chain,base,path in FAMILIES:
        need('nf_tables' in run([tool,'--version']).stdout,'Use Debian iptables-nft')
        need(run([tool,'-S',chain],check=False).returncode!=0,'A toolkit guard already exists; check or roll it back before changing policy')
        need(run([tool,'-S',base],check=False).returncode!=0,'Owned baseline already exists')
        rows=run([tool,'-S','INPUT']).stdout.splitlines()
        policy=[x for x in rows if x.startswith('-P INPUT ')][0].split()[-1]
        hooks=[x for x in rows if x.startswith('-A ')]
        # A missing base is supported only on a fresh host with no unrelated INPUT rules.
        baseline=run([tool,'-S','FUSIONPBX-IN' if tool=='iptables' else 'FUSIONPBX6-IN'],check=False).returncode==0
        if not baseline:
            for row in hooks:
                f=shlex.split(row);need('-j' in f and (f[-1].startswith('f2b-') or f[-1] in ('sip-auth-fail','sip-auth-ip')),'Unrecognized firewall topology; inspect before deployment')
            fresh.append(tool)
        else:need(policy=='DROP','Existing firewall must use INPUT DROP')
    return fresh

def strip_dynamic(text):
    lines=text.splitlines();names={s[1:].split()[0] for s in lines if s.startswith(':f2b-')}
    out=[]
    for s in lines:
        f=shlex.split(s) if s.startswith('-') else []
        if s.startswith(':') and s[1:].split()[0] in names:continue
        if f and (f[1] in names or any(k in f and f[f.index(k)+1] in names for k in ('-j','-g'))):continue
        out.append(s)
    return '\n'.join(out)+'\n'

def check(c):
    def normalize(tokens):
        out=[];i=0
        while i<len(tokens):
            if tokens[i]=='-m' and tokens[i+1] in ('tcp','udp','icmp','icmp6'):i+=2
            else:out.append(tokens[i]);i+=1
        return out
    for family,(tool,restore,chain,base,path) in zip((4,6),FAMILIES):
        current=run([tool,'-S',chain]).stdout.splitlines()
        need([normalize(shlex.split(x)[2:]) for x in current if x.startswith('-A ')]==[normalize(x) for x in rules(c,family)],'Firewall content differs')
        rows=[x for x in run([tool,'-S','INPUT']).stdout.splitlines() if x.startswith('-A ')]
        hook='-A INPUT -j '+chain;need(rows.count(hook)==1,'Missing/duplicate guard hook')
        for row in rows[:rows.index(hook)]:
            f=shlex.split(row);need('-j' in f,'Unexpected preceding rule');target=f[f.index('-j')+1]
            need(target.startswith('f2b-') or target in ('sip-auth-fail','sip-auth-ip'),'Unexpected preceding chain')
            for s in run([tool,'-S',target]).stdout.splitlines():
                if s.startswith('-A '):
                    t=shlex.split(s);need('-j' in t and t[t.index('-j')+1] in ('DROP','RETURN','REJECT'),'A preceding chain can bypass the guard')
        need('-P INPUT DROP' in run([tool,'-S']).stdout,'INPUT policy changed')
    return {'firewall':'verified'}

def apply(c):
    fresh=preflight(c);b=BACKUPS/(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-firewall')
    b.mkdir(parents=True,mode=0o700)
    m={'fresh':fresh,'config':c,'confirmed':False,'peers':peers(),'unit':'pbxctl-firewall-'+b.name,'families':{}}
    for family,(tool,restore,chain,base,path) in zip((4,6),FAMILIES):
        raw=run([tool+'-save']).stdout;atomic(b/(tool+'.before'),raw)
        m['families'][tool]={'policies':[s for s in run([tool,'-S']).stdout.splitlines() if s.startswith('-P ')], 'boot_existed':path.exists()}
        if path.exists():atomic(b/(tool+'.boot'),path.read_text())
        run([restore,'--test','--noflush'],data=payload(c,family,chain,base if tool in fresh else None))
    atomic(b/'firewall.json',json.dumps(m,indent=2))
    run(['systemd-run','--quiet','--unit='+m['unit'],'--on-active=10m','/usr/bin/python3',ROOT/'assets/firewall.py','--rollback',b])
    run(['systemctl','is-active',m['unit']+'.timer'])
    try:
        for family,(tool,restore,chain,base,path) in zip((4,6),FAMILIES):
            run([restore,'--noflush','--wait','5'],data=payload(c,family,chain,base if tool in fresh else None))
            run([tool,'-w','5','-P','INPUT','DROP']);run([tool,'-w','5','-P','FORWARD','DROP'])
            atomic(path,strip_dynamic(run([tool+'-save']).stdout),0o640)
        check(c);m['boot_hashes']={str(x[4]):digest(x[4]) for x in FAMILIES};atomic(b/'firewall.json',json.dumps(m,indent=2))
        run(['systemctl','enable','netfilter-persistent'])
        return {'applied':True,'transaction':str(b),'next':'Open a new SSH transport and run pbxctl firewall --confirm '+str(b),'deadline':'10 minutes; do not reboot before confirmation'}
    except BaseException:rollback(b);raise

def transaction(path):
    p=Path(path);need(not p.is_symlink() and p.resolve().parent==BACKUPS.resolve(),'Invalid firewall transaction')
    return p,json.loads((p/'firewall.json').read_text())

def rollback(path,automatic=True):
    p,m=transaction(path)
    if m.get('rolled_back') or (automatic and m.get('confirmed')):return
    if not automatic:
        check(m['config'])
        need(all(digest(Path(k))==v for k,v in m['boot_hashes'].items()),'Persistent rules changed; inspect before manual rollback')
    for tool,restore,chain,base,file in FAMILIES:
        for name in [chain,*([base] if tool in m['fresh'] else [])]:
            while run([tool,'-C','INPUT','-j',name],check=False).returncode==0:run([tool,'-w','5','-D','INPUT','-j',name])
            if run([tool,'-S',name],check=False).returncode==0:run([tool,'-F',name]);run([tool,'-X',name])
        for policy in m['families'][tool]['policies']:run([tool,*shlex.split(policy)])
        if m['families'][tool]['boot_existed']:atomic(file,(p/(tool+'.boot')).read_text(),0o640)
        elif file.exists():file.unlink()
    m['rolled_back']=True;atomic(p/'firewall.json',json.dumps(m));run(['systemctl','stop',m['unit']+'.timer'],check=False)
    return {'rolled_back':str(p)}

def confirm(path):
    p,m=transaction(path);need(not m.get('rolled_back'),'Already rolled back');check(m['config'])
    need(set(peers())-set(m['peers']),'Open a new SSH transport before confirmation')
    need(all(digest(Path(k))==v for k,v in m['boot_hashes'].items()),'Persistent rules changed')
    m['confirmed']=True;atomic(p/'firewall.json',json.dumps(m));run(['systemctl','stop',m['unit']+'.timer']);return {'confirmed':True}
