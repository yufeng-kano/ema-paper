#!/bin/sh
# The release, run by hand in the server checkout (docs/deployment.md § Release flow):
#
#   deploy/deploy.sh          deploy origin/main's head
#   deploy/deploy.sh <sha>    the same, only when <sha> is origin/main's head
#
# Seven steps, each named as it starts: preflight, a database dump, a fast-forward merge,
# the build, in-flight work, the swap and the health checks. The first step that fails
# stops the deploy and prints the commands that roll it back; nothing is restored
# automatically. A deploy that stopped after its merge is finished by running this again.
#
# Settings come from the shell, never from .env (defaults in parentheses):
#   DEPLOY_COMPOSE_FILES    compose files, colon-separated (docker-compose.production.yaml)
#   DEPLOY_COMPOSE_PROJECT  compose project name (empty: compose's own choice, as by hand)
#   DEPLOY_DRAIN_SECONDS    how long to wait for running parses and chat turns (300)
#   DEPLOY_HEALTH_SECONDS   how long each health check may take (300)
#   DEPLOY_DUMPS_KEEP       how many pre-deploy dumps backups/ keeps (5)
#
# Of .env it prints DOMAIN alone: keys are read one line at a time, never by sourcing
# the file, and compose is checked with `config --quiet`, since `config` prints every
# secret of .env.

set -eu

