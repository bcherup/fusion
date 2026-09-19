#!/usr/bin/python3
import json
import sys
sys.path.insert(0,'/opt/pbxctl')
from lib.common import STATE, Error
from lib.config import load_config
from lib.recovery import backup
from lib.offsite import upload

if __name__=='__main__':
    try:
        import fcntl
        with open('/run/lock/pbxctl-backup.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            c=load_config();directory=backup(c)
            if c['remote_backup']['enabled']:upload(c,directory)
            print(json.dumps({'local_backup':str(directory),'remote_enabled':c['remote_backup']['enabled']}))
    except Exception:
        print('Backup failed; completed recovery sets preserved',file=sys.stderr);sys.exit(1)
