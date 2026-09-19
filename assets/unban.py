#!/usr/bin/env python3
"""Unban this FusionPBX host's Event Guard and Fail2ban addresses.

Run as root:
    fusionpbx-unban --status
    fusionpbx-unban --all
    fusionpbx-unban --ip 172.58.165.24

Designed for this Debian PBX's iptables-backed Event Guard.
No services are stopped, no chains are flushed, and no allowlists are added.
Exit codes: 0 = requested bans clear; 1 = error; 2 = bans remain/reappeared.
"""
import argparse
import datetime
import fcntl
import ipaddress
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile

import sys
sys.path.insert(0, '/opt/pbxctl')
from lib.common import load
C = load()

CHAINS = ("sip-auth-ip", "sip-auth-fail")
BACKUP_ROOT = pathlib.Path("/root/pbxctl-backups")
DB = C['database']
PG_SOCKET = "/var/run/postgresql"
JAIL_LIST = re.compile(r"Jail list:\s*(.*)")


class UnbanError(Exception):
    pass


def run(args, *, data=None, timeout=30):
    result = subprocess.run(args, input=data, capture_output=True, text=True,
                            timeout=timeout, check=False)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise UnbanError(f"{args[0]} failed: {detail[:500]}")
    return result.stdout


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def psql(statement):
    return run(["runuser", "-u", "postgres", "--", "psql", "-X", "-qAt",
                "-h", PG_SOCKET, "-v", "ON_ERROR_STOP=1", "-d", DB],
               data=statement)


def rows(query):
    result = psql("BEGIN READ ONLY;\n"
                  "SELECT coalesce(json_agg(x),'[]'::json) FROM (" + query +
                  ") x;\nCOMMIT;\n")
    return json.loads(result)


def parse_event_guard_rules(text, family):
    """Recognize only the exact single-address DROP rules Event Guard creates."""
    recognized, unexpected = [], []
    for line in text.splitlines():
        tokens = shlex.split(line)
        if len(tokens) < 2 or tokens[0] != "-A" or tokens[1] not in CHAINS:
            continue
        valid = (len(tokens) == 6 and tokens[2] == "-s"
                 and tokens[4:] == ["-j", "DROP"])
        if valid:
            try:
                network = ipaddress.ip_network(tokens[3], strict=True)
                valid = (network.version == family
                         and network.prefixlen == network.max_prefixlen)
            except ValueError:
                valid = False
        if not valid:
            unexpected.append(line)
            continue
        recognized.append({"family": family, "chain": tokens[1],
                           "source": tokens[3],
                           "ip": str(network.network_address)})
    return recognized, unexpected


def event_guard_snapshot():
    rules, unexpected = [], []
    for family, binary in ((4, "iptables"), (6, "ip6tables")):
        found, other = parse_event_guard_rules(run([binary, "-w", "5", "-S"]), family)
        rules.extend(found)
        unexpected.extend({"family": family, "rule": line} for line in other)
    return {"rules": rules, "unexpected": unexpected}


def fail2ban_snapshot():
    status = run(["fail2ban-client", "status"])
    match = JAIL_LIST.search(status)
    if match is None:
        raise UnbanError("Could not read Fail2ban's jail list.")
    result = {}
    for jail in (item.strip() for item in match.group(1).split(",")):
        if not jail:
            continue
        output = run(["fail2ban-client", "get", jail, "banip"])
        try:
            result[jail] = [str(ipaddress.ip_address(value)) for value in output.split()]
        except ValueError as error:
            raise UnbanError(f"Unexpected ban list returned by jail {jail}.") from error
    return result


def target_rows(host, address):
    where = "hostname=" + sql_literal(host) + " AND log_status IN ('blocked','pending')"
    if address is not None:
        where += " AND ip_address=" + sql_literal(address)
    return rows("SELECT * FROM v_event_guard_logs WHERE " + where)


def target_rule(rule, address):
    return address is None or rule["ip"] == address


def snapshot(address):
    return {"event_guard": event_guard_snapshot(),
            "fail2ban": fail2ban_snapshot(),
            "event_guard_records": target_rows(socket.gethostname(), address)}


def matching_bans(state, address):
    guard = [r for r in state["event_guard"]["rules"] if target_rule(r, address)]
    f2b = {j: [ip for ip in ips if address is None or ip == address]
           for j, ips in state["fail2ban"].items()}
    return guard, {j: ips for j, ips in f2b.items() if ips}


