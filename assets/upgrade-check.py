#!/usr/bin/python3
"""Read-only local Git/integration check. Never pulls, resets, stashes, or logs diffs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

def run(args):
    return subprocess.run(args,capture_output=True,text=True,timeout=30,env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'})

def inspect(web):
    results={}; roots=set(); issues=[]
    candidates=[web,web/'app/transcribe']
    if (web/'app').is_dir():
        candidates.extend(p for p in sorted((web/'app').iterdir()) if (p/'.git').exists() and p not in candidates)
    for candidate in candidates:
        if not candidate.is_dir(): continue
        # Trust only the explicitly supplied directory for this invocation, not globally.
        args=['git','-c','safe.directory='+str(candidate),'-C',str(candidate)]
        top=run(args+['rev-parse','--show-toplevel'])
        if top.returncode:
            results[str(candidate)]={'git':'unavailable or not a repository'}
            issues.append('Could not inspect Git metadata at '+str(candidate))
            continue
        root=Path(top.stdout.strip()).resolve()
        if root in roots: continue
        roots.add(root); args=['git','-c','safe.directory='+str(root),'-C',str(root)]
        status=run(args+['status','--porcelain','--untracked-files=no'])
        head=run(args+['rev-parse','HEAD'])
        divergence=run(args+['rev-list','--left-right','--count','HEAD...@{upstream}'])
        changes=status.stdout.splitlines()
        results[str(root)]={'head':head.stdout.strip(),'tracked_changes':changes,
             'ahead_behind_cached_upstream':divergence.stdout.strip() if divergence.returncode==0 else None,
             'upstream_note':'Local tracking reference only; no fetch performed'}
        if changes: issues.append('Tracked local edits exist in '+str(root))
    native=web/'app/transcribe/resources/classes/transcribe_local.php'
    adapter={'native_file_exists':native.is_file()}
    if native.is_file():
        adapter['native_sha256']=hashlib.sha256(native.read_bytes()).hexdigest()
        # Ownership checks for nested subdirectories can resolve to the web repo.
        for root in roots:
            if native.is_relative_to(root):
                rel=str(native.relative_to(root)); args=['git','-c','safe.directory='+str(root),'-C',str(root)]
                tracked=run(args+['ls-files','--error-unmatch','--',rel])
                if tracked.returncode==0:
                    adapter['tracked_by']=str(root)
                    adapter['differs_from_HEAD']=run(args+['diff','--quiet','HEAD','--',rel]).returncode!=0
    link=web/'app/pbxctl'; target=Path('/opt/pbxctl/assets/app')
    adapter['custom_link_ok']=link.is_symlink() and link.resolve()==target.resolve() and target.is_dir()
    if Path('/var/lib/pbxctl/transcription.json').exists() and not adapter['custom_link_ok']:
        issues.append('External transcription integration link missing or changed')
    config_path=Path('/etc/pbxctl/site.json')
    if adapter['custom_link_ok'] and config_path.is_file():
        config=json.loads(config_path.read_text())
        php='require '+json.dumps(str(web/'resources/require.php'))+"; exit(class_exists('transcribe_pbxctl_local') ? 0 : 1);"
        probe=run(['runuser','-u',config['service_user'],'--','php','-r',php])
        adapter['autoload_interface_compatible']=probe.returncode==0
        if probe.returncode: issues.append('Custom adapter no longer loads with the installed FusionPBX interface')
    summary={}
    if Path('/var/lib/pbxctl/ai-summary.json').exists() and config_path.is_file():
        config=json.loads(config_path.read_text())
        asset=Path('/opt/pbxctl/assets/ai/voicemail-summary.php')
        for name in ('summary.php','voicemail-summary.php','restore-summaries.php'):
            summary[name+'_syntax_ok']=run(['php','-l',str(asset.parent/name)]).returncode==0
        summary['database_interface_compatible']=run(['runuser','-u',config['service_user'],'--','php',str(asset),'--schema-check']).returncode==0
        if not all(summary.values()):issues.append('Local voicemail summary integration needs review after the application update')
    return {'repositories':results,'transcription':adapter,'ai_summary':summary,'issues':issues,
            'read_only':True,'does_not_predict_future_merge_conflicts':True}

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--web-root'); a=p.parse_args()
    config=Path('/etc/pbxctl/site.json')
    web=Path(a.web_root or (json.loads(config.read_text())['web_root'] if config.exists() else '/var/www/fusionpbx')).resolve()
    result=inspect(web); print(json.dumps(result,indent=2)); sys.exit(int(bool(result['issues'])))
