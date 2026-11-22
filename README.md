# LocalReader + OCR

Aplicativo desktop com duas áreas integradas:

- **Leitor TTS:** abre PDF, DOCX, Markdown e TXT, divide o texto em períodos e usa vozes Edge, Piper, Qwen3-TTS (local), Proxecto Nós (galego) ou gTTS. Cada documento abre numa aba própria, com voz, velocidade e posição de leitura independentes. PDFs são exibidos como PDF, com o período lido destacado sobre a página; passe o mouse sobre um período e clique para pular direto para ele.
- **Texto limpo para ouvir:** nos PDFs, cabeçalhos, rodapés e números de página não são lidos, palavras partidas no fim da linha são juntadas, chamadas de nota (¹, ²) saem do meio das frases e as notas de rodapé são puladas (dá para ativá-las em Exibir). Um período que continua na página seguinte é lido de uma vez, e títulos não grudam no parágrafo. As linhas e parágrafos são refeitos pela posição das palavras na página, o que deixa fluida a leitura de livros digitalizados com camadas de texto fragmentadas (como os do Internet Archive). Páginas digitalizadas ou com texto ilegível são reconhecidas por OCR ao abrir, com o mesmo destaque sobre a página; o resultado fica em cache para as próximas aberturas. Se o texto que veio no PDF tiver erros de um OCR antigo, **⋯ → Refazer o OCR deste PDF** reconhece tudo de novo com o Tesseract.
- **Ferramentas de leitura:** no PDF e no texto (arquivo ou colado) dá para selecionar e copiar, buscar com Ctrl+F (sem diferenciar maiúsculas nem acentos), realçar com marca-texto em quatro cores (Ctrl+H, salvo por documento) e aumentar a letra (Ctrl+= / Ctrl+-). No PDF também há ir para a página (Ctrl+G), sumário e salvar uma cópia com os realces.
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

### Linux

O `run.sh` faz tudo: na primeira vez cria o ambiente Python e instala as dependências (e reinstala sozinho quando o `requirements.txt` muda, por exemplo depois de um `git pull`); nas próximas, só abre o app.

```bash
./run.sh                  # abre o app (instalando o que faltar)
./run.sh --desktop        # cria o atalho "LocalReader" no menu de aplicativos
./run.sh --install-qwen   # instala também a voz Qwen3-TTS (PyTorch com CUDA)
./run.sh --check          # confere Python, Tesseract e bibliotecas do sistema
./run.sh --reinstall      # apaga o ambiente e instala tudo de novo
```

Além do Tesseract, o Qt 6 precisa da biblioteca `libxcb-cursor0` (Debian/Ubuntu: `sudo apt install libxcb-cursor0`; Fedora: `xcb-util-cursor`); o `./run.sh --check` avisa o que faltar. Arquivos passados na linha de comando (ou com "Abrir com") abrem direto no app.

### Vozes

- **Edge** e **gTTS** precisam de internet; **Piper** e **Proxecto Nós** rodam no computador e baixam cada voz uma vez.
- **Proxecto Nós (galego):** Celtia, Sabela e Iago, vozes VITS do Proxecto Nós (Universidade de Santiago de Compostela) em ONNX, rodando com o ONNX Runtime (Windows e Linux, sem placa de vídeo). Sabela e Iago usam a transcrição fonética do Cotovía, gerada pelo `pycotovia` (Python puro). Modelos Apache-2.0, vozes CC BY 4.0.
- **Qwen3-TTS 1.7B (opcional):** voz neural muito natural em 10 idiomas (inclusive português), rodando no computador, com leitura em tom neutro. Em **Áudio → Vozes personalizadas** dá para criar vozes próprias descrevendo-as em texto (por exemplo, "sotaque paulistano, leitura acadêmica neutra") ou clonando 10 a 20 segundos de um áudio; elas aparecem com ★ na lista de vozes. Usa o `faster-qwen3-tts` (CUDA graphs), cerca de 2 vezes mais rápido que a leitura numa RTX 5060. Precisa do PyTorch e, de preferência, de uma placa NVIDIA com ~5 GB de memória de vídeo; o modelo (~4 GB) é baixado no primeiro uso. Não cabe no instalador do Windows, então funciona rodando pelo código-fonte:

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements-qwen.txt
```

## Versão para Windows (.exe) e atualizações

O app é distribuído como instalador (`LocalReader-Setup.exe`), com o Tesseract e os idiomas pt/en/es/fr/de/it embutidos. A instalação é por usuário, sem precisar de administrador. Para o OCR com idiomas extras, copie os arquivos `.traineddata` para `tesseract\tessdata` dentro da pasta de instalação. A criação de PDF pesquisável (OCRmyPDF) não vem embutida e fica desativada no `.exe`.

### Tempo de leitura

No painel de leitura, o app mostra o tempo restante até o fim do documento e o horário previsto de término. A estimativa parte do tamanho do texto e se ajusta à velocidade real da voz conforme a leitura avança.

### Tema

**Exibir → Tema** alterna entre Automático (segue o Windows), Claro e Escuro.

### Publicando uma versão

1. Crie um repositório **público** no GitHub e envie este projeto.
2. Publique uma tag de versão:

   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```

3. O workflow [`.github/workflows/release.yml`](.github/workflows/release.yml) compila o instalador no GitHub e cria a Release com `LocalReader-Setup.exe` e o `.sha256`. Se existir `release-notes/<tag>.md` (ex.: `release-notes/v1.1.0.md`), o texto vira as notas da release e aparece no aviso de atualização do app.

Para lançar atualizações, repita com uma tag maior (`v1.0.1`, `v1.1.0`...). O repositório é preenchido automaticamente no build.

### Como o app se atualiza

Ao abrir, o app consulta a última Release do repositório. Se houver versão maior, pergunta se você quer atualizar. Em caso positivo, baixa o instalador, confere o SHA-256, fecha, instala por cima e reabre. Também há **Ajuda → Verificar atualizações**.

### Build local

Requer Python 3.11+ no PATH. O Tesseract e o Inno Setup são instalados via Chocolatey se faltarem.

```powershell
.\build_windows.ps1 -Version v1.0.0 -Repo seuusuario/LocalReader
```

O resultado fica em `release\`. Para um ícone próprio, coloque `assets\icon.ico`. O instalador não é assinado digitalmente, então o Windows SmartScreen pode exibir um aviso na primeira execução.

## Testes

```bash
venv/bin/python -m unittest -v
```
