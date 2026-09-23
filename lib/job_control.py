"""Pause or resume known background jobs without replacing an existing installation."""
import hashlib
import json
from .common import STATE, Error, atomic, need, run

JOBS={
 'transcription': [('pbxctl-transcribe','pbx-whisper.service'),('fusionpbx-local-transcribe','pbx-whisper.service')],
 'ai-summary': [('pbxctl-ai-summary','pbxctl-ai-model.service'),('pbx-ai-summary','pbx-ai-model.service')],
 'alerts': [('pbxctl-health',None),('fusionpbx-health',None)],
}


def units(names):
    text=run(['systemctl','show',*names,'--no-pager','--property=Id,LoadState,ActiveState,UnitFileState'],check=False).stdout
    result={}
    for block in text.strip().split('\n\n'):
        row=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
        if row.get('Id') in names:result[row['Id']]=row
    need(all(name in result for name in names),'Background-job status could not be read')
    return result


def inspect(name):
    need(name in JOBS,'Choose an available background job')
    names=list(dict.fromkeys(u for prefix,model in JOBS[name] for u in (prefix+'.timer',prefix+'.service',model) if u))
    state=units(names)
    found=[(prefix,model) for prefix,model in JOBS[name] if state[prefix+'.timer']['LoadState']!='not-found']
    need(len(found)==1,'No single existing job was found. Use Setup or review duplicate jobs in Advanced.')
    prefix,model=found[0]
    chosen=[prefix+'.timer',prefix+'.service']+([model] if model else [])
    selected={k:state[k] for k in chosen}
    need(all(x['LoadState']=='loaded' for x in selected.values()),'A required job service is missing or masked; inspect its details first')
    for key in (prefix+'.timer',model):
        if key:need(state[key]['UnitFileState'] in ('enabled','disabled'),'This job has a custom startup mode; review it in Advanced')
    return {'timer':prefix+'.timer','model':model,'units':selected,'running':state[prefix+'.timer']['ActiveState']=='active'}


def plan(name, enabled):
    state=inspect(name)
    marker=STATE/(name+'.json')
    record=marker.read_bytes() if marker.is_file() else b''
    if enabled and record:
        need(json.loads(record).get('enabled',True),'This feature is disabled in its configuration. Enable it through feature setup first.')
    token=hashlib.sha256(json.dumps([name,enabled,state,hashlib.sha256(record).hexdigest()],sort_keys=True).encode()).hexdigest()
    return {'before':'Running' if state['running'] else 'Paused','after':'Running' if enabled else 'Paused',
            'scope':'All mailboxes handled by this existing background job' if name!='alerts' else 'This server health-alert job',
            'note':'A job already in progress may finish. Models, settings and existing messages are retained.',
            'token':token,'state':state}


def apply(name, enabled, expected):
    p=plan(name,enabled);need(p['token']==expected,'Job state changed since review; refresh and try again')
    state=p['state'];changed=[]
    path=STATE/'job-controls'/name/'last-change.json'
    atomic(path,json.dumps({'before':state,'requested':enabled},indent=2))
    try:
        if enabled and state['model']:
            changed.append(state['model']);run(['systemctl','enable','--now',state['model']])
        changed.append(state['timer']);run(['systemctl','enable' if enabled else 'disable','--now',state['timer']])
        after=inspect(name)
        need(after['running']==enabled,'The background job did not reach the requested state')
        return {k:v for k,v in p.items() if k not in ('state','token')}
    except BaseException:
        try:
            for unit in reversed(changed):
                previous=state['units'][unit]
                run(['systemctl','enable' if previous['UnitFileState']=='enabled' else 'disable',unit])
                run(['systemctl','start' if previous['ActiveState']=='active' else 'stop',unit])
        except Exception as recovery:
            raise Error('Background job recovery needs attention. Saved state: '+str(path)) from recovery
        raise


def execute(args):
    from .base import supported
    supported();need(args.enable != args.disable,'Choose whether to pause or resume this job')
    if not args.apply:return {k:v for k,v in plan(args.name,args.enable).items() if k!='state'}
    import fcntl
    with open('/run/lock/pbxctl.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return apply(args.name,args.enable,args.confirm)
