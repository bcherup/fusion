#!/usr/bin/python3
import argparse
import json
import sys
sys.path.insert(0,'/opt/pbxctl')
from lib.config import load_config
from lib import firewall
p=argparse.ArgumentParser();p.add_argument('--rollback',required=True);a=p.parse_args()
firewall.rollback(a.rollback)
