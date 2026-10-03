#!/usr/bin/env bash
# Nightly database dumps (OPS-06, INV-25; D-09, D-10, D-11, D-12).
#
# The `backup` service runs this script in its own container, on postgres:18.6-trixie: the
# same image tag as `db`, so pg_dump's major version matches the server's (D-09).
#
# Modes:
#   --once [--now EPOCH]  one schedule check: dump, verify and rotate when a dump is due
# Exit codes: 0 ok (or no dump due); 1 a dump failed; 2 a configuration or usage error
# (then nothing is written).
#
# Schedule (D-09, stateless): a dump is due when there is none yet, or when the newest dump,
# by the UTC time in its name, is older than the most recent BACKUP_TIME_UTC slot. That slot
# is always less than 24 h ago, so a newest dump older than 24 h gives a dump at once, at
# start and at any later check (INV-25 #1). Nothing is kept between runs. The age comes from
# the name, not the mtime, so a dump copied to a new server keeps its real age.
#
# Dump, verify, rotate (D-10): pg_dump -Fc writes a dot-prefixed .partial file in the backup
# directory. Only a non-empty file that passes pg_restore --list is renamed to
# powermon-YYYYMMDDTHHMMSSZ.dump (UTC), and only after that rename are the dumps beyond the
# newest BACKUP_KEEP deleted, by name. A failed dump removes its own temp file, deletes no
# dump and logs one error line. No ops notice is sent (D-12).
#
# Permissions (D-11): dumps hold every bot token and device key. umask 077 makes a new
# directory 0700 and every dump 0600. The container starts as root (the image has no USER):
# root only prepares the bind-mounted directory for the postgres user, then the script runs
# again as postgres through gosu, as the db entrypoint does (RESEARCH Pitfall 9).
#
# Settings (env): BACKUP_DIR (default /backups), BACKUP_TIME_UTC (HH:MM, default 03:00),
# BACKUP_KEEP (1-365, default 14), POSTGRES_HOST (default db), POSTGRES_PORT (default 5432),
# POSTGRES_DB, POSTGRES_USER and POSTGRES_PASSWORD (required).
#
# Secrets: libpq reads the password from PGPASSWORD, so it is in no command line. The script
# never prints an env value and never runs with set -x.

# No -e: a false (( )) or a "not due" check must not end the script; every step is checked
# explicitly instead (RESEARCH Pitfall 8).
set -u -o pipefail
umask 077
export LC_ALL=C
shopt -s nullglob

BACKUP_DIR=${BACKUP_DIR:-/backups}
BACKUP_TIME_UTC=${BACKUP_TIME_UTC:-03:00}
BACKUP_KEEP=${BACKUP_KEEP:-14}
# A dump's name: its UTC start time, fixed width, so byte order (LC_ALL=C) is time order.
DUMP_NAME_RE='^powermon-[0-9]{8}T[0-9]{6}Z\.dump$'
ARGS=("$@")

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

die() { log "error: $*"; exit 2; }

