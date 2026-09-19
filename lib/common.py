"""Shared safety, configuration, database, and rollback helpers."""
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path('/opt/pbxctl')
CONFIG = Path('/etc/pbxctl/site.json')
STATE = Path('/var/lib/pbxctl')
BACKUPS = Path('/root/pbxctl-backups')

class Error(RuntimeError):
    pass

def need(condition, message):
    if not condition:
        raise Error(message)

def run(args, data=None, timeout=60, check=True):
    p = subprocess.run([str(x) for x in args], input=data, capture_output=True,
                       text=True, timeout=timeout)
    need(not check or p.returncode == 0, str(args[0]) + ' failed; sensitive output withheld')
    return p

def hostname(value):
    need(isinstance(value, str) and len(value) <= 253 and '.' in value,
         'Use a complete DNS hostname')
    need(all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', x)
             for x in value.split('.')), 'Invalid DNS hostname')
    return value.lower()

def validate(c):
    c = dict(c)
    c['domain'] = hostname(c['domain'])
    c['nat_hostname'] = hostname(c['nat_hostname'])
    need(ipaddress.ip_address(c['lan_ip']).version == 4, 'lan_ip must be IPv4')
    for field in ('management_cidrs', 'lan_ipv6_cidrs', 'provider_cidrs'):
        need(isinstance(c[field], list), field + ' must be a list')
        c[field] = [str(ipaddress.ip_network(x, strict=True)) for x in c[field]]
    need(c['management_cidrs'], 'At least one trusted management network is required')
    need(all(ipaddress.ip_network(x).prefixlen > 0 for x in c['management_cidrs']),
         'Public management /0 is prohibited')
    need(all(ipaddress.ip_network(x).version == 6 for x in c['lan_ipv6_cidrs']), 'IPv6 LAN CIDRs required')
    need(all(ipaddress.ip_network(x).version == 4 and
         ipaddress.ip_network(x).prefixlen == 32 for x in c['provider_cidrs']),
         'Verify individual carrier IPv4 /32 addresses; broad carrier ranges are refused')
    for k in ('database', 'service_user', 'service_group', 'provider_acl', 'internal_profile', 'external_profile'):
        need(re.fullmatch(r'[A-Za-z0-9_-]+', c[k]) is not None, 'Invalid identifier: ' + k)
    for k in ('tls_port', 'trunk_port', 'rtp_start', 'rtp_end', 'whisper_port'):
        need(type(c[k]) is int and 1024 <= c[k] <= 65535, 'Invalid port: ' + k)
    need(c['rtp_start'] < c['rtp_end'], 'RTP range must increase')
    need(c['tls_port'] != c['trunk_port'] and c['whisper_port'] not in
         (c['tls_port'], c['trunk_port']), 'Service ports must differ')
    need(type(c['whisper_cpu_percent']) is int and 10 <= c['whisper_cpu_percent'] <= 100,
         'Recognition CPU limit must be 10â€“100% of one core')
    need(type(c['whisper_memory_mb']) is int and 512 <= c['whisper_memory_mb'] <= 1600,
         'Recognition memory limit must be 512â€“1600 MiB')
    need(type(c['backup_min_free_gib']) is int and c['backup_min_free_gib'] >= 5,
         'Keep at least 5 GiB free for backups')
    for k in ('web_root', 'freeswitch_conf', 'freeswitch_scripts', 'cloudflare_credentials_file'):
        p = PurePosixPath(c[k])
        need(p.is_absolute() and '..' not in p.parts and str(p) != '/' and
             re.fullmatch(r'/[A-Za-z0-9_./-]+', str(p)), 'Unsafe path: ' + k)
    numbers = [c['mailbox'], c['template_extension'], c['ring_group'], *c['devices']]
    need(all(isinstance(x, str) and re.fullmatch(r'[0-9]{2,8}', x) for x in numbers),
         'Extension numbers must be 2â€“8 digits')
    need(c['ring_group'] not in c['devices'] and c['template_extension'] not in c['devices'],
         'Device, template, and group numbers must be distinct')
    need(c['ring_group'] != c['template_extension'], 'Group cannot equal template extension')
    need(all(isinstance(x, str) and 1 <= len(x) <= 80 and '\n' not in x
             for x in [c['ring_group_name'], *c['devices'].values()]), 'Invalid device/group label')
    need(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', c['acme_email']), 'ACME email is required')
    # These files intentionally never hold SIP, database, SMTP, or Cloudflare secrets.
    need(not any(re.search(r'password|token|secret', k, re.I) for k in c),
         'Do not put credentials in site.json')
    return c

def load(path=CONFIG):
    return validate(json.loads(Path(path).read_text()))

def literal(v):
    if v is None: return 'NULL'
    if v is True: return 'true'
    if v is False: return 'false'
    return "'" + str(v).replace("'", "''") + "'"

def identifier(v):
    need(re.fullmatch(r'[a-z][a-z0-9_]*', v) is not None, 'Unsafe database identifier')
    return v

class Database:
    def __init__(self, name): self.name = name
    def execute(self, text):
        # stdin, not argv: SQL can contain freshly generated credentials.
        return run(['runuser', '-u', 'postgres', '--', 'psql', '-X', '-qAt',
                    '-v', 'ON_ERROR_STOP=1', '-d', self.name], data='SET standard_conforming_strings=on;\n'+text).stdout
    def rows(self, query):
        return json.loads(self.execute('BEGIN READ ONLY; SELECT coalesce(json_agg(x),\'[]\'::json) FROM (' + query + ') x; COMMIT;'))
    def one(self, query):
        rows = self.rows(query)
        need(len(rows) == 1, 'Expected exactly one matching database object')
        return rows[0]
    def dump(self, path):
        with Path(path).open('xb') as out:
            p = subprocess.run(['runuser', '-u', 'postgres', '--', 'pg_dump', '-Fc', self.name],
                               stdout=out, stderr=subprocess.PIPE)
        need(p.returncode == 0, 'Database backup failed')
        run(['pg_restore', '--list', path])

def atomic(path, data, mode=0o600, owner=None):
    path = Path(path)
    need(not path.is_symlink(), 'Refusing to overwrite a symlink: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.pbxctl-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data.encode() if isinstance(data, str) else data)
            out.flush(); os.fsync(out.fileno())
        os.chmod(temporary, mode)
        if owner: os.chown(temporary, *owner)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def digest(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()

class Change:
    """Each component gets its own database snapshot and scoped file/row undo."""
    def __init__(self, db, component):
        os.umask(0o077)
        BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = BACKUPS / (datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + component + '-' + uuid.uuid4().hex[:8])
        self.path.mkdir(mode=0o700)
        self.db = db
        self.actions = []
        db.dump(self.path / 'database.dump')
        self.save()
    def save(self):
        atomic(self.path / 'undo.json', json.dumps(self.actions, indent=2))
    def file(self, path, content, mode=0o644, owner=None):
        path = Path(path)
        old = None
        if path.exists():
            need(path.is_file() and not path.is_symlink(), 'Expected regular file: ' + str(path))
            old = str(len(self.actions)) + '.before'
            shutil.copy2(path, self.path / old)
            st = path.stat()
            old = {'file': old, 'mode': st.st_mode & 0o777, 'uid': st.st_uid, 'gid': st.st_gid}
        encoded=content.encode() if isinstance(content,str) else content
        self.actions.append({'kind': 'file', 'path': str(path), 'before': old,
                             'after_sha256':hashlib.sha256(encoded).hexdigest()})
        self.save()
        atomic(path, content, mode, owner)
    def symlink(self,path,target):
        path=Path(path)
        need(not path.exists() and not path.is_symlink(),'Link path occupied')
        self.actions.append({'kind':'symlink','path':str(path),'target':str(target)})
        self.save(); path.symlink_to(target)
    def directory(self,path,mode,owner):
        path=Path(path)
        need(not path.is_symlink(),'Directory cannot be a symlink')
        old=None
        if path.exists():
            need(path.is_dir(),'Expected directory')
            s=path.stat(); old={'mode':s.st_mode&0o777,'uid':s.st_uid,'gid':s.st_gid}
        self.actions.append({'kind':'directory','path':str(path),'before':old})
        self.save(); path.mkdir(mode=mode,parents=True,exist_ok=True)
        path.chmod(mode); os.chown(path,*owner)
    def row(self, table, key, row):
        identifier(table); identifier(key)
        for name in row: identifier(name)
        need(key in row, 'Row key missing')
        before = self.db.rows('SELECT * FROM ' + table + ' WHERE ' + key + '=' + literal(row[key]))
        need(len(before) <= 1, 'Duplicate database key')
        self.actions.append({'kind': 'row', 'table': table, 'key': key, 'id': row[key],
                             'before': before[0] if before else None,'after_fields':row})
        self.save()
        if before:
            sets = ','.join(k+'='+literal(v) for k,v in row.items() if k != key)
            self.db.execute('UPDATE '+table+' SET '+sets+' WHERE '+key+'='+literal(row[key])+';')
        else:
            self.db.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES ('+','.join(literal(v) for v in row.values())+');')
        after=self.db.rows('SELECT * FROM '+table+' WHERE '+key+'='+literal(row[key]))
        need(len(after)==1,'Database write did not produce exactly one row')
        self.actions[-1]['after_full']=after[0]
        self.save()
    def rollback(self):
        rollback(self.path, self.db)

def rollback(directory, db):
    directory = Path(directory).resolve()
    need(directory.parent == BACKUPS.resolve(), 'Rollback must use a component backup directory')
    actions = json.loads((directory/'undo.json').read_text())
    # Refuse to overwrite later edits. Rollback is for the immediate component,
    # not a general database restore or deletion of data created since installation.
    for a in actions:
        p=Path(a.get('path','/'))
        if a['kind']=='file' and p.exists():
            before=a['before']
            old_hash=digest(directory/before['file']) if before else None
            need(not p.is_symlink() and digest(p) in (old_hash,a['after_sha256']), 'File changed after installation: '+str(p))
        elif a['kind']=='symlink' and (p.exists() or p.is_symlink()):
            need(p.is_symlink() and os.readlink(p)==a['target'],'Integration link has changed')
        elif a['kind']=='row':
            rows=db.rows('SELECT * FROM '+identifier(a['table'])+' WHERE '+identifier(a['key'])+'='+literal(a['id']))
            if rows:
                current=rows[0]
                expected=a['after_fields']
                def equal(x,y): return str(x).lower()==str(y).lower() if x is not None and y is not None else x is y
                applied=all(equal(current.get(k),v) for k,v in expected.items())
                if a.get('after_full') is not None: applied=current==a['after_full']
                original=a['before'] is not None and current==a['before']
                need(applied or original,'Database object changed after installation; manual rollback required')
            else:
                need(a['before'] is None,'Existing database object was deleted after installation; manual recovery required')
    sql = ['BEGIN;']
    for a in reversed(actions):
        if a['kind'] != 'row': continue
        table, key = identifier(a['table']), identifier(a['key'])
        if a['before'] is None:
            sql.append('DELETE FROM '+table+' WHERE '+key+'='+literal(a['id'])+';')
        else:
            row = a['before']; cols = [identifier(x) for x in row if x != key]
            sql.append('UPDATE '+table+' t SET '+','.join(x+'=r.'+x for x in cols)+
                       ' FROM jsonb_populate_record(NULL::'+table+','+literal(json.dumps(row))+'::jsonb) r WHERE t.'+key+'='+literal(a['id'])+';')
    db.execute('\n'.join(sql + ['COMMIT;']))
    for a in reversed(actions):
        if a['kind']=='row': continue
        p = Path(a['path'])
        if a['kind']=='symlink':
            if p.is_symlink(): p.unlink()
            continue
        if a['kind']=='directory':
            if a['before']:
                old=a['before']; p.chmod(old['mode']); os.chown(p,old['uid'],old['gid'])
            # Keep newly created directories: later cache/media may live there.
            continue
        if a['kind'] != 'file': continue
        if a['before'] is None:
            if p.exists():
                need(p.is_file() and not p.is_symlink(), 'Unexpected rollback target')
                p.unlink()
        else:
            old=a['before']; atomic(p,(directory/old['file']).read_bytes(),old['mode'],(old['uid'],old['gid']))

def idle():
    need(re.search(r'\b0 total\.', run(['fs_cli','-x','show calls count']).stdout),
         'Active calls; restart/configuration change deferred')

def invalidate(config, keys):
    fd,path = tempfile.mkstemp(prefix='pbxctl-', suffix='.lua', dir='/run')
    try:
        with os.fdopen(fd,'w') as f:
            f.write('local c=require "resources.functions.cache"; '+''.join('c.del('+json.dumps(k)+');' for k in keys)+'stream:write("cache invalidated")')
        os.chmod(path,0o644)
        need('cache invalidated' in run(['fs_cli','-x','lua '+path]).stdout, 'Cache invalidation failed')
        need('+OK' in run(['fs_cli','-x','reloadxml']).stdout, 'XML reload failed')
    finally: os.unlink(path)
