import os
import shutil
import tempfile
import unittest

import pymupdf

from pdf_cleanup import Token, join_hyphenated, rebuild_lines
from pdf_layout import ExtractOptions, _assemble, extract_pdf_layout
from platform_support import setup_bundled_tools

PAGE_W, PAGE_H = 400, 600
BODY_SIZE, NOTE_SIZE, SUP_SIZE = 11, 8, 7


def _write(page, x, y, text, size=BODY_SIZE):
    page.insert_text((x, y), text, fontsize=size)
    return x + pymupdf.get_text_length(text, fontsize=size)


def _book_page(doc, number, lines, note=None, header=True, title=None):
    """Página com cabeçalho corrido, número no rodapé e, opcionalmente, um título
    e uma nota de rodapé com chamada sobrescrita no fim da primeira linha."""
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    if header:
        _write(page, 40, 30, "Livro de Teste", size=9)
    y = 90
    if title:
        _write(page, 40, y, title, size=16)
        y += 20
    for i, line in enumerate(lines):
        end = _write(page, 40, y, line)
        if note and i == 0:
            _write(page, end, y - 4, "1", size=SUP_SIZE)
        y += 16
    if note:
        _write(page, 40, 540, "1", size=6)
        _write(page, 46, 543, note, size=NOTE_SIZE)
    _write(page, PAGE_W / 2, 580, str(number), size=9)
    return page


class CleanupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, "livro.pdf")
        doc = pymupdf.open()
        _book_page(doc, title="Capitulo Um", lines=["Era uma vez um texto com exem-", "plo partido e ele disse-", "-lhe que sim; e o texto continua na"], number=1, header=False)
        _book_page(doc, 2, ["proxima pagina sem quebrar a frase.", "Outra frase com nota de rodape aqui.", "Mais um periodo comum."], note="Texto da nota explicando algo.")
        _book_page(doc, 3, ["Terceira pagina com texto normal.", "E um pouco mais de conteudo."])
        _book_page(doc, 4, ["Quarta pagina para repetir o cabecalho.", "Fim do livro."])
        doc.save(cls.path)
        doc.close()
        cls.layout = extract_pdf_layout(cls.path, options=ExtractOptions(ocr=False))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def sentences(self, layout=None):
        return [s for s, _, _ in (layout or self.layout).sentences]

    def test_headers_and_page_numbers_are_not_read(self):
        text = self.layout.text
        self.assertNotIn("Livro de Teste", text)
        for number in ("1", "2", "3", "4"):
            self.assertNotIn(f"\n{number}\n", f"\n{text}\n")
        self.assertTrue(text.endswith("Fim do livro."), text[-40:])

    def test_hyphenated_words_are_joined(self):
        self.assertIn("exemplo partido", self.layout.text)
        self.assertIn("disse-lhe que sim", self.layout.text)  # hífen repetido: palavra composta

    def test_sentence_continues_on_next_page_and_title_is_separate(self):
        sentences = self.sentences()
        self.assertEqual(sentences[0], "Capitulo Um")
        self.assertIn("continua na\nproxima pagina sem quebrar a frase.", sentences[1])
        # O período que atravessa a página é destacado nas duas.
        _, start, end = self.layout.sentences[1]
        self.assertEqual({page for page, _ in self.layout.range_rects(start, end)}, {0, 1})

    def test_footnote_marker_and_text_are_skipped_by_default(self):
        self.assertIn("Outra frase com nota de rodape aqui.", self.sentences())
        self.assertNotIn("Texto da nota", self.layout.text)
        # A palavra sem a chamada continua com o retângulo certo (não cobre o "1").
        offset = self.layout.text.index("aqui.")
        _, (x0, _, x1, _) = self.layout.word_rect_at(offset)
        self.assertLess(x1, x0 + pymupdf.get_text_length("aqui.", fontsize=BODY_SIZE) + 0.5)

    def test_footnotes_can_be_read(self):
        layout = extract_pdf_layout(self.path, options=ExtractOptions(ocr=False, read_footnotes=True))
        self.assertIn("Texto da nota explicando algo.", self.sentences(layout))
        self.assertIn("rodape aqui.", layout.text)  # a chamada continua fora

    def test_text_matches_word_offsets(self):
        words = self.layout.words
        for i in range(len(words)):
            word = self.layout.text[words.starts[i]:words.ends[i]]
            self.assertTrue(word and not any(c.isspace() for c in word), repr(word))


