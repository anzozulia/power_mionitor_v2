#!/usr/bin/env bash
# Nightly database dumps and the restore tool (OPS-06, INV-25; D-09 ... D-12, D-15, D-16).
#
# The `backup` service runs this script in its own container, on postgres:18.6-trixie: the
# same image tag as `db`, so pg_dump's major version matches the server's (D-09).
#
# Modes:
#   (no argument)           the container loop: removes leftover temp files, checks every
#                           60 s and dumps when a dump is due. A failed dump is retried after
#                           5 min, doubling to at most 1 h, not at every check. Exits 0 at once
#                           on SIGTERM or SIGINT (the image's stop signal).
#   --once [--now EPOCH]    one schedule check: dump, verify and rotate when a dump is due
#   --health [--now EPOCH]  the container healthcheck: exit 0 only while the newest dump, by
#                           the time in its name, is less than 26 h old (D-11) and no dump is
#                           dated more than 5 min in the future (F-15). It reads file names
#                           only, needs no database setting and runs as root, read-only.
#   --dump-now              dump, verify and rotate now, whatever the schedule (D-16: the
#                           restore drill, or a dump before a risky deploy)
#   --restore NAME          pg_restore the dump NAME from the backup directory into the
#                           database, only when its public schema has no table yet (D-15).
#                           It never drops or cleans anything.
# Exit codes: 0 ok (or no dump due); 1 a dump, health or restore failure or refusal; 2 a
# configuration or usage error (then nothing is written).
#
# Schedule (D-09, stateless): a dump is due when there is none yet, or when the newest dump,
# by the UTC time in its name, is older than the most recent BACKUP_TIME_UTC slot. That slot
# is always less than 24 h ago, so a newest dump older than 24 h gives a dump at once, at
# start and at any later check (INV-25 #1). Nothing is kept between runs. The age comes from
# the name, not the mtime, so a dump copied to a new server keeps its real age. A dump dated
# more than FUTURE_SLACK_S (5 min) after the clock is ignored by the schedule and the health
# age, so it never stops the nightly dumps; --health is unhealthy until it is moved out (F-15).
#
# Dump, verify, rotate (D-10): pg_dump -Fc writes a dot-prefixed .partial file in the backup
# directory. Only a non-empty file that passes pg_restore --list is renamed to
# powermon-YYYYMMDDTHHMMSSZ.dump (UTC), and only after that rename are the dumps beyond the
# newest BACKUP_KEEP deleted, by name. A failed dump removes its own temp file, deletes no
# dump and logs one error line. No ops notice is sent: the healthcheck and the log show a
# failing backup (D-12).
#
# Permissions (D-11): dumps hold every bot token and device key. umask 077 makes a new
# directory 0700 and every dump 0600. The container starts as root (the image has no USER):
# root only prepares the bind-mounted directory for the postgres user, then the script runs
# again as postgres through gosu, as the db entrypoint does (RESEARCH Pitfall 9).
#
# Settings (env): BACKUP_DIR (default /backups), BACKUP_TIME_UTC (HH:MM, default 03:00),
# BACKUP_KEEP (1-365, default 14), POSTGRES_HOST (default db), POSTGRES_PORT (default 5432),
# POSTGRES_DB, POSTGRES_USER and POSTGRES_PASSWORD (required). Test-only knobs, never set
# in .env.example or the compose files: BACKUP_CHECK_EVERY_S (60), BACKUP_RETRY_FIRST_S
# (300) and BACKUP_RETRY_MAX_S (3600).
#
# Secrets: libpq reads the password from PGPASSWORD, so it is in no command line. The script
# never prints an env value and never runs with set -x.

# No -e: a false (( )) or a "not due" check must not end the loop; every step is checked
# explicitly instead (RESEARCH Pitfall 8).
set -u -o pipefail
umask 077
export LC_ALL=C
shopt -s nullglob

