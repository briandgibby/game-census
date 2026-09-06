#!/bin/sh
# Collect the two clients and their loader/libraries from the pinned PG image.
# They remain private to these clients; Python's system libraries are untouched.
set -eu
target=/opt/game-census-postgres
mkdir -p "$target/bin" "$target/lib"
for name in pg_dump pg_restore; do
    binary="/usr/lib/postgresql/18/bin/$name"
    cp "$binary" "$target/bin/$name"
    dependencies="$(ldd "$binary")"
    printf '%s\n' "$dependencies" | awk '{for(i=1;i<=NF;i++) if($i ~ /^\//) print $i}' > "$target/dependencies-$name.txt"
    while IFS= read -r dependency; do
        test -f "$dependency"
        cp -Ln "$dependency" "$target/lib/"
    done < "$target/dependencies-$name.txt"
done
loader="$(printf '%s\n' "$dependencies" | awk '$1 ~ /^\/.*ld-linux/ {print $1}')"
test -n "$loader"
basename "$loader" > "$target/loader.txt"
