#!/bin/bash
# opencode-permission-setup.sh
# Configure OpenCode permissions for Ralph Wiggum autonomous loops

set -e

CONFIG_PATH="$HOME/.config/opencode/opencode.json"
BACKUP_PATH="$HOME/.config/opencode/opencode.json.backup"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "OpenCode Permission Setup for Ralph Wiggum"
echo "=========================================="

# Backup current config
if [ -f "$CONFIG_PATH" ]; then
    echo "✓ Backing up current config to: $BACKUP_PATH.$TIMESTAMP"
    cp "$CONFIG_PATH" "$BACKUP_PATH.$TIMESTAMP"
else
    echo "✓ No existing config found, creating new"
fi

# Menu
echo ""
echo "Select permission strategy:"
echo "  1) Full Allow (YOLO mode - allows everything)"
echo "  2) Ralph-optimized (allows bash/edit/read, asks for web/search)"
echo "  3) Safe Ralph (asks for most, allows git/pytest/npm)"
echo "  4) Restore from backup"
echo "  5) Exit"
echo ""
read -p "Choice [1-5]: " choice

case $choice in
    1)
        cat > "$CONFIG_PATH" << 'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "permission": "allow"
}
EOF
        echo "✓ Configured: FULL ALLOW (YOLO mode)"
        echo "  All operations will run without approval"
        ;;

    2)
        cat > "$CONFIG_PATH" << 'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "*": "ask",
    "bash": "allow",
    "edit": "allow",
    "read": "allow",
    "task": "allow"
  }
}
EOF
        echo "✓ Configured: Ralph-optimized"
        echo "  bash/edit/read/task: ALLOW (autonomous)"
        echo "  web/search: ASK (verification)"
        ;;

    3)
        cat > "$CONFIG_PATH" << 'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "*": "ask",
    "bash": {
      "*": "ask",
      "git *": "allow",
      "pytest": "allow",
      "make": "allow",
      "npm *": "allow",
      "uv *": "allow"
    },
    "edit": {
      "*": "ask",
      "*.py": "allow",
      "*.md": "allow",
      "*.txt": "allow",
      "*.json": "allow"
    },
    "read": {
      "*": "allow"
    }
  }
}
EOF
        echo "✓ Configured: Safe Ralph"
        echo "  git/npm/make/uv/pytest: ALLOW"
        echo "  edit *.py/md/txt/json: ALLOW"
        echo "  Everything else: ASK"
        ;;

    4)
        echo "Available backups:"
        ls -lt "$BACKUP_PATH".* 2>/dev/null | awk '{print NR ". " $NF}' || echo "  No backups found"
        read -p "Restore which file (or press Enter to cancel): " backup_file
        if [ -n "$backup_file" ] && [ -f "$backup_file" ]; then
            cp "$backup_file" "$CONFIG_PATH"
            echo "✓ Restored: $backup_file"
        else
            echo "✗ Restore cancelled"
        fi
        ;;

    5)
        echo "Exiting without changes"
        exit 0
        ;;

    *)
        echo "✗ Invalid choice"
        exit 1
        ;;
esac

echo ""
echo "Current config:"
echo "=========================================="
cat "$CONFIG_PATH"
echo "=========================================="
echo ""
echo "✓ Restart OpenCode to apply changes"
echo "  TUI mode: opencode"
echo "  Server mode: opencode serve"
