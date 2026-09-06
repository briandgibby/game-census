#!/bin/sh
set -eu
IFS= read -r loader < /opt/game-census-postgres/loader.txt
client="${0##*/}"
exec "/opt/game-census-postgres/lib/$loader" --library-path /opt/game-census-postgres/lib "/opt/game-census-postgres/bin/$client" "$@"
