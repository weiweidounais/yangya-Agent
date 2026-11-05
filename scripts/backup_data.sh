#!/usr/bin/env bash
# 在线备份本地 SQLite 数据（不锁库、不影响服务），并保留最近 N 份。
#
# 用法：
#   bash scripts/backup_data.sh
#   DATA_DIR=/path/to/data BACKUP_DIR=/path/to/backups BACKUP_KEEP=30 bash scripts/backup_data.sh
#
# 恢复：见 docs/ops.md「恢复」。备份包含 .harness_id_key（轨迹假名化用），
# 请把备份目录当成敏感数据保管，不要提交到仓库或公开网盘。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$ROOT/data}"
BACKUP_DIR="${BACKUP_DIR:-$DATA_DIR/backups}"
KEEP="${BACKUP_KEEP:-14}"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "需要 sqlite3：macOS 自带；没有就 brew install sqlite" >&2
  exit 2
fi
shopt -s nullglob
DBS=("$DATA_DIR"/*.db)
if [ "${#DBS[@]}" -eq 0 ]; then
  echo "没有找到数据库：$DATA_DIR/*.db（DATA_DIR 可能不对）" >&2
  exit 3
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date '+%Y%m%d-%H%M%S')"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

for db in "${DBS[@]}"; do
  name="$(basename "$db" .db)"
  # .backup 是 SQLite 官方的在线备份，不会读到写了一半的页。
  sqlite3 "$db" ".backup '$STAGE/$name.db'"
done
[ -f "$DATA_DIR/.harness_id_key" ] && cp "$DATA_DIR/.harness_id_key" "$STAGE/"

ARCHIVE="$BACKUP_DIR/yangya-data-$STAMP.tar.gz"
tar -czf "$ARCHIVE" -C "$STAGE" .

# 只保留最近 KEEP 份，避免占满磁盘（兼容 macOS 自带 bash 3.2，不用 mapfile）。
COUNT="$(ls -1 "$BACKUP_DIR"/yangya-data-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')"
if [ "${COUNT:-0}" -gt "$KEEP" ]; then
  ls -1t "$BACKUP_DIR"/yangya-data-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) \
    | while IFS= read -r old; do [ -n "$old" ] && rm -f -- "$old"; done
fi

echo "✓ 备份完成：$ARCHIVE"
echo "  大小：$(du -h "$ARCHIVE" | awk '{print $1}')"
echo "  保留：最近 $KEEP 份（当前 $(ls -1 "$BACKUP_DIR"/yangya-data-*.tar.gz 2>/dev/null | wc -l | tr -d ' ') 份）"
echo "  提醒：备份含 .harness_id_key，按敏感数据保管；恢复步骤见 docs/ops.md。"
