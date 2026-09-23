"""Install the pinned, local-only voicemail summary runtime."""
import grp
import json
import os
import platform
import pwd
import secrets
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from .common import ROOT, STATE, atomic, digest, need, run
from . import pbx, services

BASE = Path('/opt/pbxctl-ai')
RUNTIME_URL = 'https://github.com/ggml-org/llama.cpp/releases/download/b10964/llama-b10964-bin-ubuntu-x64.tar.gz'
RUNTIME_SHA = '9abf88aea48a55d0f80edb1ee20220b186848cca0b4e919d71518cfd7ca67443'
MODEL_URL = 'https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf'
MODEL_SHA = '6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e'
UNITS = ['pbxctl-ai-summary.timer', 'pbxctl-ai-summary.service', 'pbxctl-ai-model.service']


def download(url, path, expected):
    need(not path.is_symlink(), 'Download path cannot be linked')
    if path.exists():
        need(digest(path) == expected, 'Existing model/runtime differs from its pin'); return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.download-'); os.close(fd)
    try:
        run(['curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
             '--tlsv1.2', '--output', temporary, url], timeout=1800)
        need(digest(temporary) == expected, 'Model/runtime checksum mismatch; not activated')
        os.chmod(temporary, 0o644); os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def runtime():
    need(platform.machine() in ('x86_64', 'AMD64'), 'Pinned AI runtime currently supports Debian 13 amd64 only')
    need(not BASE.is_symlink(), 'AI runtime directory cannot be linked')
    BASE.mkdir(mode=0o755, parents=True, exist_ok=True); BASE.chmod(0o755)
    archive = BASE / 'llama-b10964.tar.gz'; download(RUNTIME_URL, archive, RUNTIME_SHA)
    model = BASE / 'qwen2.5-1.5b-instruct-q4_k_m.gguf'; download(MODEL_URL, model, MODEL_SHA)
    target = BASE / 'b10964'; manifest = BASE / 'runtime.json'
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=BASE, prefix='.runtime-') as stage:
            with tarfile.open(archive) as tar: tar.extractall(stage, filter='data')
            servers = list(Path(stage).rglob('llama-server'))
            need(len(servers) == 1 and servers[0].is_file() and not servers[0].is_symlink(), 'Unexpected runtime archive layout')
            relative = servers[0].relative_to(stage).as_posix()
            for top, dirs, files in os.walk(stage):
                os.chmod(top, 0o755)
                for name in files:
                    p = Path(top) / name
                    if not p.is_symlink(): p.chmod(0o755 if p.stat().st_mode & 0o111 else 0o644)
            shutil.copytree(stage, target, symlinks=True)
        hashes = {p.relative_to(target).as_posix(): digest(p) for p in target.rglob('*') if p.is_file()}
        atomic(manifest, json.dumps({'server': relative, 'sha256': hashes}), 0o600)
    need(manifest.is_file() and not manifest.is_symlink() and not target.is_symlink(), 'AI runtime manifest missing or linked')
    saved = json.loads(manifest.read_text())
    for name, expected in saved['sha256'].items():
        p = target / name
        need(target.resolve() in p.resolve().parents and p.is_file() and digest(p) == expected, 'Installed AI runtime changed')
    return target / saved['server'], model


