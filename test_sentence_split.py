import time
import unittest

from sentence_split import split_sentences


def texts(text):
    return [s for s, _, _ in split_sentences(text)]


class SentenceSplitTest(unittest.TestCase):
    def test_offsets_match_text(self):
        text = "  Primeira frase. Segunda?  Terceira!"
        for sentence, start, end in split_sentences(text):
            self.assertEqual(text[start:end], sentence)

    def test_abbreviations_initials_and_decimals(self):
        text = "O Dr. Silva leu J. K. Rowling às 3.14 horas. Ver p. 10, etc. Fim."
        self.assertEqual(texts(text), ["O Dr. Silva leu J. K. Rowling às 3.14 horas.", "Ver p. 10, etc. Fim."])

    def test_punctuation_inside_brackets(self):
        text = "Veja [o site](https://site.com/a.html) agora. Depois."
        self.assertEqual(texts(text), ["Veja [o site](https://site.com/a.html) agora.", "Depois."])

    def test_long_word_before_dot_is_not_an_abbreviation(self):
        word = "x" * 100
        self.assertEqual(texts(f"{word}. Depois."), [f"{word}.", "Depois."])

    def test_long_text_is_fast(self):
        text = "A Academia foi fundada em 1725. Pedro I via a ciência como força. " * 20000
        started = time.perf_counter()
        self.assertEqual(len(split_sentences(text)), 40000)
        self.assertLess(time.perf_counter() - started, 5)


if __name__ == "__main__":
    unittest.main()