# The whole deploy is this one function, called on the last line: the shell has read all
# of it before the first step, so the merge below may rewrite this file without changing
# the deploy that is running.
main() {
  if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && ! printf '%s\n' "$1" | grep -Eqx '[0-9a-f]{7,40}'; }; then
    echo "usage: deploy/deploy.sh [<sha>]   (a commit id: 7 to 40 lowercase hex characters)" >&2
    exit 2
  fi
  wanted=${1:-}

  compose_files=${DEPLOY_COMPOSE_FILES:-docker-compose.production.yaml}
  compose_project=${DEPLOY_COMPOSE_PROJECT:-}
  drain_seconds=${DEPLOY_DRAIN_SECONDS:-300}
  health_seconds=${DEPLOY_HEALTH_SECONDS:-300}
  dumps_keep=${DEPLOY_DUMPS_KEEP:-5}
  for value in "$drain_seconds" "$health_seconds" "$dumps_keep"; do
    case $value in
      '' | *[!0-9]*)
        echo "DEPLOY_DRAIN_SECONDS, DEPLOY_HEALTH_SECONDS and DEPLOY_DUMPS_KEEP are whole numbers" >&2
        exit 2
        ;;
    esac
  done
  if [ "$dumps_keep" -lt 1 ]; then
    echo "DEPLOY_DUMPS_KEEP is at least 1: this deploy's own dump is kept" >&2
    exit 2
  fi

  # Compose reads these itself, so every `docker compose` below is plain. Set here, they
  # replace whatever the shell exported, so another stack's settings never steer a deploy.
  export COMPOSE_FILE="$compose_files" COMPOSE_PATH_SEPARATOR=:
  if [ -n "$compose_project" ]; then
    export COMPOSE_PROJECT_NAME="$compose_project"
  else
    unset COMPOSE_PROJECT_NAME
  fi
  # The same, spelled out for the commands a failure prints. Those run in the operator's
  # shell: with no project named, they clear COMPOSE_PROJECT_NAME as this script did, or
  # one exported there for another stack would take them, `dropdb` included, to it.
  if [ -n "$compose_project" ]; then
    compose_cmd="docker compose"
  else
    compose_cmd="env -u COMPOSE_PROJECT_NAME docker compose"
  fi
  old_ifs=$IFS
  IFS=:
  for file in $compose_files; do
    compose_cmd="$compose_cmd -f $file"
  done
  IFS=$old_ifs
  if [ -n "$compose_project" ]; then
    compose_cmd="$compose_cmd -p $compose_project"
  fi

  # The checkout this script lives in, wherever it is run from. A copy outside any
  # checkout works in the working directory's: the deploy that brings this script runs
  # origin/main's copy that way (docs/deployment.md § Release flow).
  if ! checkout=$(cd "$(dirname -- "$0")/.." 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null); then
    checkout=$(git rev-parse --show-toplevel)
  fi
  cd "$checkout"
  # Compose would read a COMPOSE_FILE line of .env, but the export above replaces it: a
  # host that keeps its files there (a front proxy's overlay, say) would lose them here.
  if [ -z "${DEPLOY_COMPOSE_FILES:-}" ] && [ -f .env ] &&
    grep -Eq '^[[:space:]]*(export[[:space:]]+)?COMPOSE_FILE[[:space:]]*=' .env; then
    echo ".env sets COMPOSE_FILE, which this script replaces with DEPLOY_COMPOSE_FILES" >&2
    echo "(docker-compose.production.yaml when unset): set DEPLOY_COMPOSE_FILES to the same files" >&2
    exit 2
  fi
  # Written before the merge, deleted once the health checks pass: while it stands, the
  # deploy it names has not finished, and the next run finishes it (preflight below).
  unfinished="$(git rev-parse --git-dir)/sightread-deploy-unfinished"

  # What the failure report reads: how far the deploy got (before: nothing changed;
  # merged: the checkout moved, the containers did not; swapped: `up -d` ran, or may have
  # in the unfinished deploy this run finishes) and what the deploy brings.
  stage=before
  old=''
  dump=''
  old_revision=''
  applied=''
  migrations_changed=0
  caddy_changed=0
  sandbox_changed=0
  sandbox_built=0
  step='1/7 preflight'
  trap 'report_failure "$?"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  # A dropped SSH connection: without this, dash dies on the hangup and never reports.
  trap 'exit 129' HUP

  echo "== $step"
  if [ -f "$unfinished" ]; then
    # The merge has moved HEAD already: the deploy counts from where it started, and its
    # way back is the one it noted. Read first, so that whatever fails from here prints it.
    {
      read -r old
      read -r old_revision
      read -r dump
    } <"$unfinished" || true
    if ! git merge-base --is-ancestor "$old" HEAD 2>/dev/null; then
      echo "$unfinished names a deploy from '$old' that did not finish, and HEAD is not that" >&2
      echo "commit or past it: the checkout was moved by hand. Once the containers run HEAD," >&2
      echo "delete that file and run this again." >&2
      exit 1
    fi
    echo "a deploy from $old did not finish: this run finishes it (its note: $unfinished)"
    # It may have swapped, so a failure prints the whole way back for what it brought,
    # the VM image included, built or not: building the old one again is harmless.
    stage=swapped
    applied=$(git diff --name-only "$old" HEAD)
    if printf '%s\n' "$applied" | grep -q '^apps/api/migrations/'; then
      migrations_changed=1
    fi
    if printf '%s\n' "$applied" | grep -qxF 'deploy/caddy/Caddyfile'; then
      caddy_changed=1
    fi
    if printf '%s\n' "$applied" | grep -q '^deploy/sandbox-image/'; then
      sandbox_built=1
    fi
  fi
  if [ -n "$(git status --porcelain)" ]; then
    echo "the working tree is not clean; a deploy starts from a clean checkout:" >&2
    git status --short >&2
    exit 1
  fi
  if [ ! -f .env ]; then
    echo ".env is missing (docs/self-hosting.md § Run it on a server)" >&2
    exit 1
  fi
  domain=$(sed -n 's/^DOMAIN=//p' .env | tail -n 1 | tr -d "\"' \r")
  domain=${domain%%#*}
  case $domain in
    '' | *[!A-Za-z0-9.-]*)
      echo ".env names no usable DOMAIN" >&2
      exit 1
      ;;
  esac
  mkdir -p backups
  if [ ! -w backups ]; then
    echo "backups/ is not writable by $(id -un), so the pre-deploy dump cannot be written." >&2
    echo "Docker created it as root with the backup service: sudo chown $(id -un) backups, once." >&2
    exit 1
  fi
  docker compose config --quiet
  git fetch --quiet origin
  target=$(git rev-parse --verify 'origin/main^{commit}')
  if [ -n "$wanted" ] && [ "$(git rev-parse --verify --quiet "$wanted^{commit}")" != "$target" ]; then
    echo "$wanted is not origin/main's head ($target); refusing" >&2
    exit 1
  fi
  if [ -z "$old" ]; then
    old=$(git rev-parse HEAD)
    if [ "$old" = "$target" ]; then
      echo "HEAD is already origin/main's head ($target): nothing to deploy"
      exit 0
    fi
  fi
  if ! git merge-base --is-ancestor HEAD "$target"; then
    echo "origin/main ($target) is not a fast-forward of HEAD ($(git rev-parse HEAD)): HEAD has" >&2
    echo "commits origin/main lacks, or origin/main was rewritten. Only a fast-forward deploys." >&2
    exit 1
  fi
  echo "deploying $old -> $target to https://$domain"
  # Of an unfinished deploy, what it applied (old to HEAD) counts with what is left (HEAD to
  # the target): a target that reverts it must still take back its schema, the Caddyfile
  # caddy mounted and the VM image. On a fresh run HEAD is old, and this is old to target.
  remaining=$(git diff --name-only HEAD "$target")
  changed=$(printf '%s\n%s\n' "$applied" "$remaining" | sed '/^$/d' | sort -u)
  echo "changed paths:"
  printf '%s\n' "$changed" | sed 's/^/  /'
  if printf '%s\n' "$changed" | grep -q '^apps/api/migrations/'; then
    migrations_changed=1
    echo "this deploy migrates the database (apps/api/migrations/ changed)"
  fi
  if printf '%s\n' "$changed" | grep -qxF 'deploy/caddy/Caddyfile'; then
    caddy_changed=1
  fi
  if printf '%s\n' "$changed" | grep -q '^deploy/sandbox-image/'; then
    sandbox_changed=1
  fi
  if printf '%s\n' "$changed" | grep -qxF '.env.production.example'; then
    echo "note: .env.production.example changed; compare it with .env for new settings"
  fi

  step='2/7 database dump'
  echo "== $step"
  # shellcheck disable=SC2016 # $POSTGRES_USER and $POSTGRES_DB expand inside the pg container
  revision=$(docker compose exec -T pg sh -c 'psql -X -q -tA -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version"')
  if [ -n "$old_revision" ] && [ "$revision" != "$old_revision" ]; then
    # The unfinished deploy migrated the database: a dump taken now would hold the new
    # schema, so the one it took before stays the way back, and the way back takes the
    # schema back whatever the paths say (a target that reverts the migration among them).
    migrations_changed=1
    if [ -f "$dump" ]; then
      echo "the database moved to revision $revision in the unfinished deploy; the way back stays"
      echo "$dump, taken at revision $old_revision"
    else
      # The daily backup pass deletes pre-deploy dumps seven days old.
      echo "warning: the database moved to revision $revision in the unfinished deploy, and its"
      echo "dump, $dump, is gone: no dump of revision $old_revision is left to restore"
    fi
  else
    new_dump="backups/predeploy-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 "$old").dump"
    # Readable by its owner alone (every session, key hash and encrypted credential is in
    # it), and written under a temporary name renamed once complete, so a dump that exists
    # is whole. The umask stays in the subshell: files the merge writes keep the usual one.
    # shellcheck disable=SC2016 # $POSTGRES_USER and $POSTGRES_DB expand inside the pg container
    if ! (umask 077 && docker compose exec -T pg sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' >"$new_dump.tmp"); then
      rm -f "$new_dump.tmp"
      exit 1
    fi
    mv "$new_dump.tmp" "$new_dump"
    dump=$new_dump
    old_revision=$revision
    echo "wrote $dump ($(du -h "$dump" | cut -f1)); the database is at revision $old_revision"
    # The newest DEPLOY_DUMPS_KEEP are kept; the names sort by their stamp. The daily backup
    # pass also deletes any seven days old (docker-compose.production.yaml).
    # shellcheck disable=SC2012 # the names are this script's own: predeploy-<stamp>-<sha>.dump
    ls -1r backups/predeploy-*.dump | tail -n +"$((dumps_keep + 1))" | while read -r stale; do
      rm -f "$stale"
      echo "deleted $stale"
    done
  fi

  step='3/7 merge'
  echo "== $step"
  printf '%s\n%s\n%s\n' "$old" "$old_revision" "$dump" >"$unfinished"
  # An unfinished deploy this run finishes stays `swapped`.
  if [ "$stage" = before ]; then
    stage=merged
  fi
  git merge --ff-only --quiet "$target"
  echo "HEAD is $target"

  step='4/7 build'
  echo "== $step"
  # The new compose file may want a variable .env lacks.
  docker compose config --quiet
  docker compose build
  if [ "$sandbox_changed" = 1 ]; then
    # Compose does not build the VM image; it is tagged as .env's SANDBOX_IMAGE names it,
    # or as the runner's default when .env names none. Quiet: the tag is a value of .env.
    sandbox_image=$(sed -n 's/^SANDBOX_IMAGE=//p' .env | tail -n 1 | tr -d "\"' \r")
    sandbox_image=${sandbox_image%%#*}
    echo "deploy/sandbox-image changed: building the VM image (output hidden; this takes minutes)"
    sandbox_built=1
    docker build --quiet --tag "${sandbox_image:-sightread-sandbox}" deploy/sandbox-image >/dev/null
  fi

  step='5/7 in-flight work'
  echo "== $step"
  # Recreating the worker requeues a running parse, keeping its finished pages, and fails a
  # running chat turn with "worker stopped" (docs/jobs.md § Chat turns): wait for both.
  # When the deploy migrates, a database dump running (the daily backup pass's, say) is
  # waited for too: its lock on every table would keep the migration's ALTER TABLE waiting
  # past the api's healthcheck.
  # Counted right before the swap, so work that started during the build counts too.
  # tests/test_deploy_script.py runs this query.
  deadline=$(($(date +%s) + drain_seconds))
  while :; do
    # shellcheck disable=SC2016 # $POSTGRES_USER and $POSTGRES_DB expand inside the pg container
    counts=$(docker compose exec -T pg sh -c 'psql -X -q -tA -F " " -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'IN_FLIGHT_SQL'
SELECT (SELECT count(*) FROM jobs WHERE status = 'running'),
       (SELECT count(*) FROM chat_turns WHERE status <> 'queued' AND finished_at IS NULL),
       (SELECT count(*) FROM pg_stat_activity
         WHERE application_name = 'pg_dump' AND datname = current_database());
IN_FLIGHT_SQL
    )
    read -r running_jobs running_turns running_dumps <<EOF
$counts
EOF
    if [ "$migrations_changed" = 0 ]; then
      running_dumps=0
    fi
    if [ "$running_jobs" = 0 ] && [ "$running_turns" = 0 ] && [ "$running_dumps" = 0 ]; then
      echo "no parse or chat turn running, and no database dump holding back a migration"
      break
    fi
    echo "running: $running_jobs parse(s), $running_turns chat turn(s), $running_dumps database dump(s)"
    if [ "$(date +%s)" -ge "$deadline" ]; then
      if [ -t 0 ]; then
        printf 'Still running after %ss. Deploy anyway? A running parse goes back to the queue; a running chat turn fails; a running dump may keep the migration waiting past the api healthcheck. [y/N] ' "$drain_seconds"
        read -r answer || answer=''
        case $answer in
          y | Y | yes) break ;;
        esac
      fi
      echo "stopped: work still running after ${drain_seconds}s (DEPLOY_DRAIN_SECONDS)" >&2
      exit 1
    fi
    sleep 10
  done

  step='6/7 swap'
  echo "== $step"
  # The api migrates as it starts; the worker waits for it to be healthy. Every service
  # but backup: a recreated backup starts a pass at once, and its pg_dump's lock on every
  # table would keep the migration waiting. It follows the migration in step 7.
  services=$(docker compose config --services | grep -vx backup)
  stage=swapped
  # shellcheck disable=SC2086 # one word per service name
  docker compose up -d $services
  if [ "$caddy_changed" = 1 ]; then
    # A single bind-mounted file: only a restart mounts the new one. Open connections drop.
    echo "deploy/caddy/Caddyfile changed: restarting caddy"
    docker compose restart caddy
  fi

  step='7/7 health'
  echo "== $step"
  # Nothing here sends a parse, a chat turn, a model catalog fetch or a Pay call.
  deadline=$(($(date +%s) + health_seconds))
  until [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q api)" 2>/dev/null)" = healthy ]; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "api is not healthy after ${health_seconds}s" >&2
      exit 1
    fi
    sleep 5
  done
  echo "api healthy"

  current=$(docker compose exec -T api alembic current 2>/dev/null | awk '{print $1}' | sort)
  heads=$(docker compose exec -T api alembic heads 2>/dev/null | awk '{print $1}' | sort)
  if [ -z "$heads" ] || [ "$current" != "$heads" ]; then
    echo "the database is at revision '$current' but the code's heads are '$heads'" >&2
    exit 1
  fi
  echo "database at the head revision ($current)"
  # The migration is through: backup may start its pass now.
  docker compose up -d backup

  deadline=$(($(date +%s) + health_seconds))
  until [ "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "https://$domain/healthz" 2>/dev/null)" = 200 ]; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "https://$domain/healthz did not answer 200 within ${health_seconds}s" >&2
      exit 1
    fi
    sleep 5
  done
  echo "https://$domain/healthz answers 200"

  # The worker holds the worker lock from before its first claim for as long as it lives
  # (docs/jobs.md § Queue), whenever it started and whatever its logs have rotated away.
  # Read from pg_locks, never taken: an advisory lock on one bigint shows its high 32 bits
  # as classid and its low 32 bits as objid. 7312024001 is WORKER_LOCK_KEY
  # (apps/api/src/sightread/jobs/queue.py); tests/test_deploy_script.py runs this query.
  deadline=$(($(date +%s) + health_seconds))
  while :; do
    running=$(docker inspect -f '{{.State.Running}}' "$(docker compose ps -q worker)" 2>/dev/null || true)
    # shellcheck disable=SC2016 # $POSTGRES_USER and $POSTGRES_DB expand inside the pg container
    held=$(docker compose exec -T pg sh -c 'psql -X -q -tA -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'WORKER_LOCK_SQL'
SELECT count(*) FROM pg_locks
 WHERE locktype = 'advisory' AND granted AND objsubid = 1
   AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
   AND (classid::bigint << 32) + objid::bigint = 7312024001;
WORKER_LOCK_SQL
    ) || held=''
    if [ "$running" = true ] && [ "$held" = 1 ]; then
      break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "the worker is not running with the worker lock held after ${health_seconds}s" >&2
      exit 1
    fi
    sleep 5
  done
  echo "worker running, holding the worker lock"

  deadline=$(($(date +%s) + health_seconds))
  until [ "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "https://$domain/" 2>/dev/null)" = 200 ]; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "https://$domain/ (the web app) did not answer 200 within ${health_seconds}s" >&2
      exit 1
    fi
    sleep 5
  done
  echo "https://$domain/ answers 200"

  rm -f "$unfinished"
  echo
  echo "deployed $target (was $old); pre-deploy dump: $dump"
}

# The EXIT trap. On a failure: the step that failed, and the way back from what the deploy
# had reached. It prints commands and runs none of them.
# shellcheck disable=SC2329 # called by the EXIT trap main sets, which shellcheck cannot see
report_failure() {
  if [ "$1" -eq 0 ]; then
    return 0
  fi
  echo
  echo "!! deploy failed at step $step (exit $1)"
  if [ "$stage" = before ]; then
    echo "Nothing was changed: the checkout and the containers are as they were."
    return 0
  fi
  echo "Nothing is restored automatically. To look first:"
  echo "  $compose_cmd ps"
  echo "  $compose_cmd logs --tail=100 api worker"
  if [ "$stage" = merged ]; then
    echo "No container was touched: they still run $old."
  fi
  echo "To finish the deploy once the cause is fixed, run deploy/deploy.sh again."
  echo "To roll back to $old instead, in $(pwd):"
  if [ "$stage" = swapped ] && [ "$migrations_changed" = 1 ]; then
    cat <<EOF
  This deploy brought migrations, and old code cannot start on a newer schema. If the
  database is past revision $old_revision, where it was before
  ($compose_cmd run --rm --no-deps api alembic current), stop the new code and take
  it back with the new image's downgrade (some downgrades refuse by design):
    $compose_cmd stop api worker
    $compose_cmd run --rm --no-deps api alembic downgrade $old_revision
EOF
    if [ -f "$dump" ]; then
      cat <<EOF
  OR instead restore the pre-deploy dump into an empty database, which loses every
  write made since it was taken:
    $compose_cmd stop api worker backup
    $compose_cmd exec -T pg sh -c 'dropdb --force -U "\$POSTGRES_USER" "\$POSTGRES_DB" && createdb -U "\$POSTGRES_USER" "\$POSTGRES_DB"'
    $compose_cmd exec -T pg sh -c 'pg_restore -U "\$POSTGRES_USER" -d "\$POSTGRES_DB"' < $dump
EOF
    else
      cat <<EOF
  The pre-deploy dump, $dump, is gone (the daily backup pass deletes them
  at seven days): the downgrade is the way back, or a daily stamp in backups/ from
  before the deploy (docs/deployment.md § Compose files).
EOF
    fi
    echo "  Then:"
  fi
  echo "  git reset --keep $old"
  # The build may have tagged images with the new code, which a later `up` would start;
  # building the old code again puts the tags back without recreating what runs it.
  echo "  $compose_cmd up -d --build"
  if [ "$stage" = swapped ] && [ "$caddy_changed" = 1 ]; then
    echo "  $compose_cmd restart caddy"
  fi
  if [ "$sandbox_built" = 1 ]; then
    echo "  docker build -t <the SANDBOX_IMAGE of .env> deploy/sandbox-image"
  fi
  if [ -f "$dump" ]; then
    echo "The pre-deploy dump: $dump"
  else
    echo "The pre-deploy dump, $dump, is gone."
  fi
} >&2

main "$@"
exit