def print_status(state, address):
    guard, f2b = matching_bans(state, address)
    print(f"Event Guard: {len(guard)} matching ban rule(s)")
    for rule in guard:
        print(f"  {rule['ip']}  [{rule['chain']}, IPv{rule['family']}]")
    print(f"Fail2ban: {sum(len(ips) for ips in f2b.values())} matching jail ban(s)")
    for jail, ips in f2b.items():
        print(f"  {jail}: {', '.join(ips)}")
    if state["event_guard"]["unexpected"]:
        print("Nonstandard Event Guard rules exist; they will be preserved.")
    if state["event_guard_records"]:
        print(f"Event Guard has {len(state['event_guard_records'])} matching "
              "blocked/pending log record(s) for this hostname.")


def make_backup(before):
    BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = pathlib.Path(tempfile.mkdtemp(prefix=stamp + "-unban-script-", dir=BACKUP_ROOT))
    (path / "before.json").write_text(json.dumps(before, indent=2) + "\n")
    for binary, filename in (("iptables-save", "firewall4-before.txt"),
                             ("ip6tables-save", "firewall6-before.txt")):
        (path / filename).write_text(run([binary]))
    for filename in ("rules.v4", "rules.v6"):
        source = pathlib.Path("/etc/iptables") / filename
        if source.is_file():
            shutil.copyfile(source, path / ("persistent-" + filename))
    return path


def clear_log_records(records):
    """Update only the rows captured before this run, on the current PBX."""
    identifiers = []
    for record in records:
        value = record["event_guard_log_uuid"]
        if not re.fullmatch(r"[a-fA-F0-9-]{36}", value):
            raise UnbanError("Unexpected Event Guard log identifier.")
        identifiers.append(sql_literal(value))
    if not identifiers:
        return
    psql("BEGIN;\nSET LOCAL lock_timeout='5s';\nSET LOCAL statement_timeout='10s';\n"
         "UPDATE v_event_guard_logs SET log_status='unblocked',update_date=now() "
         "WHERE hostname=" + sql_literal(socket.gethostname()) +
         " AND event_guard_log_uuid IN (" + ",".join(identifiers) + ") "
         "AND log_status IN ('blocked','pending');\nCOMMIT;\n")


def delete_guard_rule(rule):
    binary = "iptables" if rule["family"] == 4 else "ip6tables"
    args = [binary, "-w", "5", "-D", rule["chain"], "-s", rule["source"], "-j", "DROP"]
    try:
        run(args)
    except UnbanError:
        # Another administrator or service may already have removed the rule.
        remaining = event_guard_snapshot()["rules"]
        if rule in remaining:
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true", help="clear all bans on this PBX")
    mode.add_argument("--ip", type=ipaddress.ip_address, help="clear one IPv4 or IPv6 address")
    mode.add_argument("--status", action="store_true", help="show bans without changing them")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise UnbanError("Run this script from a root shell (su -), or with sudo.")
    os.umask(0o077)
    for program in ("iptables", "ip6tables", "iptables-save", "ip6tables-save",
                    "fail2ban-client", "runuser", "psql"):
        if shutil.which(program) is None:
            raise UnbanError("Required command is missing: " + program)
    if not pathlib.Path(PG_SOCKET).is_dir():
        raise UnbanError("Local PostgreSQL socket directory is missing.")
    lock_fd = os.open("/run/lock/fusionpbx-unban.lock",
                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UnbanError("Another unban run is already active.") from error
        address = str(args.ip) if args.ip is not None else None
        before = snapshot(address)
        if args.status:
            print_status(before, address)
            return 0
        backup = make_backup(before)
        print("Backup:", backup, flush=True)
        try:
            run(["fail2ban-client", "unban", address if address else "--all"])
            for rule in before["event_guard"]["rules"]:
                if target_rule(rule, address):
                    delete_guard_rule(rule)
            clear_log_records(before["event_guard_records"])
            after = snapshot(address)
            (backup / "after.json").write_text(json.dumps(after, indent=2) + "\n")
            print_status(after, address)
            guard, f2b = matching_bans(after, address)
            if guard or f2b or after["event_guard"]["unexpected"] or after["event_guard_records"]:
                print("Not fully clear: a ban remains, reappeared, or a nonstandard "
                      "rule needs review. Protection services are still running.")
                return 2
            print("Requested bans are clear. Protection services remain running; "
                  "a new failed authentication can create a fresh ban.")
            return 0
        except Exception as error:
            (backup / "error.txt").write_text(str(error) + "\n")
            raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (UnbanError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("ERROR:", error, file=sys.stderr)
        sys.exit(1)
