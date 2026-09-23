"""Discover and adjust existing audio without creating a full site configuration."""
import copy
import hashlib
import json
import math
import re
from pathlib import Path
from . import features, operations
from .common import CONFIG, STATE, Database, Change, Error, atomic, digest, hostname, invalidate, literal, need
from .config import module_config, load_config
from .diagnostics import bounded_json

HELPER = Path('/usr/local/lib/pbxctl/call-volume.lua')
SOURCE = Path(__file__).resolve().parents[1]


class Volume:
    def __init__(self, domain=None, database='fusionpbx', db=None):
        need(re.fullmatch(r'[A-Za-z0-9_-]+', database), 'Invalid database name')
        self.database=database;self.db=db or Database(database)
        domains=self.db.rows('SELECT domain_uuid,domain_name FROM v_domains ORDER BY domain_name LIMIT 101')
        if domain:domain=hostname(domain)
        elif len(domains)==1:domain=hostname(domains[0]['domain_name'])
        matches=[d for d in domains if d['domain_name']==domain]
        need(len(matches)==1,'Choose one existing SIP domain before adjusting volume')
        self.domain=domain;self.domain_id=matches[0]['domain_uuid']
        self.active=load_config(CONFIG) if CONFIG.exists() else None
        self.same_site=bool(self.active and self.active['domain']==domain and self.active['database']==database)
        self.c={'domain':domain,'database':database,'web_root':self.active['web_root'] if self.same_site else '/var/www/fusionpbx',
                'internal_profile':self.active['internal_profile'] if self.same_site else 'internal'}

    def marker(self, name):
        path=STATE/(name+'.json')
        if not path.exists():return None
        record=bounded_json(path)
        need(record.get('desired',{}).get('database')==self.database,'This audio record belongs to a different database; review it in Advanced')
        if name=='call-volume':need(record['desired'].get('domain')==self.domain,'Phone volume is managed for another domain; review it in Advanced')
        return operations.check_managed(path,self.db)

    def music(self):
        rows=self.db.rows('SELECT domain_uuid,music_on_hold_name,music_on_hold_path,music_on_hold_rate FROM v_music_on_hold WHERE domain_uuid='+literal(self.domain_id)+' OR domain_uuid IS NULL ORDER BY music_on_hold_name,music_on_hold_rate LIMIT 401')
        need(len(rows)<=400,'Too many music streams for simple controls; use Advanced')
        groups={}
        for row in rows:
            name=row['music_on_hold_name'] or '';rate=str(row['music_on_hold_rate'])
            stream=(self.domain+'/' if row['domain_uuid'] else '')+name
            if not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*',stream) or rate not in ('8000','16000','32000','48000'):continue
            raw=(row['music_on_hold_path'] or '').replace('$${sounds_dir}',str(features.MUSIC_ROOT.parent))
            path=Path(raw).resolve();root=features.MUSIC_ROOT.resolve()
            if path.name!=rate or root not in path.parents or not path.is_dir():continue
            groups.setdefault(stream,[]).append(path)
        selections=self.db.rows("SELECT hold_music AS selection FROM v_extensions WHERE domain_uuid="+literal(self.domain_id)+" UNION SELECT ring_group_ringback AS selection FROM v_ring_groups WHERE domain_uuid="+literal(self.domain_id))
        used={str(r['selection']).removeprefix('local_stream://') for r in selections if r['selection']}
        result=[]
        for stream,paths in groups.items():
            directories={p.parent for p in paths}
            if len(directories)!=1:continue
            directory=directories.pop()
            if directory==features.MUSIC_ROOT.resolve():continue
            files=sorted(directory.rglob('*.wav'))
            if not files or len(files)>200:continue
            if not all(not p.is_symlink() and p.resolve().parent in paths for p in files):continue
            if sum(p.stat().st_size for p in files)>256*1024**2:continue
            result.append({'stream':stream,'directory':str(directory),'in_use':stream in used,'files':len(files),
                           'fingerprint':hashlib.sha256(json.dumps([(str(p),digest(p)) for p in files]).encode()).hexdigest()})
        return sorted(result,key=lambda x:(not x['in_use'],x['stream']))

    def phones(self):
        rows=self.db.rows('SELECT extension FROM v_extensions WHERE domain_uuid='+literal(self.domain_id)+' AND enabled=true ORDER BY extension LIMIT 101')
        phones=[r['extension'] for r in rows if re.fullmatch(r'[0-9]{2,8}',r['extension'] or '')]
        need(phones and len(phones)<=100,'No supported enabled phone extensions were found')
        groups=self.db.rows('SELECT ring_group_extension FROM v_ring_groups WHERE domain_uuid='+literal(self.domain_id)+' ORDER BY ring_group_extension LIMIT 101')
        destinations=sorted(set(phones+[r['ring_group_extension'] for r in groups if re.fullmatch(r'[0-9]{2,8}',r['ring_group_extension'] or '')]))
        need(len(destinations)<=100,'Too many phone destinations for simple controls')
        return phones,destinations

    def current(self, name, stream=None):
        need(name in ('hold-music','call-volume'),'Choose music or phone volume')
        record=self.marker(name)
        if name=='hold-music':
            choices=self.music()
            if not stream:
                active=[x for x in choices if x['in_use']]
                if len(active)==1:stream=active[0]['stream']
                elif len(choices)==1:stream=choices[0]['stream']
            selected=next((x for x in choices if x['stream']==stream),None)
            if not selected:return {'choices':choices,'selection_required':True}
            if record:
                saved=record['desired']['hold_music']
                need(saved['stream']==stream and Path(saved['directory']).resolve()==Path(selected['directory']),
                     'A different music folder already has a preserved baseline; review it in Advanced before switching')
                baseline=bounded_json(STATE/'hold-music-originals/manifest.json')
                actual={p.relative_to(selected['directory']).as_posix() for p in Path(selected['directory']).rglob('*.wav')}
                need(set(baseline['files'])==actual,'The music track set changed; review it before changing volume')
                current=saved['gain_db'] if record.get('enabled',True) else 0
            else:
                need(not (STATE/'hold-music-originals/manifest.json').exists(),'An unrecorded music baseline needs review in Advanced')
                current=None
            return {**selected,'gain_db':current,'recorded':bool(record),'scope':'Everyone using this music folder',
                    'label':f'{current:+g} dB from preserved tracks' if current is not None else 'Existing track level (not yet tracked)',
                    'record':record}
        phones,destinations=self.phones()
        saved=copy.deepcopy(record['desired']['call_volume']) if record else {'enabled':False,'extensions':phones,'destinations':destinations,'read_level':0,'write_level':0}
        # A custom gain rule could compound this adjustment. Require review instead of assuming neutral audio.
        owned=" AND coalesce(p.dialplan_description,'') NOT IN ('Managed by PBX Toolkit: PBX Toolkit originating phone gain','Managed by PBX Toolkit: PBX Toolkit answering phone gain')" if record else ''
        other=self.db.rows('SELECT DISTINCT p.dialplan_uuid FROM v_dialplans p JOIN v_dialplan_details d ON p.dialplan_uuid=d.dialplan_uuid WHERE p.dialplan_enabled=true AND d.dialplan_detail_enabled=true AND (p.domain_uuid='+literal(self.domain_id)+' OR (p.domain_uuid IS NULL AND p.dialplan_context='+literal(self.domain)+")) AND (d.dialplan_detail_type='set_audio_level' OR d.dialplan_detail_data ~ '(set_audio_level|call-volume.lua)')"+owned+' LIMIT 1')
        need(not other,'Existing custom volume rules need review in Advanced before adding another adjustment')
        profile=self.db.rows('SELECT sip_profile_uuid FROM v_sip_profiles WHERE sip_profile_name='+literal(self.c['internal_profile']))
        need(len(profile)==1,'Phone profile could not be identified; select it in Advanced')
        if not saved['enabled']:saved['read_level']=saved['write_level']=0
        return {**saved,'recorded':bool(record),'record':record,'scope':'Phones '+', '.join(saved['extensions'])+'; answering destinations '+', '.join(saved['destinations']),
                'label':f"Microphone {saved['read_level']:+d} / listening {saved['write_level']:+d} steps" if record else 'No toolkit adjustment; original PBX level',
                'fingerprint':hashlib.sha256(json.dumps(saved,sort_keys=True).encode()).hexdigest()}

    def plan(self, name, stream=None, gain=None, read=None, write=None, restore=False):
        state=self.current(name,stream)
        need(not state.get('selection_required'),'Choose one of the detected music streams')
        c=copy.deepcopy(self.c)
        if name=='hold-music':
            need(read is None and write is None,'Phone gain is separate from music volume')
            value=0 if restore else gain
            need(type(value) in (float,int) and math.isfinite(value) and -30<=value<=6,'Choose music gain between -30 and +6 dB')
            c['hold_music']={'enabled':not restore,'stream':state['stream'],'directory':state['directory'],'gain_db':value}
            after=f'{value:+g} dB from '+('preserved tracks' if state['recorded'] else 'the tracks as they are now')
        else:
            need(gain is None and stream is None,'Music gain is separate from phone volume')
            read=0 if restore else state['read_level'] if read is None else read
            write=0 if restore else state['write_level'] if write is None else write
            need(type(read) is int and type(write) is int and -4<=read<=4 and -4<=write<=4,'Phone gain must be an integer from -4 to +4')
            c['call_volume']={key:copy.deepcopy(state[key]) for key in features.DEFAULTS['call_volume']}
            c['call_volume'].update(enabled=not restore,read_level=read,write_level=write)
            after=f'Microphone {read:+d} / listening {write:+d} steps'
        if restore:need(state['recorded'],'No saved toolkit adjustment to restore yet')
        signature={'current':state,'desired':c,'active_site':digest(CONFIG) if self.same_site else None}
        token=hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()
        return {'domain':self.domain,'control':name,'before':state['label'],'after':after,'scope':state['scope'],
                'baseline_note':'The current tracks will be preserved before this first adjustment.' if name=='hold-music' and not state['recorded'] else '',
                'token':token,'config':c,'state':state}

    def apply(self, plan, expected):
        need(expected and expected==plan['token'],'Settings changed since review. Reopen the volume control and review the new level')
        name=plan['control'];c=plan['config'];previous=plan['state']['record'];marker=STATE/(name+'.json')
        # Existing records and files were checked while rebuilding the plan under the shared lock.
        before_marker=marker.read_bytes() if marker.exists() else None
        before_config=CONFIG.read_bytes() if self.same_site else None
        if not STATE.exists():
            STATE.mkdir(mode=0o755,parents=True);STATE.chmod(0o755)
        change=Change(self.db,name)
        try:
            if name=='hold-music':features.hold_music(self.db,c,change)
            else:
                if c['call_volume']['enabled']:
                    source=SOURCE/'assets/call-volume.lua'
                    need(source.is_file() and not source.is_symlink(),'Phone-volume helper is missing from this release')
                    content=source.read_bytes()
                    need(not HELPER.exists() or (not HELPER.is_symlink() and HELPER.read_bytes()==content),'Existing phone-volume helper differs; review it in Advanced')
                    change.directory(HELPER.parent,0o755,(0,0));change.file(HELPER,content,0o644,(0,0))
                features.call_volume(self.db,c,change,helper=HELPER)
                invalidate(c,['dialplan:'+self.domain])
            operations.save_managed(change,previous)
            key='hold_music' if name=='hold-music' else 'call_volume'
            record={'module':name,'revision':1,'desired':module_config(c,name),'backup':str(change.path),'enabled':c[key]['enabled']}
            atomic(marker,json.dumps(record,indent=2))
            if self.same_site:
                updated=copy.deepcopy(self.active);updated[key]=c[key]
                atomic(CONFIG,json.dumps(updated,indent=2)+'\n',0o644)
            return {'current':plan['after'],'scope':plan['scope'],'saved_copy':str(change.path),'restart_required':False}
        except BaseException:
            try:
                change.rollback()
                if before_marker is None:marker.unlink(missing_ok=True)
                else:atomic(marker,before_marker)
                if before_config is not None:atomic(CONFIG,before_config,0o644)
                if name=='hold-music':
                    refresh=change.path/'music-refresh.json'
                    if refresh.exists():
                        r=json.loads(refresh.read_text())
                        features.refresh_music({'hold_music':{'stream':r['stream']}},r['rates'])
                else:invalidate(c,['dialplan:'+self.domain])
            except Exception as recovery:
                raise Error('Volume change failed and recovery needs attention. Saved recovery data: '+str(change.path)) from recovery
            raise


def execute(args):
    """CLI and console share exactly the same preview/confirmation path."""
    from .base import supported
    supported()
    need(not args.enable,'Choose a new volume or --disable to restore the previous baseline')
    volume=Volume(args.domain,args.database or 'fusionpbx')
    need(args.name in ('hold-music','call-volume'),'Choose --name hold-music or call-volume')
    changing=args.gain_db is not None or args.read_level is not None or args.write_level is not None or args.disable
    if not changing:
        need(not args.apply,'Review a volume change before using --apply')
        result=volume.current(args.name,args.stream)
        result.pop('record',None)
        for choice in result.get('choices',[]):choice.pop('fingerprint',None)
        return result
    kwargs={'stream':args.stream,'gain':args.gain_db,'read':args.read_level,'write':args.write_level,'restore':args.disable}
    if not args.apply:
        plan=volume.plan(args.name,**kwargs)
        return {key:value for key,value in plan.items() if key not in ('config','state')}
    import fcntl
    with open('/run/lock/pbxctl.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Re-read discovery and configuration under the mutation lock.
        volume=Volume(args.domain,args.database or 'fusionpbx')
        return volume.apply(volume.plan(args.name,**kwargs),args.confirm)
