#!/bin/bash
# Wrapper para rodar o TTS Reader sem precisar ativar o venv manualmente.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/venv/bin/python" "$DIR/main.py" "$@"
