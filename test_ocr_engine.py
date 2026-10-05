"""Testes rápidos das partes determinísticas do OCR."""
import unittest

from ocr_engine import OCRProcessor, format_page_indices, native_text_quality, parse_page_expression


class PageExpressionTests(unittest.TestCase):
    def test_all_pages(self):
        self.assertEqual(parse_page_expression("", 4), [0, 1, 2, 3])

    def test_ranges_and_open_end(self):
        self.assertEqual(parse_page_expression("1-3, 5, 8-", 10), [0, 1, 2, 4, 7, 8, 9])

    def test_invalid_range(self):
        with self.assertRaises(ValueError):
            parse_page_expression("5-2", 10)

    def test_format_ranges(self):
        self.assertEqual(format_page_indices([0, 1, 2, 4, 7, 8, 9]), "1-3,5,8-10")


class QualityTests(unittest.TestCase):
    def test_empty_text_is_not_usable(self):
        self.assertEqual(native_text_quality(""), 0)

    def test_normal_paragraph_is_usable(self):
        paragraph = "Este documento contém texto selecionável e palavras legíveis. " * 10
        self.assertGreaterEqual(native_text_quality(paragraph), 80)

    def test_tsv_reconstruction_and_confidence(self):
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t95.0\tOlá\n"
            "5\t1\t1\t1\t1\t2\t10\t0\t10\t10\t85.0\tmundo\n"
        )
        text, confidence = OCRProcessor._parse_tsv(tsv)
        self.assertEqual(text, "Olá mundo")
        self.assertEqual(confidence, 90.0)


if __name__ == "__main__":
    unittest.main()
