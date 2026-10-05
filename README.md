# TTS Reader + OCR

Aplicativo desktop com duas áreas integradas:

- **Leitor TTS:** abre PDF, DOCX, Markdown e TXT, divide o texto em períodos e usa vozes Edge, Piper ou gTTS.
- **OCR de alta qualidade:** extrai PDFs e imagens com Tesseract, reaproveita automaticamente camadas de texto confiáveis e envia o resultado ao leitor.

## OCR adaptativo

O modo recomendado avalia cada página separadamente. Páginas digitais com uma camada de texto saudável são extraídas sem rasterização; páginas escaneadas, vazias ou com texto corrompido passam pelo OCR. Isso preserva a fidelidade de PDFs digitais e reduz bastante o tempo em documentos mistos.

A aba OCR permite escolher páginas, resolução de 150 a 600 dpi, múltiplos idiomas, sensibilidade da decisão automática, uma/duas/três colunas, texto esparso, cinco modos de pré-processamento, correção de orientação e inclinação, retirada de cabeçalhos/rodapés e deshifenização. O resultado pode ser salvo em TXT, Markdown ou DOCX, convertido em PDF pesquisável ou enviado ao TTS.

## Instalação

As dependências Python são instaladas com:

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
```

O OCR requer os executáveis Tesseract e, para criar PDFs pesquisáveis, OCRmyPDF. Em Debian/Ubuntu, por exemplo:

```bash
sudo apt install tesseract-ocr tesseract-ocr-por ocrmypdf
```

Instale os pacotes de idioma desejados (`tesseract-ocr-eng`, `tesseract-ocr-spa`, `tesseract-ocr-fra` etc.). A lista da interface é atualizada a partir dos modelos realmente presentes no sistema; não há catálogo fixo.

Execute com `./run.sh`.

## Versão para Windows (.exe) e atualizações

O app é distribuído como instalador (`TTSReader-Setup.exe`), com o Tesseract e os idiomas pt/en/es/fr/de/it embutidos. A instalação é por usuário, sem precisar de administrador. Para o OCR com idiomas extras, copie os arquivos `.traineddata` para `tesseract\tessdata` dentro da pasta de instalação. A criação de PDF pesquisável (OCRmyPDF) não vem embutida e fica desativada no `.exe`.

### Publicando uma versão

1. Crie um repositório **público** no GitHub e envie este projeto.
2. Publique uma tag de versão:

   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```

3. O workflow [`.github/workflows/release.yml`](.github/workflows/release.yml) compila o instalador no GitHub e cria a Release com `TTSReader-Setup.exe` e o `.sha256`.

Para lançar atualizações, repita com uma tag maior (`v1.0.1`, `v1.1.0`...). O repositório é preenchido automaticamente no build.

### Como o app se atualiza

Ao abrir, o app consulta a última Release do repositório. Se houver versão maior, pergunta se você quer atualizar. Em caso positivo, baixa o instalador, confere o SHA-256, fecha, instala por cima e reabre. Também há **Ajuda → Verificar atualizações**.

### Build local

Requer Python 3.11+ no PATH. O Tesseract e o Inno Setup são instalados via Chocolatey se faltarem.

```powershell
.\build_windows.ps1 -Version v1.0.0 -Repo seuusuario/tts-reader
```

O resultado fica em `release\`. Para um ícone próprio, coloque `assets\icon.ico`. O instalador não é assinado digitalmente, então o Windows SmartScreen pode exibir um aviso na primeira execução.

## Testes

```bash
venv/bin/python -m unittest -v
```
