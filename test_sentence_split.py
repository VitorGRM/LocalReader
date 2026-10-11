import time
import unittest

from sentence_split import split_sentences, step_word, word_span, word_starts


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


class WordNavigationTest(unittest.TestCase):
    def setUp(self):
        self.text = "Um dois três. Quatro cinco. Seis."
        self.sentences = split_sentences(self.text)

    def word_at(self, index, offset):
        start = self.sentences[index][1] if offset is None else offset
        s, e = word_span(self.sentences[index], start)
        return self.text[s:e]

    def test_moves_inside_sentence_and_across(self):
        pos = (0, None)
        seen = []
        for _ in range(6):
            pos = step_word(self.sentences, *pos, 1)
            seen.append(self.word_at(*pos))
        self.assertEqual(seen, ["dois", "três.", "Quatro", "cinco.", "Seis.", "Seis."])

    def test_moves_back_to_last_word_of_previous_sentence(self):
        pos = step_word(self.sentences, 1, None, -1)
        self.assertEqual(pos, (0, self.text.index("três")))
        self.assertEqual(step_word(self.sentences, 0, None, -1), (0, None))

    def test_first_word_is_none(self):
        pos = step_word(self.sentences, 0, self.text.index("dois"), -1)
        self.assertEqual(pos, (0, None))
        self.assertEqual(word_starts(self.sentences[1]), [self.text.index("Quatro"), self.text.index("cinco")])

    def test_single_word_sentence_back(self):
        sentences = split_sentences("Um. Dois três.")
        self.assertEqual(step_word(sentences, 1, None, -1), (0, None))


if __name__ == "__main__":
    unittest.main()
