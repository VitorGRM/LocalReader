import importlib.util
import unittest

from nos_tts import NOS_VOICES, SymbolTable, galician_number, normalize_text, split_chunks, text_to_symbols

NEWLINE = chr(10)
# Tabelas como nas configurações reais das vozes (Celtia: VitsCharacters; Iago: Graphemes).
CELTIA_CHARS = {
    "pad": "_", "eos": "*", "bos": "^", "blank": None,
    "characters": "ABCDEFGHIJKLMNOPQRSTUVXYZabcdefghijklmnopqrstuvwxyzÁÉÍÓÚáéíñóúü",
    "punctuations": '!"(),-.:;?¡¿ ', "is_unique": True, "is_sorted": True,
}
IAGO_CHARS = {
    "pad": "<PAD>", "eos": "<EOS>", "bos": "<BOS>", "blank": "<BLNK>",
    "characters": "ABCDEFGHIJKLMNOPQRSTUVWXYZÇÁÉÍÓÚÜabcdefghijklmnopqrstuvwxyzñáéíóúü",
    "punctuations": "!¡'(),-.:;¿? " + '"' + NEWLINE, "is_unique": True, "is_sorted": True,
}


class GalicianNumbersTest(unittest.TestCase):
    def test_words(self):
        cases = {
            0: "cero", 15: "quince", 21: "vinte e un", 100: "cen", 101: "cento un",
            555: "cincocentos cincuenta e cinco", 1000: "mil", 1922: "mil novecentos vinte e dous",
            2024: "dous mil vinte e catro", 1_000_000: "un millón", 3_200_000: "tres millóns douscentos mil",
        }
        for number, words in cases.items():
            self.assertEqual(galician_number(number), words, number)

    def test_normalize(self):
        self.assertEqual(normalize_text("En 2024, o 35% de 1.500 persoas."),
                         "En dous mil vinte e catro , o trinta e cinco por cento de mil cincocentos persoas.")
        self.assertEqual(normalize_text("Mide 3,5 metros"), "Mide tres coma cinco metros")


class TextToSymbolsTest(unittest.TestCase):
    def test_chunks_keep_punctuation_apart(self):
        self.assertEqual(split_chunks("Ola, bo día! Que tal?"), [("Ola", ","), ("bo día", "!"), ("Que tal", "?")])
        self.assertEqual(split_chunks("Fin... (nota)"), [("Fin", "...("), ("nota", ")")])

    def test_celtia_reads_lowercase_letters(self):
        self.assertEqual(text_to_symbols("Ola, bo día! En 2 anos.", NOS_VOICES["gl-ES-Celtia"]),
                         "ola, bo día! en dous anos.")

    @unittest.skipUnless(importlib.util.find_spec("pycotovia"), "pycotovia não instalado")
    def test_phonemes_with_stress_and_unstressed_words(self):
        symbols = text_to_symbols("Como van executar o plan de hoxe?", NOS_VOICES["gl-ES-Sabela"])
        self.assertEqual(symbols, "kómo BáN eSekutár o plán de óSe?")  # "d" oclusivo depois de nasal


class SymbolTableTest(unittest.TestCase):
    def test_vits_order(self):
        table = SymbolTable(CELTIA_CHARS, "vits")
        self.assertEqual(len(table.ids), 78)  # o tamanho da tabela do modelo
        self.assertEqual(table.ids["_"], 0)
        self.assertEqual(table.ids["!"], 1)
        self.assertEqual(table.ids[" "], 13)
        self.assertEqual(table.ids["A"], 14)
        self.assertEqual(table.blank_id, 77)
        self.assertEqual(table.encode("ab"), [77, table.ids["a"], 77, table.ids["b"], 77])

    def test_graphemes_order(self):
        table = SymbolTable(IAGO_CHARS, "graphemes")
        self.assertEqual([table.ids[s] for s in ("<PAD>", "<EOS>", "<BOS>", "<BLNK>")], [0, 1, 2, 3])
        self.assertEqual(table.ids["A"], 4)  # caracteres em ordem, depois a pontuação
        self.assertEqual(table.ids["!"], 4 + 66)
        self.assertEqual(len(table.ids), 85)  # o tamanho da tabela do modelo do Iago
        self.assertEqual(table.encode("x?"), [3, table.ids["x"], 3, table.ids["?"], 3])
        self.assertEqual(table.encode("@"), [3])  # símbolo desconhecido é ignorado


class QwenInstructionTest(unittest.TestCase):
    def test_rate_becomes_instruction(self):
        from qwen_voices import rate_instruction

        self.assertEqual(rate_instruction(0), "")
        self.assertIn("faster", rate_instruction(20))
        self.assertIn("very slowly", rate_instruction(-50))


if __name__ == "__main__":
    unittest.main()
