"""Versão do aplicativo e repositório GitHub usado para buscar atualizações.

Em builds de release, ``build_windows.ps1`` gera ``_build_info.py`` com a
versão da tag e o repositório (``usuario/repositorio``), que sobrescrevem os
valores abaixo. Rodando direto do código-fonte, edite ``GITHUB_REPO`` aqui.
"""

__version__ = "0.0.0-dev"
GITHUB_REPO = ""

try:
    from _build_info import GITHUB_REPO, __version__  # noqa: F401
except ImportError:
    pass
