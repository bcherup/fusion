"""Validated public configuration and private credential input."""
import getpass
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
from .common import CONFIG, STATE, Error, atomic, hostname, need, validate

MODULES=('backup','tls','hardening','audio','transcription','smtp','alerts','offsite')
FIELDS={
 'backup':('backup_min_free_gib',),
 'tls':('domain','lan_ip','tls_port','service_user','service_group'),
 'hardening':('domain','lan_ip','nat_hostname','tls_port','provider_cidrs','provider_acl','internal_profile','external_profile'),
 'audio':('internal_profile','freeswitch_conf'),
 'transcription':('domain','mailbox','transcription_enabled','whisper_port','whisper_cpu_percent','whisper_memory_mb','service_user','service_group'),
 'smtp':('domain','mailbox','smtp'), 'alerts':('smtp',), 'offsite':('remote_backup',),
}

def email(s,empty=False):
    need(isinstance(s,str) and ((empty and not s) or re.fullmatch(r'[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+',s)), 'Invalid email address')
    return s

def private_path(s):
    p=PurePosixPath(s)
    need(str(p).startswith('/etc/pbxctl/secrets/') and '..' not in p.parts and re.fullmatch(r'/[A-Za-z0-9_./-]+',str(p)), 'Credential file must be inside /etc/pbxctl/secrets')
    return Path(s)

def validate_all(c):
    c=validate(c)
    need(type(c.get('transcription_enabled')) is bool,'transcription_enabled must be boolean')
    s=c['smtp']; r=c['remote_backup']
    need(set(s)=={'host','port','security','auth','username','from_address','from_name','recipient','password_file'},'Unexpected SMTP fields')
    if s['host']:
        try: ipaddress.ip_address(s['host'])
        except ValueError: hostname(s['host'])
    need(type(s['port']) is int and 1<=s['port']<=65535,'Invalid SMTP port')
    need(s['security'] in ('starttls','tls','none'),'SMTP security: starttls, tls, or none')
    need(type(s['auth']) is bool,'SMTP auth must be boolean')
    need(not s['auth'] or s['security']!='none','Password authentication requires TLS')
    for k in ('username','from_name'):
        need(isinstance(s[k],str) and len(s[k])<=254 and not any(ord(x)<32 for x in s[k]),'Invalid SMTP '+k)
    for k in ('from_address','recipient'): email(s[k],True)
    private_path(s['password_file']); private_path(r['password_file']); private_path(r['credentials_file'])
    need(set(r)=={'enabled','repository','password_file','credentials_file','keep_daily','keep_weekly','keep_monthly'},'Unexpected remote backup fields')
    need(type(r['enabled']) is bool,'Remote backup enabled must be boolean')
    if r['repository']:
        need(re.fullmatch(r'(sftp:[A-Za-z0-9_.@:/-]+|s3:https://[A-Za-z0-9_.:/-]+)',r['repository']), 'Use an SFTP or HTTPS S3 repository URL without inline credentials')
    need(not r['enabled'] or bool(r['repository']),'Choose a remote backup repository')
    for k in ('keep_daily','keep_weekly','keep_monthly'):
        need(type(r[k]) is int and 0<=r[k]<=3650,'Invalid retention count')
    return c

def load_config(path=CONFIG): return validate_all(json.loads(Path(path).read_text()))

def read_secret(path):
    p=private_path(path)
    need(p.is_file() and not p.is_symlink(),'Credential file missing or linked')
    for parent in p.parents:
        need(not parent.is_symlink(),'Credential parent is linked')
    st=p.stat()
    need(st.st_uid==0 and not st.st_mode&0o077,'Credential file must be root-owned mode 600')
    value=p.read_text().strip()
    need(value and '\n' not in value and '\r' not in value,'Invalid credential file')
    return value

def prompt_secret(path,label):
    p=private_path(path)
    value=getpass.getpass(label+': ')
    need(value and '\n' not in value and '\r' not in value,'Empty/invalid credential')
    p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    need(not p.parent.is_symlink(),'Credential parent is linked')
    p.parent.chmod(0o700); atomic(p,value+'\n',0o600)

def selected(text):
    result=text.split(',') if text else []
    need(result and len(result)==len(set(result)) and set(result)<=set(MODULES),'Select comma-separated modules: '+','.join(MODULES))
    return [m for m in MODULES if m in result]

def module_config(c,m):
    keys=(*FIELDS[m],'web_root','database')
    return {k:c[k] for k in keys}

def wizard(path,template):
    c=load_config(path if Path(path).exists() else template)
    def ask(label,current): return input(label+' ['+str(current)+']: ').strip() or current
    print('Site configuration. Enter retains the displayed value; credentials are entered separately.')
    for k,label in [('domain','SIP domain'),('nat_hostname','Public NAT hostname'),('lan_ip','PBX LAN IPv4'),('acme_email','Certificate email'),('mailbox','Mailbox for optional transcription/email')]: c[k]=ask(label,c[k])
    c['management_cidrs']=[x.strip() for x in ask('Management networks (comma separated)',','.join(c['management_cidrs'])).split(',') if x.strip()]
    c['provider_cidrs']=[x.strip() for x in ask('Carrier IPv4 /32 addresses (empty allowed)',','.join(c['provider_cidrs'])).split(',') if x.strip()]
    if ask('Configure SMTP? y/n','n').lower()=='y':
        s=c['smtp']
        for k,label in [('host','SMTP hostname'),('security','Security: starttls/tls/none'),('username','SMTP username'),('from_address','Sender address'),('from_name','Sender name'),('recipient','Voicemail/alert recipient')]: s[k]=ask(label,s[k])
        s['port']=int(ask('SMTP port',s['port']));s['auth']=ask('SMTP authentication? y/n','y' if s['auth'] else 'n').lower()=='y'
    if ask('Configure off-server backups now? y/n','n').lower()=='y':
        r=c['remote_backup'];r['enabled']=True;r['repository']=ask('Repository: sftp:user@host:/path or s3:https://host/bucket',r['repository'])
        for k in ('keep_daily','keep_weekly','keep_monthly'):r[k]=int(ask(k+' (0 disables this retention rule)',r[k]))
    validate_all(c);atomic(path,json.dumps(c,indent=2)+'\n',0o644)
    print('Saved '+str(path)+'. Use plan before applying selected modules.')
