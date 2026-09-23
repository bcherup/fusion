"""Scoped FusionPBX configuration changes, with schema and collision checks."""
import json
import os
from pathlib import Path
import secrets
import uuid
import xml.etree.ElementTree as ET
from .common import *

def uid(): return str(uuid.uuid4())

def columns(db, table):
    return {r['column_name'] for r in db.rows("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name="+literal(table))}

def require_columns(db, table, names):
    need(set(names) <= columns(db,table), 'Unsupported schema: '+table)

def domain(db,c):
    return db.one('SELECT domain_uuid FROM v_domains WHERE domain_name='+literal(c['domain']))['domain_uuid']

def profile(db,name):
    return db.one('SELECT sip_profile_uuid FROM v_sip_profiles WHERE sip_profile_name='+literal(name))['sip_profile_uuid']

def setting(db,ch,profile_id,name,value,enabled=True):
    rows=db.rows('SELECT sip_profile_setting_uuid FROM v_sip_profile_settings WHERE sip_profile_uuid='+literal(profile_id)+' AND sip_profile_setting_name='+literal(name))
    need(len(rows)<=1,'Duplicate profile setting: '+name)
    row={'sip_profile_setting_uuid':rows[0]['sip_profile_setting_uuid'] if rows else uid(),
         'sip_profile_uuid':profile_id,'sip_profile_setting_name':name,
         'sip_profile_setting_value':str(value),'sip_profile_setting_enabled':enabled}
    ch.row('v_sip_profile_settings','sip_profile_setting_uuid',row)

def domain_setting(db,ch,domain_id,category,name,value,kind='text'):
    rows=db.rows('SELECT domain_setting_uuid FROM v_domain_settings WHERE domain_uuid='+literal(domain_id)+
        ' AND domain_setting_category='+literal(category)+' AND domain_setting_subcategory='+literal(name))
    need(len(rows)<=1,'Duplicate domain setting: '+category+'/'+name)
    ch.row('v_domain_settings','domain_setting_uuid',{
        'domain_setting_uuid':rows[0]['domain_setting_uuid'] if rows else uid(),
        'domain_uuid':domain_id,'domain_setting_category':category,'domain_setting_subcategory':name,
        'domain_setting_name':kind,'domain_setting_value':str(value),'domain_setting_enabled':True})

def preflight(db,c):
    domain(db,c)
    profile(db,c['internal_profile']); profile(db,c['external_profile'])
    for table, fields in {
        'v_sip_profile_settings':['sip_profile_setting_uuid','sip_profile_uuid','sip_profile_setting_name','sip_profile_setting_value','sip_profile_setting_enabled'],
        'v_extensions':['domain_uuid','extension_uuid','extension','password','user_context'],
        'v_voicemails':['domain_uuid','voicemail_uuid','voicemail_id','voicemail_transcription_enabled'],
        'v_domain_settings':['domain_setting_uuid','domain_uuid','domain_setting_category','domain_setting_subcategory','domain_setting_name','domain_setting_value','domain_setting_enabled'],
    }.items(): require_columns(db,table,fields)
    db.one('SELECT extension_uuid FROM v_extensions WHERE domain_uuid='+literal(domain(db,c))+' AND extension='+literal(c['template_extension']))
    db.one('SELECT voicemail_uuid FROM v_voicemails WHERE domain_uuid='+literal(domain(db,c))+' AND voicemail_id='+literal(c['mailbox']))

def audio(db,c,ch):
    p=profile(db,c['internal_profile'])
    for k in ('inbound-codec-prefs','outbound-codec-prefs'): setting(db,ch,p,k,'OPUS,G722,PCMU,PCMA')
    module=Path(c['freeswitch_conf'])/'autoload_configs/modules.conf.xml'
    tree=ET.fromstring(module.read_text()); modules=tree.find('modules')
    need(modules is not None,'Unexpected modules XML')
    if not any(e.get('module')=='mod_opus' for e in modules.findall('load')):
        ET.SubElement(modules,'load',{'module':'mod_opus'})
        ch.file(module,ET.tostring(tree,encoding='unicode')+'\n')
    require_columns(db,'v_modules',['module_uuid','module_name','module_enabled'])
    rows=db.rows("SELECT module_uuid FROM v_modules WHERE module_name='mod_opus'")
    need(len(rows)==1,'Expected installed mod_opus module record; install the FreeSWITCH Opus package first')
    ch.row('v_modules','module_uuid',{'module_uuid':rows[0]['module_uuid'],'module_enabled':True})
    path=Path(c['freeswitch_conf'])/'autoload_configs/opus.conf.xml'
    tree=ET.fromstring(path.read_text()); settings=tree.find('settings')
    need(settings is not None,'Unexpected Opus XML')
    for name,value in {'complexity':'5','maxaveragebitrate':'32000','maxplaybackrate':'48000','use-vbr':'1','keep-fec-enabled':'1'}.items():
        matches=[e for e in settings.findall('param') if e.get('name')==name]
        need(len(matches)<=1,'Duplicate Opus setting')
        node=matches[0] if matches else ET.SubElement(settings,'param',{'name':name})
        node.set('value',value)
    ch.file(path,ET.tostring(tree,encoding='unicode')+'\n')

