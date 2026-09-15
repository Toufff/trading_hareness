#!/usr/bin/env bash
# Turn the shared lightServer's sshd into public-key-only auth.
#
# The host shipped with "PasswordAuthentication yes" and "PermitRootLogin yes"
# on a public address. That is an exposure to the whole internet rather than a
# question of trusting any particular collaborator: at the time this was
# written the retained auth logs held 971 failed password attempts and zero
# successful ones.
#
# Run from an operator workstation:
#   ssh <alias> 'bash -s' < scripts/shared-peer/harden-lightserver-sshd.sh
# or copy it over and run as root. Idempotent - re-running only rewrites the
# drop-in and reloads.
#
# LOCKOUT SAFETY. The script refuses to change anything unless every account
# that has actually logged in over ssh already has a usable public key, and it
# reloads rather than restarts so established sessions (including the peer's
# long-lived forwards) survive. Recovery if something still goes wrong: the
# provider's serial/VNC console, then
#   rm /etc/ssh/sshd_config.d/10-harden-auth.conf && systemctl reload ssh
set -euo pipefail

DROPIN=/etc/ssh/sshd_config.d/10-harden-auth.conf
BACKUP="/etc/ssh/sshd_config.bak-$(date +%Y%m%d)"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

# The drop-in only wins if sshd_config actually includes the directory. sshd
# keeps the FIRST value it obtains for a keyword, so an Include that sits
# above the distribution's explicit "yes" lines makes the drop-in decisive;
# one placed below them would be silently ignored.
grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config || {
    echo "sshd_config does not Include /etc/ssh/sshd_config.d/*.conf; edit the main file instead" >&2
    exit 1
}
include_line=$(grep -nE '^[[:space:]]*Include' /etc/ssh/sshd_config | head -1 | cut -d: -f1)
password_line=$(grep -nE '^[[:space:]]*PasswordAuthentication' /etc/ssh/sshd_config | head -1 | cut -d: -f1 || true)
if [ -n "${password_line:-}" ] && [ "$include_line" -gt "$password_line" ]; then
    echo "Include (line $include_line) comes after PasswordAuthentication (line $password_line); the drop-in would lose" >&2
    exit 1
fi

echo "== pre-flight: every account with ssh login history must have a public key =="
# Every reader below is optional and every stage may legitimately match
# nothing, so each one absorbs its own failure: under `set -o pipefail` a
# missing /var/log/secure (Debian/Ubuntu) or an unmatched rotation glob would
# otherwise abort the script before it had checked a single account.
users_with_history=$( { cat /var/log/auth.log 2>/dev/null || true; \
    zcat /var/log/auth.log.*.gz 2>/dev/null || true; \
    cat /var/log/secure 2>/dev/null || true; } \
    | { grep -h 'Accepted' || true; } \
    | sed -nE 's/.* for (invalid user )?([A-Za-z0-9._-]+) from .*/\2/p' | sort -u)
[ -n "$users_with_history" ] || { echo "REFUSING: no successful ssh login found in the retained logs; cannot prove key coverage" >&2; exit 1; }
missing=""
for user in $users_with_history; do
    home=$(getent passwd "$user" | cut -d: -f6 || true)
    if [ -z "$home" ] || ! ssh-keygen -lf "$home/.ssh/authorized_keys" >/dev/null 2>&1; then
        missing="$missing $user"
    fi
    printf '  %-14s %s\n' "$user" "$(ssh-keygen -lf "$home/.ssh/authorized_keys" 2>/dev/null | wc -l) key(s)"
done
if [ -n "$missing" ]; then
    echo "REFUSING: these accounts have logged in but have no usable public key:$missing" >&2
    exit 1
fi

cp -n /etc/ssh/sshd_config "$BACKUP" 2>/dev/null || true
cat > "$DROPIN" <<'EOF'
# Public-key only. See scripts/shared-peer/harden-lightserver-sshd.sh.
# Keep this as a drop-in: sshd_config's Include sits above its own explicit
# PasswordAuthentication/PermitRootLogin lines, and sshd keeps the first value
# it obtains, so these override without editing the distribution file.
PasswordAuthentication no
PermitRootLogin prohibit-password
KbdInteractiveAuthentication no
EOF
chmod 0644 "$DROPIN"

sshd -t
# reload, never restart: a restart drops every established session, including
# the peer's database and gateway forwards.
systemctl reload ssh

echo "== effective after reload =="
sshd -T | grep -E '^(passwordauthentication|permitrootlogin|kbdinteractiveauthentication|pubkeyauthentication)'
echo
echo "Verify from a workstation, in a NEW connection, before closing this one:"
echo "  ssh <alias> true                                   # must succeed"
echo "  ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no <alias> true   # must be denied"
