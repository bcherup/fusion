#!/usr/bin/env bash
set -Eeuo pipefail

# FusionPBX firewall setup for Debian 13 (Trixie).
# Uses the nftables-backed iptables compatibility layer supplied by Debian.
# Run from a local/VM console when possible because this changes INPUT policy.

if [[ ${EUID} -ne 0 ]]; then
    echo "ERROR: run this script as root."
    exit 1
fi

if [[ -r /etc/os-release ]]; then
    . /etc/os-release
fi

if [[ "${VERSION_CODENAME:-}" != "trixie" && "${ALLOW_UNSUPPORTED:-0}" != "1" ]]; then
    echo "ERROR: this script is intended for Debian 13 (trixie)."
    echo "Set ALLOW_UNSUPPORTED=1 only if you intentionally want to override this check."
    exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/root/firewall-backups/${STAMP}"

mkdir -p "${BACKUP_DIR}"

echo
echo "=============================================="
echo " FusionPBX Debian 13 / Trixie Firewall"
echo "=============================================="
echo

echo "[1/8] Backing up current firewall rules..."
iptables-save > "${BACKUP_DIR}/iptables-before.rules" 2>/dev/null || true
ip6tables-save > "${BACKUP_DIR}/ip6tables-before.rules" 2>/dev/null || true
echo "  Backup: ${BACKUP_DIR}"

echo "[2/8] Installing firewall packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y     iptables     iptables-persistent     netfilter-persistent

echo "[3/8] Selecting Debian nftables-backed iptables..."

if [[ -x /usr/sbin/iptables-nft ]]; then
    update-alternatives --set iptables /usr/sbin/iptables-nft
fi

if [[ -x /usr/sbin/ip6tables-nft ]]; then
    update-alternatives --set ip6tables /usr/sbin/ip6tables-nft
fi

iptables --version
ip6tables --version

SSH_PORT="$(
    sshd -T 2>/dev/null |
    awk '$1 == "port" { print $2; exit }'
)"
SSH_PORT="${SSH_PORT:-22}"

echo "  Detected SSH port: ${SSH_PORT}"

remove_all_jump() {
    local tool="$1"
    local parent="$2"
    local target="$3"

    while "${tool}" -C "${parent}" -j "${target}" 2>/dev/null; do
        "${tool}" -D "${parent}" -j "${target}"
    done
}

delete_rule_all() {
    local tool="$1"
    shift

    while "${tool}" -C "$@" 2>/dev/null; do
        "${tool}" -D "$@"
    done
}

echo "[4/8] Building IPv4 rules..."

# Keep the server reachable while the ruleset is built.
iptables -P INPUT ACCEPT
iptables -P FORWARD ACCEPT
iptables -P OUTPUT ACCEPT

iptables -N FUSIONPBX-IN 2>/dev/null || true
iptables -F FUSIONPBX-IN

# Normalize existing FusionPBX/Fail2ban jumps so they are evaluated
# before the base allow rules and are not duplicated.
remove_all_jump iptables INPUT FUSIONPBX-IN

if iptables -S sip-auth-fail >/dev/null 2>&1; then
    remove_all_jump iptables INPUT sip-auth-fail
    iptables -A INPUT -j sip-auth-fail
fi

if iptables -S sip-auth-ip >/dev/null 2>&1; then
    remove_all_jump iptables INPUT sip-auth-ip
    iptables -A INPUT -j sip-auth-ip
fi

iptables -A INPUT -j FUSIONPBX-IN

iptables -A FUSIONPBX-IN -i lo -j ACCEPT
iptables -A FUSIONPBX-IN     -m conntrack     --ctstate ESTABLISHED,RELATED     -j ACCEPT

# SSH is established before the default INPUT policy becomes DROP.
iptables -A FUSIONPBX-IN     -p tcp     --dport "${SSH_PORT}"     -j ACCEPT

iptables -A FUSIONPBX-IN     -p icmp     --icmp-type echo-request     -j ACCEPT

# Scanner filters mirrored from the standard FusionPBX firewall behavior.
for STRING in "friendly-scanner" "sipcli/"; do
    iptables -A FUSIONPBX-IN         -p udp         --dport 5060:5091         -m string         --string "${STRING}"         --algo bm         --icase         -j DROP

    iptables -A FUSIONPBX-IN         -p tcp         --dport 5060:5091         -m string         --string "${STRING}"         --algo bm         --icase         -j DROP
done