def hardening(db,c,ch):
    p=profile(db,c['internal_profile'])
    for k,v in {'auth-calls':'true','accept-blind-auth':'false','accept-blind-reg':'false',
                'inbound-reg-force-matching-username':'true','challenge-realm':'auto_to','context':'public',
                'aggressive-nat-detection':'true',
                'sip-ip':c['lan_ip'],'rtp-ip':c['lan_ip'],'ext-sip-ip':'host:'+c['nat_hostname'],
                'ext-rtp-ip':'host:'+c['nat_hostname'],'tls':'true','tls-only':'false',
                'tls-sip-port':str(c['tls_port']),'tls-version':'tlsv1.2,tlsv1.3',
                'tls-cert-dir':'/etc/freeswitch-tls/'+c['domain']+'/current',
                'tls-verify-policy':'none'}.items(): setting(db,ch,p,k,v)
    for k in ('force-register-domain','force-register-db-domain','force-subscription-domain','force-register-username'):
        rows=db.rows('SELECT sip_profile_setting_uuid FROM v_sip_profile_settings WHERE sip_profile_uuid='+literal(p)+' AND sip_profile_setting_name='+literal(k))
        for r in rows: ch.row('v_sip_profile_settings','sip_profile_setting_uuid',{**r,'sip_profile_setting_enabled':False})
    # Provider ACL is explicit and exclusive. Refuse to silently remove other entries.
    acl=db.one('SELECT access_control_uuid FROM v_access_controls WHERE access_control_name='+literal(c['provider_acl']))['access_control_uuid']
    nodes=db.rows('SELECT * FROM v_access_control_nodes WHERE access_control_uuid='+literal(acl))
    allowed=set(c['provider_cidrs'])
    need(all(r.get('node_cidr') in allowed and r.get('node_type')=='allow' and not r.get('node_domain') for r in nodes),
         'Provider ACL has unexpected entries; review it manually before hardening')
    ch.row('v_access_controls','access_control_uuid',{'access_control_uuid':acl,'access_control_default':'deny'})
    for cidr in sorted(allowed-{r['node_cidr'] for r in nodes}):
        ch.row('v_access_control_nodes','access_control_node_uuid',{'access_control_node_uuid':uid(),'access_control_uuid':acl,'node_type':'allow','node_cidr':cidr})
    # Require existing carrier wiring; never re-route the external profile blindly.
    ext=profile(db,c['external_profile'])
    configured=db.rows('SELECT sip_profile_setting_value FROM v_sip_profile_settings WHERE sip_profile_uuid='+literal(ext)+" AND sip_profile_setting_name='apply-inbound-acl' AND sip_profile_setting_enabled=true")
    need(any(r['sip_profile_setting_value']==c['provider_acl'] for r in configured),'External profile must already use the configured provider ACL')
    log=Path('/var/log/freeswitch/freeswitch.log'); need(log.is_file(),'FreeSWITCH log missing')
    ch.file('/etc/fail2ban/filter.d/pbxctl-auth.conf',r'''[Definition]
failregex = ^.*\[WARNING\]\s+sofia_reg\.c:\d+\s+SIP auth failure \(REGISTER\) on sofia profile '[^']+' for \[[^\r\n]*\] from ip <HOST>\s*$
ignoreregex =
''')
    ch.file('/etc/fail2ban/jail.d/pbxctl-auth.local','''[pbxctl-auth]
enabled = true
filter = pbxctl-auth
logpath = /var/log/freeswitch/freeswitch.log
backend = auto
maxretry = 10
findtime = 600
bantime = 3600
ignoreip = 127.0.0.1/8 ::1
action = iptables-allports[name=pbxctl-auth, protocol=all]
''')
    run(['fail2ban-client','-t'])

def transcription_settings(db,c,ch):
    d=domain(db,c)
    for name,value,kind in [('enabled','true','boolean'),('engine','pbxctl_local','text'),('api_model','base.en','text'),('save_response','false','boolean')]:
        domain_setting(db,ch,d,'transcribe',name,value,kind)
    box=db.one('SELECT voicemail_uuid FROM v_voicemails WHERE domain_uuid='+literal(d)+' AND voicemail_id='+literal(c['mailbox']))
    ch.row('v_voicemails','voicemail_uuid',{**box,'voicemail_transcription_enabled':True})
