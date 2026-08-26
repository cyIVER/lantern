#!/usr/bin/env bash
# Create the WeaponPaints database and a user scoped to it.
#
# Deliberately NOT done through Pelican's database-host feature: that requires
# the host user to hold CREATE USER + GRANT globally, i.e. superuser on the
# instance. This creates one database and one user whose rights end at that
# database, which is all the plugin needs.
#
# The MariaDB root password is read inside the container from its own
# environment and is never printed or passed as an argument.
#
# The generated plugin password is written to stack/.weaponpaints-db (gitignored).
set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

DB=cs2_weaponpaints
USER=weaponpaints
OUT=.weaponpaints-db

if [ -f "$OUT" ]; then
  echo "$OUT already exists -- reusing the existing credentials"
else
  PW=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  umask 077
  {
    echo "DB_HOST=database"
    echo "DB_PORT=3306"
    echo "DB_NAME=$DB"
    echo "DB_USER=$USER"
    echo "DB_PASS=$PW"
  } > "$OUT"
  echo "wrote $OUT (mode 600, gitignored)"
fi

# shellcheck disable=SC1090
. "./$OUT"

# No backticks around identifiers: the heredoc is unquoted so $P expands, which
# means backticks would be treated as command substitution by the shell.
docker compose exec -T -e P="$DB_PASS" -e D="$DB" -e U="$USER" database sh -c '
  mysql -uroot -p"$MYSQL_ROOT_PASSWORD" <<SQL
CREATE DATABASE IF NOT EXISTS $D CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS "$U"@"%" IDENTIFIED BY "$P";
ALTER USER "$U"@"%" IDENTIFIED BY "$P";
GRANT ALL PRIVILEGES ON $D.* TO "$U"@"%";
FLUSH PRIVILEGES;
SQL
'

echo "verifying the scoped user can reach only its own database:"
docker compose exec -T -e P="$DB_PASS" database sh -c '
  mysql -u'"$USER"' -p"$P" -N -e "SHOW DATABASES;"
' | sed 's/^/  /'
