"""Exercise the PHP adapter with synthetic audio and a pinned upstream interface."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body=self.rfile.read(int(self.headers['Content-Length']))
        assert b'fixture.wav' in body and b'response_format' in body
        status={'/ok':200,'/json':200,'/failure':503,'/redirect':302}[self.path]
        self.send_response(status)
        if status==302:self.send_header('Location','/ok')
        self.end_headers()
        if self.path=='/json':
            assert b'verbose_json' in body
            self.wfile.write(json.dumps({'segments':[{'text':' First sentence. ','start':0,'end':1.5},{'text':'Second sentence.','start':1.5,'end':3}]}).encode())
        else:self.wfile.write(b' First sentence. Second sentence. ' if status==200 else b'')
    def log_message(self,*args):pass

def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    php=[os.environ.get('PHP_PATH','php'),'-n']
    if os.environ.get('PHP_EXTENSION_DIR'):php+=['-d','extension_dir='+os.environ['PHP_EXTENSION_DIR']]
    php+=['-d','extension=curl']
    try:
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);interface=Path(os.environ.get('TRANSCRIBE_INTERFACE',str(directory/'transcribe_interface.php')))
            if not interface.exists():
                url='https://raw.githubusercontent.com/fusionpbx/fusionpbx-app-transcribe/d0c5420e945a1303f7f9bc49c027820d5b72deda/resources/classes/transcribe_interface.php'
                interface.write_bytes(urllib.request.urlopen(url,timeout=30).read())
            (directory/'fixture.wav').write_bytes(b'RIFF synthetic fixture')
            for endpoint,expected in [('ok','First sentence. Second sentence.'),('failure',''),('redirect',''),('json',None)]:
                p=subprocess.run([*php,ROOT/'tests/test_adapter.php',interface,'http://127.0.0.1:'+str(server.server_port)+'/'+endpoint,tmp,'json' if endpoint=='json' else 'text'],capture_output=True,text=True,check=True)
                data=json.loads(p.stdout)
                assert (data['text']==expected if expected is not None else len(json.loads(data['text'])['segments'])==2)
                assert data['unsupported_format_empty'] and data['implements_interface'] and not p.stderr
                print(endpoint+': passed')
    finally:server.shutdown();server.server_close()

if __name__=='__main__':main()
