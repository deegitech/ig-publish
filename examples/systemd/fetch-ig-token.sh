#!/bin/sh
# Fetch the Instagram access token from AWS SSM Parameter Store into a tmpfs file that only the
# container user can read. Run by ig-publish-token.service (see docs/deploy-ec2.md).
# The token goes from the aws CLI's stdout straight into the file: never into argv, the environment or a log.
set -eu

PARAM="${IG_TOKEN_PARAM:?set IG_TOKEN_PARAM, e.g. /example/ig-publish/access-token}"
DIR="${IG_TOKEN_DIR:-/run/ig-publish}"     # /run is tmpfs on systemd hosts: the token never touches the disk
OWNER="${IG_TOKEN_OWNER:-10001:10001}"     # uid:gid the container runs as

umask 077
# The directory stays root-owned (0755): the container user can reach the file but cannot create, rename or swap
# anything in it, so the chown/chmod/mv below cannot be redirected. Only the token file belongs to that user.
install -d -m 0755 -o root -g root "$DIR"
tmp="$(mktemp "$DIR/.token.XXXXXX")"
trap 'rm -f "$tmp"' EXIT INT TERM

aws ssm get-parameter --name "$PARAM" --with-decryption \
    --query Parameter.Value --output text > "$tmp"
if [ ! -s "$tmp" ]; then
    echo "fetch-ig-token: $PARAM returned an empty value" >&2
    exit 1
fi
chown "$OWNER" "$tmp"
chmod 0600 "$tmp"
mv -f "$tmp" "$DIR/token"
trap - EXIT INT TERM
echo "fetch-ig-token: refreshed $DIR/token from $PARAM"