# FusionPBX web interfaces.
iptables -A FUSIONPBX-IN -p tcp --dport 80 -j ACCEPT
iptables -A FUSIONPBX-IN -p tcp --dport 443 -j ACCEPT
iptables -A FUSIONPBX-IN -p tcp --dport 7443 -j ACCEPT

# SIP.
iptables -A FUSIONPBX-IN -p tcp --dport 5060:5091 -j ACCEPT
iptables -A FUSIONPBX-IN -p udp --dport 5060:5091 -j ACCEPT

# RTP media.
iptables -A FUSIONPBX-IN -p udp --dport 16384:32768 -j ACCEPT

# OpenVPN port retained for compatibility with the standard FusionPBX rules.
iptables -A FUSIONPBX-IN -p udp --dport 1194 -j ACCEPT

# Remove duplicate QoS rules from previous runs, then add one copy.
delete_rule_all iptables -t mangle OUTPUT     -p udp --sport 16384:32768     -j DSCP --set-dscp 46

delete_rule_all iptables -t mangle OUTPUT     -p udp --sport 5060:5091     -j DSCP --set-dscp 26

delete_rule_all iptables -t mangle OUTPUT     -p tcp --sport 5060:5091     -j DSCP --set-dscp 26

iptables -t mangle -A OUTPUT     -p udp --sport 16384:32768     -j DSCP --set-dscp 46

iptables -t mangle -A OUTPUT     -p udp --sport 5060:5091     -j DSCP --set-dscp 26

iptables -t mangle -A OUTPUT     -p tcp --sport 5060:5091     -j DSCP --set-dscp 26

echo "[5/8] Building IPv6 rules..."

ip6tables -P INPUT ACCEPT
ip6tables -P FORWARD ACCEPT
ip6tables -P OUTPUT ACCEPT

ip6tables -N FUSIONPBX6-IN 2>/dev/null || true
ip6tables -F FUSIONPBX6-IN

remove_all_jump ip6tables INPUT FUSIONPBX6-IN
ip6tables -A INPUT -j FUSIONPBX6-IN

ip6tables -A FUSIONPBX6-IN -i lo -j ACCEPT
ip6tables -A FUSIONPBX6-IN     -m conntrack     --ctstate ESTABLISHED,RELATED     -j ACCEPT

# ICMPv6 is required for normal IPv6 operation and neighbor discovery.
ip6tables -A FUSIONPBX6-IN -p ipv6-icmp -j ACCEPT

ip6tables -A FUSIONPBX6-IN     -p tcp     --dport "${SSH_PORT}"     -j ACCEPT

ip6tables -A FUSIONPBX6-IN -p tcp --dport 80 -j ACCEPT
ip6tables -A FUSIONPBX6-IN -p tcp --dport 443 -j ACCEPT
ip6tables -A FUSIONPBX6-IN -p tcp --dport 7443 -j ACCEPT

ip6tables -A FUSIONPBX6-IN -p tcp --dport 5060:5091 -j ACCEPT
ip6tables -A FUSIONPBX6-IN -p udp --dport 5060:5091 -j ACCEPT
ip6tables -A FUSIONPBX6-IN -p udp --dport 16384:32768 -j ACCEPT
ip6tables -A FUSIONPBX6-IN -p udp --dport 1194 -j ACCEPT

echo "[6/8] Enabling default DROP policies..."

iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT

ip6tables -P INPUT DROP
ip6tables -P FORWARD DROP
ip6tables -P OUTPUT ACCEPT

echo "[7/8] Saving persistent rules..."

mkdir -p /etc/iptables

iptables-save > /etc/iptables/rules.v4
ip6tables-save > /etc/iptables/rules.v6

netfilter-persistent save
systemctl enable netfilter-persistent

echo "[8/8] Verifying configuration..."

echo
echo "IPv4 policies:"
iptables -S | head -20

echo
echo "IPv6 policies:"
ip6tables -S | head -20

echo
echo "Persistence:"
systemctl is-enabled netfilter-persistent

echo
echo "=============================================="
echo " Firewall configuration complete"
echo "=============================================="
echo
echo "Rules were saved to:"
echo "  /etc/iptables/rules.v4"
echo "  /etc/iptables/rules.v6"
echo
echo "Original rules backup:"
echo "  ${BACKUP_DIR}"
echo
echo "Before exposing the PBX publicly, verify SSH, HTTPS,"
echo "SIP registration, inbound/outbound calls, and RTP audio."
echo
