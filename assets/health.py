#!/usr/bin/python3
import json
import sys
sys.path.insert(0,'/opt/pbxctl')
from lib.config import load_config
from lib.health import monitor
if __name__=='__main__':
    try:
        result=monitor(load_config());print(json.dumps(result));sys.exit(0 if result['healthy'] else 1)
    except Exception:
        print('Health monitor failed; inspect configuration locally');sys.exit(1)