def configure(db, c, ch):
    a = c['ai_summary']
    if not a['enabled']:
        if (STATE / 'ai-summary.json').exists():
            for name in UNITS: run(['systemctl', 'disable', '--now', name], check=False)
        return {'status': 'disabled', 'preserved': 'Models, recordings, transcripts, notes and worker progress'}
    need(c['transcription_enabled'] and (STATE / 'transcription.json').exists(), 'Enable toolkit-managed transcription before AI summaries')
    need(not Path('/etc/systemd/system/pbx-ai-model.service').exists(), 'Existing standalone AI deployment requires an explicit migration; refusing duplicate workers')
    need(not Path('/etc/systemd/system/pbxctl-ai-model.service').exists() or (STATE / 'ai-summary.json').exists(), 'Unmanaged AI unit exists; inspect before adoption')
    need(shutil.disk_usage('/opt').free >= 3 * 1024 ** 3, 'Keep at least 3 GiB free before installing AI')
    need(int(run(['getconf', 'LONG_BIT']).stdout.strip()) == 64, 'A 64-bit runtime is required')
    run(['apt-get', 'install', '-y', 'curl', 'ca-certificates', 'libgomp1', 'php-curl'], timeout=600)
    server, model = runtime()
    if run(['id', '-u', 'pbxctl-ai'], check=False).returncode:
        run(['useradd', '--system', '--home-dir', '/nonexistent', '--no-create-home', '--shell', '/usr/sbin/nologin', 'pbxctl-ai'])
    group = grp.getgrnam('pbxctl-ai').gr_gid
    worker = pwd.getpwnam(c['service_user']); worker_group = grp.getgrnam(c['service_group']).gr_gid
    secret = Path('/etc/pbxctl/ai-key')
    if not secret.exists(): ch.file(secret, secrets.token_hex(32) + '\n', 0o640, (0, group))
    need(not secret.is_symlink() and secret.stat().st_uid == 0 and secret.stat().st_gid == group and secret.stat().st_mode & 0o777 == 0o640, 'AI credential ownership/mode differs')
    state = STATE / 'ai'
    for p in (state, state / 'originals'):
        need(not p.is_symlink(), 'AI state cannot be linked')
        p.mkdir(mode=0o700, parents=True, exist_ok=True); p.chmod(0o700); os.chown(p, worker.pw_uid, worker_group)
    state_config = state / 'config.json'
    domain = pbx.domain(db, c)
    if not state_config.exists(): atomic(state_config, json.dumps({'domain_uuid': domain, 'since': int(time.time())}), 0o600, (worker.pw_uid, worker_group))
    else: need(json.loads(state_config.read_text())['domain_uuid'] == domain, 'AI state belongs to another domain')
    services.unit(ch, 'pbxctl-ai-model.service', f'''[Unit]
Description=Local voicemail summary model
After=network.target
[Service]
User=pbxctl-ai
Group=pbxctl-ai
ExecStart={server} -m {model} --host 127.0.0.1 --port {a['port']} -c 2048 -t 1 -tb 1 -b 128 -ub 64 -np 1 -ngl 0 --threads-http 2 --no-webui --api-key-file /etc/pbxctl/ai-key --log-disable
Restart=on-failure
RestartSec=30
TimeoutStopSec=15
Nice=19
CPUQuota={a['cpu_percent']}%
MemoryHigh={int(a['memory_mb'] * .85)}M
MemoryMax={a['memory_mb']}M
MemorySwapMax=0
TasksMax=32
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_UNIX
IPAddressDeny=any
IPAddressAllow=localhost
UMask=0077
StandardOutput=null
StandardError=null
[Install]
WantedBy=multi-user.target
''')
    services.unit(ch, 'pbxctl-ai-summary.service', f'''[Unit]
Description=Summarize new voicemail locally
After=postgresql.service freeswitch.service pbxctl-ai-model.service
[Service]
Type=oneshot
User={c['service_user']}
Group={c['service_group']}
SupplementaryGroups=pbxctl-ai
WorkingDirectory={c['web_root']}
ExecStart=/usr/bin/php {ROOT}/assets/ai/voicemail-summary.php
TimeoutStartSec=180
Nice=19
CPUQuota=20%
MemoryMax=256M
MemorySwapMax=0
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ReadWritePaths={STATE}/ai {STATE}/transcribe /var/cache/fusionpbx /dev/shm
RestrictAddressFamilies=AF_INET AF_UNIX
IPAddressDeny=any
IPAddressAllow=localhost
StandardError=null
''')
    services.timer(ch, 'pbxctl-ai-summary', '2min')
    run(['systemctl', 'daemon-reload']); run(['systemctl', 'enable', 'pbxctl-ai-model.service'])
    run(['systemctl', 'restart', 'pbxctl-ai-model.service'])
    ready = False
    for _ in range(45):
        response = run(['curl', '--noproxy', '*', '--fail', '--silent', '--max-time', '2', 'http://127.0.0.1:' + str(a['port']) + '/health'], check=False)
        if response.returncode == 0: ready = True; break
        time.sleep(1)
    need(ready, 'Local summary model failed its readiness check')
    run(['systemctl', 'enable', '--now', 'pbxctl-ai-summary.timer'])
    return {'engine': 'Local Qwen2.5 1.5B', 'scope': 'New opted-in voicemail; original text and audio preserved'}