class HyphenTest(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(join_hyphenated("exem-", "plo"), ("exem", "plo"))
        self.assertEqual(join_hyphenated("guarda-", "-chuva"), ("guarda-", "chuva"))
        self.assertEqual(join_hyphenated("fazê-", "lo."), ("fazê-", "lo."))  # pronome
        self.assertEqual(join_hyphenated("exem\u00ad", "plo"), ("exem", "plo"))
        self.assertIsNone(join_hyphenated("Rio-", "Grande"))  # maiúscula: não é sílaba
        self.assertIsNone(join_hyphenated("10-", "20"))
        self.assertIsNone(join_hyphenated("palavra", "seguinte"))


class SlideTitleTest(unittest.TestCase):
    def test_big_repeated_titles_are_kept(self):
        """Títulos grandes repetidos no topo (slides) não são cabeçalho corrido."""
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "slides.pdf")
            doc = pymupdf.open()
            for i in range(3):
                page = doc.new_page(width=PAGE_W, height=PAGE_H)
                _write(page, 40, 50, "Resultados", size=24)
                _write(page, 40, 200, f"Item {i} do slide.")
            doc.save(path)
            doc.close()
            layout = extract_pdf_layout(path, options=ExtractOptions(ocr=False))
            self.assertEqual(layout.text.count("Resultados"), 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FragmentedLayerTest(unittest.TestCase):
    """Camada de texto de OCR antigo: cada palavra (ou pedaço de linha) num bloco
    próprio, com a linha de base variando um pouco, como nos livros do Internet Archive."""

    LINES = [
        "The year 1921 not only marked the victorious end of the Civil",
        "War, it was also the year of economic crisis and of change.",
        "But the New Economic Policy was actually a backward step.",
    ]

    def _tokens(self):
        tokens, block = [], 0
        for row, line in enumerate(self.LINES):
            x, top = 40.0, 90.0 + row * 16
            for i, word in enumerate(line.split()):
                jitter = (-1.5, 0.0, 1.0)[i % 3]
                width = len(word) * 5.5
                tokens.append(Token(word, (x, top + jitter, x + width, top + 12 + jitter), (block, 0)))
                block += 1  # cada palavra num bloco, como na camada fragmentada
                x += width + 4
        return tokens

    def test_rebuilt_lines_keep_sentences_whole(self):
        tokens = self._tokens()
        rebuild_lines(tokens)
        self.assertEqual(len({t.line for t in tokens}), 3)  # três linhas visuais
        self.assertEqual(len({t.block for t in tokens}), 1)  # um parágrafo só
        text, _ = _assemble([tokens], {"body"})
        self.assertEqual(text, "\n".join(self.LINES))
        sentences = [s for s, _, _ in extract_sentences(text)]
        self.assertEqual(sentences[0], "\n".join(self.LINES[:2]))  # "Civil / War" não corta a frase

    def test_paragraph_breaks_by_indent_gap_and_short_line(self):
        def line(y, x0, x1, word, block):
            return Token(word, (x0, y, x1, y + 12), (block, 0))

        tokens = [
            line(90, 40, 360, "Uma", 0), line(106, 40, 200, "curta.", 1),  # termina antes da margem
            line(122, 60, 360, "Recuada", 2), line(138, 40, 360, "segue", 3),  # recuo de parágrafo
            line(170, 40, 360, "Depois", 4),  # espaço maior entre linhas
        ]
        rebuild_lines(tokens)
        self.assertEqual([t.block for t in tokens], [0, 0, 1, 1, 2])


def extract_sentences(text):
    from sentence_split import split_sentences

    return split_sentences(text)


class OcrTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        setup_bundled_tools()
        if not shutil.which("tesseract"):
            raise unittest.SkipTest("Tesseract não instalado")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scanned_pdf(self) -> str:
        """PDF com uma página de texto e outra só com a imagem de um texto."""
        source = pymupdf.open()
        page = source.new_page(width=PAGE_W, height=PAGE_H)
        _write(page, 40, 100, "The quick brown fox jumps over the lazy dog.", size=14)
        _write(page, 40, 130, "Scanned pages are read with OCR now.", size=14)
        image = page.get_pixmap(matrix=pymupdf.Matrix(3, 3)).tobytes("png")
        source.close()

        path = os.path.join(self.tmp, "scan.pdf")
        doc = pymupdf.open()
        _write(doc.new_page(width=PAGE_W, height=PAGE_H), 40, 100, "Native text page.")
        doc.new_page(width=PAGE_W, height=PAGE_H).insert_image(pymupdf.Rect(0, 0, PAGE_W, PAGE_H), stream=image)
        doc.save(path)
        doc.close()
        return path

    def test_scanned_page_is_recognized_with_word_positions(self):
        path = self._scanned_pdf()
        cache = os.path.join(self.tmp, "cache")
        layout = extract_pdf_layout(path, options=ExtractOptions(ocr_cache_dir=cache))
        self.assertEqual(layout.ocr_pages, [1])
        self.assertEqual(layout.ocr_missing_pages, [])
        self.assertIn("quick brown fox", layout.text)
        self.assertIn("read with OCR now.", layout.text)
        # A palavra reconhecida fica no lugar certo da página (texto em y≈100).
        page, (x0, y0, x1, y1) = layout.word_rect_at(layout.text.index("quick"))
        self.assertEqual(page, 1)
        self.assertTrue(80 < y1 < 110 and y0 > 80, (y0, y1))
        # Reabrir usa o cache (sem chamar o Tesseract).
        self.assertTrue(os.listdir(cache))
        tesseract = shutil.which("tesseract")
        os.environ["PATH"] = os.environ["PATH"].replace(os.path.dirname(tesseract), "")
        try:
            import pdf_ocr

            original = pdf_ocr._Tesseract.run
            pdf_ocr._Tesseract.run = lambda *_: self.fail("deveria usar o cache")
            again = extract_pdf_layout(path, options=ExtractOptions(ocr_cache_dir=cache))
        finally:
            pdf_ocr._Tesseract.run = original
            os.environ["PATH"] = os.path.dirname(tesseract) + os.pathsep + os.environ["PATH"]
        self.assertEqual(again.text, layout.text)

    def test_force_ocr_replaces_old_text_layer(self):
        """Página digitalizada que já traz texto (de um OCR antigo, aqui com erros)."""
        path = self._scanned_pdf()
        doc = pymupdf.open(path)
        doc[1].insert_text((40, 100), "Tke qu1ck br0wn f0x jumps 0ver the lazy d0g and m0re.", fontsize=14, render_mode=3)
        doc.saveIncr()
        doc.close()
        cache = os.path.join(self.tmp, "cache")
        original = extract_pdf_layout(path, options=ExtractOptions(ocr_cache_dir=cache))
        self.assertEqual(original.scanned_text_pages, 1)
        self.assertEqual(original.ocr_pages, [])
        self.assertIn("qu1ck", original.text)
        redone = extract_pdf_layout(path, options=ExtractOptions(ocr_cache_dir=cache, force_ocr=True))
        self.assertEqual(redone.ocr_pages, [1])
        self.assertIn("quick brown fox", redone.text)
        self.assertNotIn("qu1ck", redone.text)
        self.assertIn("Native text page.", redone.text)  # página sem imagem continua com o texto nativo

    def test_without_ocr_the_page_is_reported(self):
        layout = extract_pdf_layout(self._scanned_pdf(), options=ExtractOptions(ocr=False))
        self.assertEqual(layout.ocr_pages, [])
        self.assertNotIn("fox", layout.text)


if __name__ == "__main__":
    unittest.main()
