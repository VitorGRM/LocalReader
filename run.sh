#!/usr/bin/env bash
# LocalReader no Linux.
#
# Na primeira vez cria o ambiente Python (venv) e instala as dependências;
# nas próximas, só abre o app (e reinstala sozinho se o requirements.txt mudar,
# por exemplo depois de um "git pull").
#
#   ./run.sh                  abre o app
#   ./run.sh --install-qwen   instala também a voz Qwen3-TTS (PyTorch com CUDA, vários GB)
#   ./run.sh --desktop        cria um atalho no menu de aplicativos
#   ./run.sh --check          só confere o sistema (Python, Tesseract, bibliotecas)
#   ./run.sh --reinstall      apaga o ambiente e instala tudo de novo
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${LOCALREADER_VENV:-$DIR/venv}"
# Pasta copiada do Windows: o venv de lá não serve no Linux.
if [ -d "$VENV/Scripts" ] && [ ! -x "$VENV/bin/python" ]; then
    VENV="$DIR/.venv-linux"
fi
PY="$VENV/bin/python"
STAMP="$VENV/.requirements.sha256"

say() { printf '\033[1m[LocalReader]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[LocalReader] aviso:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[LocalReader] erro:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- Python ----------
find_python() {
    local candidate
    for candidate in python3.12 python3.13 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

create_venv() {
    local python
    python="$(find_python)" || die "Python 3.10 ou mais novo não encontrado. Instale com: sudo apt install python3 python3-venv (Fedora: sudo dnf install python3)"
    say "Criando o ambiente Python em $VENV (com $("$python" --version))..."
    "$python" -m venv "$VENV" ||
        die "não foi possível criar o venv. No Debian/Ubuntu instale: sudo apt install python3-venv"
}

requirements_hash() {
    sha256sum "$DIR/requirements.txt" | cut -d' ' -f1
}

install_requirements() {
    say "Instalando as dependências (só na primeira vez ou quando o requirements.txt muda)..."
    "$PY" -m pip install --upgrade pip >/dev/null
    "$PY" -m pip install -r "$DIR/requirements.txt"
    requirements_hash >"$STAMP"
}

ensure_environment() {
    [ -x "$PY" ] || create_venv
    if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$(requirements_hash)" ]; then
        install_requirements
    fi
}

# ---------- Sistema ----------
has_library() {
    ldconfig -p 2>/dev/null | grep -q "$1"
}

check_system() {
    local ok=1
    if ! command -v tesseract >/dev/null 2>&1; then
        ok=0
        warn "Tesseract não encontrado: o OCR (PDFs digitalizados) não vai funcionar."
        warn "  Debian/Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-por"
        warn "  Fedora:        sudo dnf install tesseract tesseract-langpack-por"
    elif ! tesseract --list-langs 2>/dev/null | grep -qx por; then
        warn "Tesseract sem o idioma português: sudo apt install tesseract-ocr-por (Fedora: tesseract-langpack-por)"
    fi
    # O Qt 6 precisa desta biblioteca para abrir janelas no X11 (e no XWayland).
    if ! has_library libxcb-cursor.so.0; then
        ok=0
        warn "Biblioteca libxcb-cursor não encontrada: a janela pode não abrir."
        warn "  Debian/Ubuntu: sudo apt install libxcb-cursor0   Fedora: sudo dnf install xcb-util-cursor"
    fi
    if ! has_library libpulse.so.0 && ! command -v pipewire >/dev/null 2>&1; then
        warn "Nem PulseAudio nem PipeWire encontrados: pode não sair som."
    fi
    [ "$ok" = 1 ] && say "Sistema ok."
    return 0
}

# ---------- Opções ----------
install_qwen() {
    ensure_environment
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        warn "nvidia-smi não encontrado: a Qwen3-TTS precisa de placa NVIDIA com o driver instalado."
        read -r -p "Instalar mesmo assim? [s/N] " answer
        [[ "$answer" =~ ^[sS] ]] || exit 1
    fi
    say "Instalando o PyTorch com CUDA (vários GB, pode demorar)..."
    "$PY" -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
    say "Instalando a Qwen3-TTS..."
    "$PY" -m pip install -r "$DIR/requirements-qwen.txt"
    say "Pronto. Escolha \"Qwen3-TTS 1.7B (local)\" no app; o modelo (~4 GB) é baixado no primeiro uso."
}

create_desktop_entry() {
    local target="${XDG_DATA_HOME:-$HOME/.local/share}/applications/localreader.desktop"
    mkdir -p "$(dirname "$target")"
    cat >"$target" <<EOF
[Desktop Entry]
Type=Application
Name=LocalReader
Comment=Leitor de documentos em voz alta, com OCR
Exec="$DIR/run.sh" %f
Path=$DIR
Icon=audio-speakers
Terminal=false
Categories=Office;Accessibility;AudioVideo;
MimeType=application/pdf;text/plain;text/markdown;application/vnd.openxmlformats-officedocument.wordprocessingml.document;
EOF
    chmod +x "$target"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(dirname "$target")" || true
    say "Atalho criado em $target (procure \"LocalReader\" no menu de aplicativos)."
}

case "${1:-}" in
    --install-qwen) install_qwen; exit 0 ;;
    --desktop) create_desktop_entry; exit 0 ;;
    --check) check_system; exit 0 ;;
    --reinstall) rm -rf "$VENV"; ensure_environment; check_system; exit 0 ;;
    -h | --help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

if [ ! -x "$PY" ]; then
    # Primeira execução: confere o sistema antes de instalar.
    check_system
fi
ensure_environment
exec "$PY" "$DIR/main.py" "$@"
