"""Disposable PostgreSQL integration fixture; opt in only inside test environments."""
import os
from pathlib import Path
import sys
import tempfile
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lib.common import Database,run
from lib.config import load_config
from lib.recovery import scratch_restore

if os.environ.get('PBXCTL_INTEGRATION')!='1':raise SystemExit('Set PBXCTL_INTEGRATION=1 only in an isolated test environment')
name='pbxctl_test_'+uuid.uuid4().hex[:12];created=False
try:
    run(['runuser','-u','postgres','--','createdb',name]);created=True
    db=Database(name);db.execute("CREATE TABLE v_domains(domain_name text); INSERT INTO v_domains VALUES ('voip.example.com');")
    with tempfile.TemporaryDirectory() as temp:
        db.dump(Path(temp)/'database.dump')
        scratch_restore(temp,load_config(Path(__file__).resolve().parents[1]/'site.example.json'))
    print('PostgreSQL custom dump and isolated scratch restore: passed')
finally:
    if created:run(['runuser','-u','postgres','--','dropdb','--force',name])
