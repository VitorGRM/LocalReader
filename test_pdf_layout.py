import os
import tempfile
import unittest

import pymupdf

from pdf_layout import extract_pdf_layout, sentence_index_at_offset


def _make_pdf(path: str, pages: list[list[tuple[float, str]]]) -> None:
    """Cria um PDF com as linhas dadas como (y, texto) em cada página."""
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page(width=400, height=300)
        for y, text in lines:
            page.insert_text((40, y), text, fontsize=12)
    doc.save(path)
    doc.close()


class PdfLayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "doc.pdf")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sentences_and_text_match_offsets(self):
        _make_pdf(self.path, [[(60, "Primeira frase aqui. Segunda frase"), (80, "continua na linha seguinte.")]])
        layout = extract_pdf_layout(self.path)
        self.assertEqual([s for s, _, _ in layout.sentences][0], "Primeira frase aqui.")
        self.assertEqual(len(layout.sentences), 2)
        for sentence, start, end in layout.sentences:
            self.assertEqual(layout.text[start:end], sentence)

    def test_sentence_spanning_two_lines_has_one_rect_per_line(self):
        _make_pdf(self.path, [[(60, "Primeira frase aqui. Segunda frase"), (80, "continua na linha seguinte.")]])
        layout = extract_pdf_layout(self.path)
        first, second = layout.sentence_rects
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 2)
        (_, (_, top_a, _, _)), (_, (_, top_b, _, _)) = second
        self.assertLess(top_a, top_b)
        # A segunda frase começa depois da primeira, na mesma linha.
        self.assertGreater(second[0][1][0], first[0][1][2] - 1)

    def test_pages_and_hit_testing(self):
        _make_pdf(self.path, [[(60, "Na primeira pagina.")], [(100, "Na segunda pagina.")]])
        layout = extract_pdf_layout(self.path)
        self.assertEqual(len(layout.page_sizes), 2)
        self.assertEqual(len(layout.sentences), 2)
        page, (x0, y0, x1, y1) = layout.sentence_rects[1][0]
        self.assertEqual(page, 1)
        self.assertEqual(layout.sentence_at(1, (x0 + x1) / 2, (y0 + y1) / 2), 1)
        self.assertIsNone(layout.sentence_at(0, (x0 + x1) / 2, (y0 + y1) / 2))
        self.assertIsNone(layout.sentence_at(1, 5, 5))

    def test_word_rect_lookup(self):
        _make_pdf(self.path, [[(60, "Primeira frase aqui.")]])
        layout = extract_pdf_layout(self.path)
        offset = layout.text.index("frase")
        page, (x0, y0, x1, y1) = layout.word_rect_at(offset)
        self.assertEqual(page, 0)
        first = layout.word_rect_at(0)[1]
        self.assertGreater(x0, first[2] - 1)  # "frase" fica à direita de "Primeira"
        self.assertIsNone(layout.word_rect_at(offset + 1))  # meio de palavra

    def test_word_near_and_selection_text(self):
        _make_pdf(self.path, [[(60, "Primeira frase aqui."), (80, "Outra linha.")], [(60, "Segunda pagina.")]])
        layout = extract_pdf_layout(self.path)
        frase = layout.text.index("frase")
        _, (x0, y0, x1, y1) = layout.word_rect_at(frase)
        middle = layout.word_near(0, (x0 + x1) / 2, (y0 + y1) / 2)
        self.assertEqual(layout.words.starts[middle], frase)
        # Fora do texto: fica com a palavra mais próxima da mesma linha.
        last_in_line = layout.word_near(0, 390, (y0 + y1) / 2)
        self.assertEqual(layout.text[layout.words.starts[last_in_line]:layout.words.ends[last_in_line]], "aqui.")
        self.assertIsNone(layout.word_near(5, 10, 10))  # página inexistente

        linha = layout.word_near(0, 390, 300)  # abaixo e à direita de tudo: fim da última linha
        self.assertRegex(layout.words_text(middle, linha), r"^frase aqui\.\n+Outra linha\.$")
        self.assertEqual(layout.words_text(linha, middle), layout.words_text(middle, linha))  # arrastando para trás
        self.assertEqual(len(layout.words_rects(middle, linha)), 2)  # um retângulo por linha

        segunda = layout.word_near(1, 40, 50)
        self.assertEqual(layout.words.pages[segunda], 1)
        self.assertEqual(
            {page for page, _ in layout.words_rects(middle, segunda + 1)}, {0, 1}
        )
        self.assertTrue(layout.words_text(middle, segunda + 1).endswith("Segunda pagina."))

    def test_find_ignores_case_accents_and_line_breaks(self):
        _make_pdf(self.path, [[(60, "Coração e PÁGINA aqui."), (80, "Outra pagina segue."), (100, "fim de")], [(60, "linha.")]])
        layout = extract_pdf_layout(self.path)
        matches = layout.find("pagina")
        self.assertEqual([layout.text[s:e] for s, e in matches], ["PÁGINA", "pagina"])
        self.assertEqual([layout.text[s:e] for s, e in layout.find("coracao")], ["Coração"])
        # Espaço na busca casa com a quebra de linha e de página do texto.
        (start, end), = layout.find("de  linha")
        self.assertEqual({page for page, _ in layout.range_rects(start, end)}, {0, 1})
        self.assertEqual(layout.find("   "), [])
        self.assertEqual(layout.find("inexistente"), [])

    def test_pdf_without_text(self):
        _make_pdf(self.path, [[]])
        layout = extract_pdf_layout(self.path)
        self.assertEqual(layout.sentences, [])
        self.assertEqual(len(layout.page_sizes), 1)
        self.assertIsNone(layout.word_near(0, 10, 10))

    def test_cancel(self):
        from pdf_layout import LoadCancelled

        _make_pdf(self.path, [[(60, "Texto.")]])
        with self.assertRaises(LoadCancelled):
            extract_pdf_layout(self.path, is_cancelled=lambda: True)


class OffsetLookupTest(unittest.TestCase):
    def test_lookup(self):
        sentences = [("Ab.", 0, 3), ("Cd.", 4, 7)]
        starts = [0, 4]
        self.assertEqual(sentence_index_at_offset(sentences, starts, 1), 0)
        self.assertIsNone(sentence_index_at_offset(sentences, starts, 3))  # espaço entre frases
        self.assertEqual(sentence_index_at_offset(sentences, starts, 6), 1)
        self.assertIsNone(sentence_index_at_offset(sentences, starts, 9))


if __name__ == "__main__":
    unittest.main()
