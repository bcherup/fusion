"""Build a source manifest and portable archive from reviewed project files."""
import ast
import hashlib
import json
from pathlib import Path
import tarfile
import zipfile

root=Path(__file__).resolve().parents[1]
paths=[]
for p in sorted(root.rglob('*')):
    if any(x in p.parts for x in ('.git','__pycache__','secrets','dist')):continue
    if not p.is_file() or p.name in ('site.json','MANIFEST.json') or p.suffix in ('.pyc','.log','.zip','.gz','.dump'):continue
    if p.is_symlink():raise RuntimeError('Linked source file')
    data=p.read_bytes().replace(b'\r\n',b'\n');p.write_bytes(data)
    if p.suffix=='.py':ast.parse(data,filename=str(p))
    paths.append(p)
manifest={'version':(root/'VERSION').read_text().strip(),'sha256':{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
(root/'MANIFEST.json').write_bytes((json.dumps(manifest,indent=2)+'\n').encode())
paths.append(root/'MANIFEST.json');out=root/'dist';out.mkdir(exist_ok=True)
with zipfile.ZipFile(out/'pbx-toolkit.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for p in paths:archive.write(p,'pbx-toolkit/'+p.relative_to(root).as_posix())
with tarfile.open(out/'pbx-toolkit.tar.gz','w:gz') as archive:
    for p in paths:
        info=archive.gettarinfo(str(p),arcname='pbx-toolkit/'+p.relative_to(root).as_posix());info.uid=info.gid=0;info.uname=info.gname='root';info.mode=0o755 if p.suffix=='.sh' or p.name=='pbxctl.py' else 0o644
        with p.open('rb') as data:archive.addfile(info,data)
print(json.dumps({'files':len(paths),'version':manifest['version'],'archives':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob('pbx-toolkit.*')}},indent=2))
