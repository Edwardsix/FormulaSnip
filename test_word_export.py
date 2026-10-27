import tempfile
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from word_export import (MATHML_NS, OMML_NS, WORD_NS, WordExportError,
                         build_clipboard_html, latex_to_mathml, latex_to_omml,
                         save_docx)

M = "{" + OMML_NS + "}"


class WordExportTests(unittest.TestCase):
    def test_fraction_has_native_numerator_denominator(self):
        root = ET.fromstring(latex_to_omml(r"\frac{x+1}{y-2}"))
        fraction = root.find(".//" + M + "f")
        self.assertIsNotNone(fraction)
        self.assertEqual("".join(fraction.find(M + "num").itertext()), "x+1")
        self.assertEqual("".join(fraction.find(M + "den").itertext()), "y−2")

    def test_roots_have_degree_argument(self):
        for value, degree in ((r"\sqrt{x}", ""), (r"\sqrt[3]{x}", "3")):
            with self.subTest(value=value):
                root = ET.fromstring(latex_to_omml(value))
                radical = root.find(".//" + M + "rad")
                self.assertIsNotNone(radical.find(M + "e"))
                self.assertEqual("".join(radical.find(M + "deg").itertext()), degree)
                if not degree:
                    self.assertEqual(radical.find(M + "radPr/" + M + "degHide").get(M + "val"), "1")

    def test_sum_preserves_limits_and_exponent(self):
        root = ET.fromstring(latex_to_omml(r"\sum_{i=1}^{n}i^2"))
        nary = root.find(".//" + M + "nary")
        self.assertEqual(nary.find(M + "naryPr/" + M + "chr").get(M + "val"), "∑")
        self.assertEqual("".join(nary.find(M + "sub").itertext()), "i=1")
        self.assertEqual("".join(nary.find(M + "sup").itertext()), "n")
        self.assertIsNotNone(nary.find(".//" + M + "sSup"))

    def test_matrix_preserves_delimiters_rows_and_columns(self):
        root = ET.fromstring(latex_to_omml(r"\begin{bmatrix}a&b\\c&d\end{bmatrix}"))
        matrix = root.find(".//" + M + "m")
        rows = matrix.findall(M + "mr")
        self.assertEqual(len(rows), 2)
        self.assertEqual([len(row.findall(M + "e")) for row in rows], [2, 2])
        self.assertEqual(len(matrix.findall(M + "mPr/" + M + "mcs/" + M + "mc")), 2)
        self.assertEqual(root.find(".//" + M + "begChr").get(M + "val"), "[")
        self.assertEqual(root.find(".//" + M + "endChr").get(M + "val"), "]")

    def test_aligned_preserves_right_left_alignment(self):
        root = ET.fromstring(latex_to_omml(r"\begin{aligned}a&=b+c\\d&=e\end{aligned}"))
        alignment = root.findall(".//" + M + "mcJc")
        self.assertEqual([item.get(M + "val") for item in alignment], ["right", "left"])
        self.assertEqual(len(root.findall(".//" + M + "mr")), 2)

    def test_nested_matrices_have_separate_properties(self):
        root = ET.fromstring(latex_to_omml(r"\begin{pmatrix}\begin{matrix}a&b\end{matrix}&c\\d&e\end{pmatrix}"))
        self.assertEqual(len(root.findall(".//" + M + "m")), 2)
        self.assertEqual(len(root.findall(".//" + M + "mPr")), 2)

    def test_chinese_text_and_special_characters_are_xml_safe(self):
        mathml = latex_to_mathml(r"x<y\quad\text{中文}")
        root = ET.fromstring(mathml)
        self.assertIn("中文", "".join(root.itertext()))
        omml = ET.fromstring(latex_to_omml(r"x<y\quad\text{中文}"))
        self.assertIn("<", "".join(omml.itertext()))
        self.assertIn("中文", "".join(omml.itertext()))

    def test_wrappers_do_not_change_formula(self):
        expected = latex_to_mathml(r"\frac{1}{2}")
        for wrapped in (r"$\frac{1}{2}$", r"\[\frac{1}{2}\]", "```latex\n\\frac{1}{2}\n```", r"\begin{equation}\frac{1}{2}\end{equation}"):
            self.assertEqual(latex_to_mathml(wrapped), expected)

    def test_invalid_and_unknown_input_is_rejected(self):
        cases = ["", " ", "{x", "x}", r"\frac{x}", r"\badcommand{x}",
                 r"\begin{matrix}x\end{cases}", r"\begin{foo}x\end{foo}",
                 r"\begin{matrix}x", r"\input{passwords}", r"\newcommand{\foo}{x}",
                 "x\x00y", "x" * 20001]
        for value in cases:
            with self.subTest(value=value[:40]):
                with self.assertRaises(WordExportError):
                    latex_to_mathml(value)

    def test_clipboard_has_one_math_fragment(self):
        html = build_clipboard_html(r"\frac{x}{y}")
        self.assertNotIn("<!--StartFragment-->", html)
        self.assertNotIn("<!--EndFragment-->", html)
        fragment = html.split("<body>")[1].split("</body>")[0]
        self.assertEqual(ET.fromstring(fragment).tag, "{" + MATHML_NS + "}math")

    def test_docx_is_complete_package_with_native_math(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "公式.docx"
            save_docx(r"x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}", str(path))
            with ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                for name in archive.namelist():
                    ET.fromstring(archive.read(name))
                root = ET.fromstring(archive.read("word/document.xml"))
                self.assertEqual(root.tag, "{" + WORD_NS + "}document")
                self.assertIsNotNone(root.find(".//" + M + "oMath"))
                self.assertIsNotNone(root.find(".//" + M + "f"))
                self.assertNotIn("word/media/", "\n".join(archive.namelist()))

    def test_failed_conversion_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "existing.docx"
            path.write_bytes(b"original")
            with self.assertRaises(WordExportError):
                save_docx(r"\badcommand{x}", str(path))
            self.assertEqual(path.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