BACKUP_DIR=${BACKUP_DIR:-/backups}
BACKUP_TIME_UTC=${BACKUP_TIME_UTC:-03:00}
BACKUP_KEEP=${BACKUP_KEEP:-14}
BACKUP_CHECK_EVERY_S=${BACKUP_CHECK_EVERY_S:-60}
BACKUP_RETRY_FIRST_S=${BACKUP_RETRY_FIRST_S:-300}
BACKUP_RETRY_MAX_S=${BACKUP_RETRY_MAX_S:-3600}
# A dump's name: its UTC start time, fixed width, so byte order (LC_ALL=C) is time order.
DUMP_NAME_RE='^powermon-[0-9]{8}T[0-9]{6}Z\.dump$'
# Healthy while the newest dump is younger than this: a night's slot plus two hours.
HEALTH_MAX_AGE_S=$(( 26 * 3600 ))
# A dump dated more than this after the clock is from a clock that was ahead: the schedule and
# the health age ignore it, and --health reports it until it is moved out (F-15).
FUTURE_SLACK_S=300
# --restore restores only while this is 0 (D-15).
TABLE_COUNT_SQL="SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
ARGS=("$@")
# The loop's pending sleep and an unfinished dump's temp file, for the stop trap.
SLEEP_PID=""
CURRENT_TMP=""

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
  for name in BACKUP_CHECK_EVERY_S BACKUP_RETRY_FIRST_S BACKUP_RETRY_MAX_S; do
    if ! [[ ${!name} =~ ^[0-9]{1,6}$ ]] || (( 10#${!name} < 1 )); then
      die "$name must be a whole number of seconds from 1 to 999999"
    fi
    printf -v "$name" '%d' "$(( 10#${!name} ))"
  done
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
  # "EPOCH NAME" of the newest dump at epoch $1, skipping dumps dated more than FUTURE_SLACK_S
  # after it (F-15); fails when there is none.
  local all=() i epoch
  mapfile -t all < <(dumps)
  for (( i = ${#all[@]} - 1; i >= 0; i-- )); do
    if epoch=$(name_epoch "${all[i]}") && (( epoch <= $1 + FUTURE_SLACK_S )); then
      printf '%s %s\n' "$epoch" "${all[i]##*/}"
      return 0
    fi
  done
  return 1
}

future_dump() {
  # The name of a dump dated more than FUTURE_SLACK_S after epoch $1 (a clock that was ahead).
  local path epoch
  while IFS= read -r path; do
    if epoch=$(name_epoch "$path") && (( epoch > $1 + FUTURE_SLACK_S )); then
      printf '%s\n' "${path##*/}"
      return 0
    fi
  done < <(dumps)
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
  if ! read -r newest_epoch _name < <(newest "$1"); then return 0; fi
  (( newest_epoch < $(last_slot "$1") ))
}

discard() {
  # A failed dump: remove its temp file ($1), keep every dump, log one error line ($2).
  rm -f -- "$1"
  CURRENT_TMP=""
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
  CURRENT_TMP=$tmp
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
  CURRENT_TMP=""
  rotate
  log "dump $name ok"
}

health() {
  # Healthy while the newest dump, by the time in its name, is under 26 h old at epoch $1, and
  # no dump is dated in the future (F-15: dumps go on; the file must be moved out by hand).
  local epoch name age
  if name=$(future_dump "$1"); then
    log "unhealthy: dump $name is dated in the future; move it out of the backup directory"
    return 1
  fi
  if ! read -r epoch name < <(newest "$1"); then
    log "unhealthy: no dump yet"
    return 1
  fi
  age=$(( $1 - epoch ))
  if (( age < HEALTH_MAX_AGE_S )); then
    log "healthy: newest dump $name is $(( age / 3600 )) h old"
    return 0
  fi
  log "unhealthy: newest dump $name is $(( age / 3600 )) h old (limit 26 h)"
  return 1
}

stop_loop() {
  # SIGTERM or SIGINT: drop an unfinished dump's temp file and exit at once (Pitfall 7).
  log "stopping"
  if [[ -n $SLEEP_PID ]]; then kill "$SLEEP_PID" 2>/dev/null; fi
  if [[ -n $CURRENT_TMP ]]; then rm -f -- "$CURRENT_TMP"; fi
  exit 0
}

run_loop() {
  # The container's command (D-09): check every BACKUP_CHECK_EVERY_S, dump when due.
  local failures=0 retry_at=0 wait_s=0 now path
  trap stop_loop TERM INT
  log "started: a dump every night at $BACKUP_TIME_UTC UTC, the newest $BACKUP_KEEP kept"
  # A dump cut off by a stop or a crash leaves its temp file behind.
  for path in "$BACKUP_DIR"/.powermon-*.dump.partial; do
    if rm -f -- "$path"; then log "removed leftover ${path##*/}"; fi
  done
  while true; do
    now=$(date -u +%s)
    if (( now >= retry_at )) && is_due "$now"; then
      if dump "$now"; then
        failures=0
        retry_at=0
      else
        # Paced retries, counted from the clock after the failed attempt: BACKUP_RETRY_FIRST_S,
        # doubling to at most BACKUP_RETRY_MAX_S, never once per check (no log spam).
        if (( failures == 0 )); then wait_s=$BACKUP_RETRY_FIRST_S; else wait_s=$(( wait_s * 2 )); fi
        if (( wait_s > BACKUP_RETRY_MAX_S )); then wait_s=$BACKUP_RETRY_MAX_S; fi
        failures=$(( failures + 1 ))
        retry_at=$(( $(date -u +%s) + wait_s ))
        log "next attempt in $wait_s s"
      fi
    fi
    # A background sleep, so the stop trap runs at once instead of after the sleep.
    sleep "$BACKUP_CHECK_EVERY_S" &
    SLEEP_PID=$!
    wait "$SLEEP_PID"
    SLEEP_PID=""
  done
}

restore() {
  # Restore the dump named $1 from the backup directory, only into an empty database (D-15).
  local name=$1 file tables
  file="$BACKUP_DIR/$name"
  if [[ -z $name ]]; then
    log "error: --restore needs the file name of a dump in the backup directory"
    return 1
  fi
  if [[ $name == */* ]]; then
    log "error: --restore takes a file name in the backup directory, not a path"
    return 1
  fi
  if [[ ! -f $file || -L $file ]]; then
    log "error: no dump file named $(printf '%q' "$name") in the backup directory"
    return 1
  fi
  if ! pg_restore --list "$file" > /dev/null; then
    log "error: $(printf '%q' "$name") is not a valid dump (pg_restore --list failed)"
    return 1
  fi
  if ! tables=$(psql -X -At -c "$TABLE_COUNT_SQL"); then
    log "error: cannot count the tables of the target database (psql failed)"
    return 1
  fi
  if [[ $tables != 0 ]]; then
    log "error: refused: the database already has tables; --restore restores only into a new, empty database"
    return 1
  fi
  log "restoring $name into the empty database"
  if ! pg_restore --no-owner --single-transaction --exit-on-error -d "$PGDATABASE" "$file"; then
    log "error: restore of $name failed (pg_restore); nothing was restored"
    return 1
  fi
  log "restore of $name ok"
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

parse_now() {
  # [--now EPOCH]: the clock, or an injected epoch for tests and checks.
  NOW=$(date -u +%s)
  if (( $# == 0 )); then return 0; fi
  if [[ $# != 2 || $1 != --now || ! $2 =~ ^[0-9]{1,12}$ ]]; then
    die "--now needs a whole number of seconds since 1970-01-01 UTC"
  fi
  NOW=$(( 10#$2 ))
}

# Usage errors exit 2 before anything is written.
MODE=""
if (( $# > 0 )); then
  MODE=$1
  shift
  case $MODE in
    --once | --health)
      parse_now "$@"
      ;;
    --dump-now)
      if (( $# > 0 )); then die "--dump-now takes no argument"; fi
      ;;
    --restore)
      if (( $# != 1 )); then die "--restore needs exactly one dump file name"; fi
      RESTORE_NAME=$1
      ;;
    *)
      die "unknown mode $(printf '%q' "$MODE"); use no argument (the loop), --once, --health, --dump-now or --restore NAME"
      ;;
  esac
fi

if [[ $MODE == --health ]]; then
  # Before the settings check and the root branch: read-only, file names only.
  health "$NOW"
  exit $?
fi

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
  "")
    run_loop
    ;;
  --once)
    if ! is_due "$NOW"; then
      log "no dump due: the newest dump is from after the last $BACKUP_TIME_UTC UTC slot"
      exit 0
    fi
    dump "$NOW" || exit 1
    ;;
  --dump-now)
    dump "$(date -u +%s)" || exit 1
    ;;
  --restore)
    restore "$RESTORE_NAME" || exit 1
    ;;
esac
exit 0
