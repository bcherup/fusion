#!/usr/bin/python3
"""Deploy only the configured Certbot lineage; never restart FreeSWITCH."""
import argparse
import datetime as dt
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import syslog
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

import sys
sys.path.insert(0, '/opt/pbxctl')
from lib.common import load
C = load()
HOST = C['domain']
LINEAGE = Path('/etc/letsencrypt/live') / HOST
BASE = Path('/etc/freeswitch-tls')
ROOT = BASE / HOST
CURRENT = ROOT / 'current'
BACKUPS = Path('/root/pbxctl-backups')
CA = Path('/etc/ssl/certs/ca-certificates.crt')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(args):
    p = subprocess.run(args, capture_output=True, timeout=25)
    require(p.returncode == 0, 'Command validation failed: ' + args[0])
    return p.stdout


def log(message):
    syslog.openlog('groundwire-cert-deploy')
    syslog.syslog(syslog.LOG_NOTICE, message)
    print(message)


def root_directory(path, gid, mode):
    if path.exists() or path.is_symlink():
        require(not path.is_symlink() and path.is_dir(), 'Unsafe deployment directory')
        st = path.stat()
        require(st.st_uid == 0 and not st.st_mode & 0o022, 'Unsafe directory ownership/mode')
    else:
        path.mkdir(mode=mode)
    os.chown(path, 0, gid)
    os.chmod(path, mode)


def private_write(path, data, gid=0, mode=0o600):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(path, 0, gid)
    os.chmod(path, mode)


def switch_target(target):
    tmp = ROOT / ('current.new.' + str(os.getpid()))
    require(not tmp.exists() and not tmp.is_symlink(), 'Temporary link already exists')
    tmp.symlink_to(target)
    os.replace(tmp, CURRENT)
    fd = os.open(ROOT, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def handshake(expected):
    for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
        context = ssl.create_default_context()
        context.minimum_version = version
        context.maximum_version = version
        with socket.create_connection((C['lan_ip'], C['tls_port']), timeout=5) as tcp:
            with context.wrap_socket(tcp, server_hostname=HOST) as tls:
                actual = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
                require(actual == expected, 'Listener certificate differs from renewed certificate')


def reload_and_verify(expected):
    # Compiled mod_sofia implements CERT_RELOAD with nua_reload_tls. This reloads
    # TLS contexts without stopping profiles, their registrations, or calls.
    pid = run(['systemctl', 'show', 'freeswitch', '-p', 'MainPID', '--value']).strip()
    require(pid != b'0', 'FreeSWITCH is not running')
    answer = run(['fs_cli', '-x', 'reloadcert'])
    require(b'+OK cert reload event sent' in answer, 'Certificate reload event rejected')
    for attempt in range(10):
        time.sleep(1)
        try:
            handshake(expected)
            require(run(['systemctl', 'show', 'freeswitch', '-p', 'MainPID', '--value']).strip() == pid,
                    'FreeSWITCH process changed during certificate reload')
            return
        except (OSError, RuntimeError):
            if attempt == 9:
                raise RuntimeError('Reloaded TLS listener verification failed') from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true', help='Initial deployment before TLS listener activation')
    parser.add_argument('--reload-test', action='store_true', help='Verify a live reload of the existing certificate')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Root required')
    os.umask(0o077)
    renewed = os.environ.get('RENEWED_LINEAGE')
    if not args.prepare and not args.reload_test:
        # Certbot invokes this executable only as a deploy hook after renewal.
        if renewed != str(LINEAGE):
            return
    require(not (args.prepare and args.reload_test), 'Choose one mode')
    lock = open('/run/lock/groundwire-cert-deploy.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    cert_bytes = (LINEAGE / 'cert.pem').read_bytes()
    fullchain = (LINEAGE / 'fullchain.pem').read_bytes()
    chain = (LINEAGE / 'chain.pem').read_bytes()
    key_bytes = (LINEAGE / 'privkey.pem').read_bytes()
    cert = x509.load_pem_x509_certificate(cert_bytes)
    key = serialization.load_pem_private_key(key_bytes, password=None)
    public = lambda obj: obj.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    require(public(cert) == public(key), 'Certificate/private-key mismatch')
    require(HOST in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName),
            'Required certificate SAN missing')
    require(cert.not_valid_after_utc > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7), 'Certificate expires too soon')
    run(['openssl', 'verify', '-purpose', 'sslserver', '-verify_hostname', HOST,
         '-CAfile', str(CA), '-untrusted', str(LINEAGE / 'chain.pem'), str(LINEAGE / 'cert.pem')])
    expected = cert.fingerprint(hashes.SHA256()).hex()
    require(x509.load_pem_x509_certificate(fullchain).fingerprint(hashes.SHA256()).hex() == expected,
            'Full chain leaf differs from certificate')
    gid = grp.getgrnam(C['service_group']).gr_gid  # Verified FreeSWITCH runtime group.
    root_directory(BASE, gid, 0o750)
    root_directory(ROOT, gid, 0o750)
    require(not CURRENT.exists() or CURRENT.is_symlink(), 'Current certificate must be a managed link')
    old = os.readlink(CURRENT) if CURRENT.is_symlink() else None
    if old:
        require('/' not in old and old.startswith('cert-'), 'Unexpected previous target')
    target = 'cert-' + expected
    agent = fullchain.rstrip() + b'\n' + key_bytes.rstrip() + b'\n'
    cafile = chain.rstrip() + b'\n' + CA.read_bytes()
    destination = ROOT / target
    if destination.exists():
        require(not destination.is_symlink(), 'Unsafe certificate version directory')
        require((destination / 'agent.pem').read_bytes() == agent, 'Existing version content differs')
    else:
        root_directory(destination, gid, 0o750)
        private_write(destination / 'agent.pem', agent, gid, 0o640)
        private_write(destination / 'cafile.pem', cafile, gid, 0o640)
    changed = old != target
    if changed:
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%f-cert-deploy')
        backup = BACKUPS / stamp
        backup.mkdir(mode=0o700, parents=True)
        private_write(backup / 'previous-target.json', json.dumps({'previous': old, 'new': target}).encode())
        if old:
            shutil.copytree(ROOT / old, backup / 'previous-certificate')
        switch_target(target)
    if args.prepare:
        log('Validated certificate prepared for ' + HOST + '; listener activation pending')
        return
    try:
        if changed or args.reload_test:
            reload_and_verify(expected)
        else:
            handshake(expected)
    except Exception:
        if changed and old:
            switch_target(old)
            old_cert = x509.load_pem_x509_certificate((CURRENT / 'agent.pem').read_bytes())
            try:
                reload_and_verify(old_cert.fingerprint(hashes.SHA256()).hex())
                log('New certificate failed listener verification; previous certificate restored')
            except Exception:
                log('ERROR: previous files restored but listener verification failed; manual attention required')
        raise RuntimeError('Certificate deployment failed; see journal for rollback status') from None
    log('Certificate deployment and TLS 1.2/1.3 verification passed for ' + HOST + '; no service restart')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never emit exception details that could contain certificate/key material.
        log('ERROR: certificate deployment failed (' + type(error).__name__ + '); inspect configuration locally')
        sys.exit(1)