check_settings() {
  # Each message names the variable, never its value.
  if ! [[ $BACKUP_TIME_UTC =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    die "BACKUP_TIME_UTC must be HH:MM, from 00:00 to 23:59"
  fi
  if ! [[ $BACKUP_KEEP =~ ^[0-9]{1,3}$ ]] || (( 10#$BACKUP_KEEP < 1 || 10#$BACKUP_KEEP > 365 )); then
    die "BACKUP_KEEP must be a whole number from 1 to 365"
  fi
  BACKUP_KEEP=$(( 10#$BACKUP_KEEP ))
  local name
  for name in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD; do
    if [[ -z ${!name:-} ]]; then die "$name is not set"; fi
  done
}

pg_env() {
  # libpq settings from the app's POSTGRES_*: never on a command line, never logged.
  export PGHOST=${POSTGRES_HOST:-db} PGPORT=${POSTGRES_PORT:-5432} PGCONNECT_TIMEOUT=10
  export PGDATABASE=$POSTGRES_DB PGUSER=$POSTGRES_USER PGPASSWORD=$POSTGRES_PASSWORD
}

dumps() {
  # The dump paths, oldest first. Temp files and anything else in the directory are ignored.
  local path
  for path in "$BACKUP_DIR"/powermon-*.dump; do
    if [[ ${path##*/} =~ $DUMP_NAME_RE && -f $path ]]; then printf '%s\n' "$path"; fi
  done
  return 0
}

name_epoch() {
  # powermon-20261003T030010Z.dump -> its UTC time in epoch seconds; fails for an impossible time.
  local stamp=${1##*/} epoch
  stamp=${stamp#powermon-}
  stamp=${stamp%Z.dump}
  epoch=$(date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}" +%s 2>/dev/null) \
    || return 1
  [[ $(date -u -d "@$epoch" +%Y%m%dT%H%M%S) == "$stamp" ]] || return 1
  printf '%s\n' "$epoch"
}

newest() {
  # "EPOCH NAME" of the newest dump; fails when there is none.
  local all=() i epoch
  mapfile -t all < <(dumps)
  for (( i = ${#all[@]} - 1; i >= 0; i-- )); do
    if epoch=$(name_epoch "${all[i]}"); then
      printf '%s %s\n' "$epoch" "${all[i]##*/}"
      return 0
    fi
  done
  return 1
}

last_slot() {
  # The most recent BACKUP_TIME_UTC at or before epoch $1 (a UTC day is always 86400 s).
  local now=$1 slot
  slot=$(( now - now % 86400 + 10#${BACKUP_TIME_UTC:0:2} * 3600 + 10#${BACKUP_TIME_UTC:3:2} * 60 ))
  if (( slot > now )); then slot=$(( slot - 86400 )); fi
  printf '%s\n' "$slot"
}

is_due() {
  # A dump is due at epoch $1 when there is none, or the newest is older than the last slot.
  local newest_epoch _name
  if ! read -r newest_epoch _name < <(newest); then return 0; fi
  (( newest_epoch < $(last_slot "$1") ))
}

discard() {
  # A failed dump: remove its temp file ($1), keep every dump, log one error line ($2).
  rm -f -- "$1"
  log "error: $2"
}

rotate() {
  # Keep the newest BACKUP_KEEP dumps by name; runs only after a verified rename (D-10).
  local all=() i
  mapfile -t all < <(dumps)
  for (( i = 0; i < ${#all[@]} - BACKUP_KEEP; i++ )); do
    if rm -f -- "${all[i]}"; then
      log "removed ${all[i]##*/}"
    else
      log "error: cannot remove ${all[i]##*/}"
    fi
  done
}

dump() {
  # Dump the database as of epoch $1: pg_dump -> verify -> rename -> rotate (D-10).
  local name tmp
  name="powermon-$(date -u -d "@$1" +%Y%m%dT%H%M%S)Z.dump"
  tmp="$BACKUP_DIR/.$name.partial"
  if ! pg_dump -Fc --no-password -f "$tmp"; then
    discard "$tmp" "dump failed (pg_dump)"
    return 1
  fi
  if [[ ! -s $tmp ]]; then
    discard "$tmp" "dump failed verification (empty file)"
    return 1
  fi
  if ! pg_restore --list "$tmp" > /dev/null; then
    discard "$tmp" "dump failed verification (pg_restore --list)"
    return 1
  fi
  if ! chmod 0600 -- "$tmp" || ! mv -f -- "$tmp" "$BACKUP_DIR/$name"; then
    discard "$tmp" "dump failed (rename)"
    return 1
  fi
  rotate
  log "dump $name ok"
}

prepare_as_root() {
  # Docker creates a missing bind source as root:root 0755: hand it to the postgres user,
  # owner-only, then run this script again as postgres (D-11, RESEARCH Pitfall 9).
  local path
  if ! mkdir -p -- "$BACKUP_DIR" || ! chown -R postgres:postgres -- "$BACKUP_DIR" \
    || ! chmod 0700 -- "$BACKUP_DIR"; then
    log "error: cannot prepare the backup directory for the postgres user"
    exit 1
  fi
  for path in "$BACKUP_DIR"/* "$BACKUP_DIR"/.[!.]* "$BACKUP_DIR"/..?*; do
    if [[ -f $path && ! -L $path ]]; then chmod 0600 -- "$path"; fi
  done
  exec gosu postgres bash "$0" "$@"
}

usage() {
  die "unknown mode $(printf '%q' "${1:-}"); use --once [--now EPOCH]"
}

MODE=${1:-}
case $MODE in
  --once)
    shift
    NOW=$(date -u +%s)
    if (( $# > 0 )); then
      if [[ $# != 2 || $1 != --now || ! $2 =~ ^[0-9]{1,12}$ ]]; then
        die "--now needs a whole number of seconds since 1970-01-01 UTC"
      fi
      NOW=$(( 10#$2 ))
    fi
    ;;
  *)
    usage "$MODE"
    ;;
esac

check_settings
if [[ $(id -u) == 0 ]]; then
  prepare_as_root "${ARGS[@]}"
fi
if ! mkdir -p -- "$BACKUP_DIR"; then
  log "error: cannot create the backup directory"
  exit 1
fi
pg_env

case $MODE in
  --once)
    if ! is_due "$NOW"; then
      log "no dump due: the newest dump is from after the last $BACKUP_TIME_UTC UTC slot"
      exit 0
    fi
    dump "$NOW" || exit 1
    ;;
esac
exit 0
