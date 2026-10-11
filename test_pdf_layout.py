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

    def test_pdf_without_text(self):
        _make_pdf(self.path, [[]])
        layout = extract_pdf_layout(self.path)
        self.assertEqual(layout.sentences, [])
        self.assertEqual(len(layout.page_sizes), 1)

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
