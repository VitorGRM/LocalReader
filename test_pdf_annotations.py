import os
import tempfile
import unittest

import pymupdf

from pdf_annotations import Highlight, export_with_highlights, load_highlights, make_highlight, save_highlights
from pdf_layout import extract_pdf_layout


def _make_pdf(path: str, text: str, rotation: int = 0) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 60), text, fontsize=12)
    page.set_rotation(rotation)
    doc.save(path)
    doc.close()


class HighlightStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = os.path.join(self.tmp.name, "store")
        self.path = os.path.join(self.tmp.name, "doc.pdf")
        _make_pdf(self.path, "Primeira frase aqui. Segunda frase.")
        self.layout = extract_pdf_layout(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        saved = [make_highlight(self.layout, 2, 1, "green"), make_highlight(self.layout, 3, 3, "yellow")]
        self.assertEqual(saved[0], Highlight(1, 2, "green", "frase aqui."))
        save_highlights(self.path, saved, self.store)
        self.assertEqual(load_highlights(self.path, self.layout, self.store), saved)

    def test_missing_and_empty(self):
        self.assertEqual(load_highlights(self.path, self.layout, self.store), [])
        save_highlights(self.path, [make_highlight(self.layout, 0, 0, "blue")], self.store)
        save_highlights(self.path, [], self.store)  # sem realces: o arquivo some
        self.assertEqual(os.listdir(self.store), [])

    def test_drops_highlights_that_no_longer_match(self):
        save_highlights(
            self.path,
            [
                Highlight(0, 0, "yellow", "Primeira"),
                Highlight(0, 0, "yellow", "Outro texto"),  # o PDF mudou
                Highlight(0, 99, "yellow", "x"),  # fora do documento
                Highlight(1, 1, "roxo", "frase"),  # cor desconhecida
                Highlight(2, 2, "blue", "fras"),  # pedaço de palavra
            ],
            self.store,
        )
        self.assertEqual(load_highlights(self.path, self.layout, self.store), [Highlight(0, 0, "yellow", "Primeira")])

    def test_relocates_highlight_when_word_numbers_change(self):
        # "frase" aparece duas vezes (palavras 1 e 4); o índice antigo (5) está mais perto da segunda.
        save_highlights(self.path, [Highlight(5, 5, "green", "frase.")], self.store)
        self.assertEqual(load_highlights(self.path, self.layout, self.store), [Highlight(4, 4, "green", "frase.")])
        save_highlights(self.path, [Highlight(0, 1, "green", "Segunda   frase.")], self.store)  # espaços diferentes
        self.assertEqual(load_highlights(self.path, self.layout, self.store), [Highlight(3, 4, "green", "Segunda frase.")])

    def test_corrupted_file_is_ignored(self):
        save_highlights(self.path, [make_highlight(self.layout, 0, 0, "blue")], self.store)
        (name,) = os.listdir(self.store)
        with open(os.path.join(self.store, name), "w", encoding="utf-8") as fh:
            fh.write("{não é json")
        self.assertEqual(load_highlights(self.path, self.layout, self.store), [])


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _export_and_read(self, rotation: int) -> list[str]:
        src = os.path.join(self.tmp.name, f"src{rotation}.pdf")
        dest = os.path.join(self.tmp.name, f"dest{rotation}.pdf")
        _make_pdf(src, "Primeira frase aqui.", rotation)
        layout = extract_pdf_layout(src)
        export_with_highlights(src, dest, layout, [make_highlight(layout, 1, 1, "pink")])
        with pymupdf.open(dest) as doc:
            page = doc[0]
            # annot.rect tem uma folga em volta; os vértices são o trecho realçado.
            return [page.get_textbox(pymupdf.Quad(annot.vertices[:4]).rect).strip() for annot in page.annots()]

    def test_export_adds_highlight_over_the_word(self):
        self.assertEqual(self._export_and_read(0), ["frase"])

    def test_export_on_rotated_page(self):
        self.assertEqual(self._export_and_read(90), ["frase"])


if __name__ == "__main__":
    unittest.main()
