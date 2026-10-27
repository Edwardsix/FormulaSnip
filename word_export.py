"""Convert formula LaTeX to MathML and editable Office Math documents.

Conversion is deliberately local: no TeX subprocess and no external service.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

MATHML_NS = "http://www.w3.org/1998/Math/MathML"
OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MAX_LENGTH = 20000
ET.register_namespace("m", OMML_NS)
ET.register_namespace("w", WORD_NS)
_ENVIRONMENTS = frozenset(("matrix", "matrix*", "pmatrix", "pmatrix*", "bmatrix", "bmatrix*", "Bmatrix", "Bmatrix*", "vmatrix", "vmatrix*", "Vmatrix", "Vmatrix*", "array", "smallmatrix", "cases", "split", "align", "align*", "gathered"))


class WordExportError(ValueError):
    """A formula cannot be faithfully converted to the requested format."""


def _normalise(latex: str) -> str:
    if not isinstance(latex, str) or not latex.strip():
        raise WordExportError("请先识别或输入一个公式。")
    value = latex.strip()
    if len(value) > _MAX_LENGTH:
        raise WordExportError("公式过长，请分段导出（每次最多 20,000 个字符）。")
    if value.startswith("```"):
        match = re.fullmatch(r"```(?:latex|tex|math)?\s*\n?(.*?)\n?```", value, re.S | re.I)
        if match:
            value = match.group(1).strip()
    for start, end in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
        if value.startswith(start) and value.endswith(end) and len(value) > len(start) + len(end):
            value = value[len(start):-len(end)].strip()
            break
    # Standalone equation environments are presentation wrappers, not content.
    match = re.fullmatch(r"\\begin\{(equation\*?|displaymath)\}(.*?)\\end\{\1\}", value, re.S)
    if match:
        value = match.group(2).strip()
    # latex2mathml implements split as a matrix of aligned cells.
    value = re.sub(r"\\(begin|end)\{aligned\}", r"\\\1{split}", value)
    if not value:
        raise WordExportError("公式为空。")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        raise WordExportError("公式含有不可显示的控制字符，请重新粘贴。")
    if re.search(r"\\(?:input|include|write|openout|read|documentclass|usepackage|newcommand|renewcommand|def|newenvironment|href|url|html[A-Za-z]*)\b", value):
        raise WordExportError("只支持数学公式；请去掉文档、外部资源或自定义命令。")
    depth = 0
    for token in re.finditer(r"\\.|[{}]", value, re.S):
        part = token.group()
        if part == "{":
            depth += 1
            if depth > 80:
                raise WordExportError("公式嵌套过深，请拆成较短的公式。")
        elif part == "}":
            depth -= 1
            if depth < 0:
                raise WordExportError("公式的花括号不匹配，请检查 { 和 }。")
    if depth:
        raise WordExportError("公式的花括号不匹配，请检查 { 和 }。")
    stack = []
    for match in re.finditer(r"\\(begin|end)\{([^{}]+)\}", value):
        operation, name = match.groups()
        if name not in _ENVIRONMENTS:
            raise WordExportError("暂不支持公式环境 " + name + "，请改为 aligned、cases 或 matrix 等常见公式环境。")
        if operation == "begin":
            stack.append(name)
        elif not stack or stack.pop() != name:
            raise WordExportError("公式环境不匹配，请检查 begin 和 end。")
    if stack:
        raise WordExportError("公式环境缺少 end。")
    value = re.sub(r"\\(begin|end)\{gathered\}", r"\\\1{displaylines}", value)
    return value


def latex_to_mathml(latex: str) -> str:
    """Return standalone, validated Presentation MathML (block display)."""
    value = _normalise(latex)
    try:
        from latex2mathml.converter import convert
    except ImportError as exc:
        raise WordExportError("缺少公式转换组件 latex2mathml，请重新解压完整程序。") from exc
    try:
        mathml = convert(value, display="block")
        tree = ET.fromstring(mathml)
        unknown = []
        argument_counts = {"mfrac": 2, "mroot": 2, "msub": 2, "msup": 2,
                           "msubsup": 3, "munder": 2, "mover": 2, "munderover": 3}
        for element in tree.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag in argument_counts and len(element) != argument_counts[tag]:
                raise WordExportError("公式参数不完整，请检查分式的分子分母或上下标。")
            for text in (element.text, element.tail):
                if text:
                    unknown.extend(re.findall(r"\\[A-Za-z]+", text))
        if unknown:
            raise WordExportError("暂不支持这些 LaTeX 命令：" + "、".join(sorted(set(unknown))[:6]) + "。可复制原始 LaTeX，或改写后再导出。")
        if not "".join(tree.itertext()).strip():
            raise WordExportError("转换后没有可显示的公式内容。")
        return mathml
    except WordExportError:
        raise
    except Exception as exc:
        raise WordExportError("无法转换这段 LaTeX，请检查分式参数、上下标和括号是否完整。") from exc


def latex_to_omml(latex: str) -> str:
    """Return an m:oMath element with an explicit Office Math namespace."""
    mathml = latex_to_mathml(latex)
    try:
        import mathml2omml
    except ImportError as exc:
        raise WordExportError("缺少 Word 转换组件 mathml2omml，请重新解压完整程序。") from exc
    try:
        omml = mathml2omml.convert(mathml)
        # Upstream emits a prefix without its binding; bind it for standalone use.
        if "xmlns:m=" not in omml:
            omml = omml.replace("<m:oMath", f'<m:oMath xmlns:m="{OMML_NS}"', 1)
        tree = ET.fromstring(omml)
        if tree.tag != f"{{{OMML_NS}}}oMath":
            raise WordExportError("Word 公式转换结果无效。")
        if not list(tree):
            raise WordExportError("Word 公式转换结果为空。")
        # Upstream omits the degree argument on square roots. Office Math's
        # radical structure has a degree slot even when it is hidden.
        for radical in tree.iter(f"{{{OMML_NS}}}rad"):
            if radical.find(f"{{{OMML_NS}}}deg") is None:
                props = ET.Element(f"{{{OMML_NS}}}radPr")
                ET.SubElement(props, f"{{{OMML_NS}}}degHide", {f"{{{OMML_NS}}}val": "1"})
                radical.insert(0, props)
                radical.insert(1, ET.Element(f"{{{OMML_NS}}}deg"))
        # Preserve column alignment for aligned equations and matrices.
        source_tables = list(ET.fromstring(mathml).iter(f"{{{MATHML_NS}}}mtable"))
        matrices = list(tree.iter(f"{{{OMML_NS}}}m"))
        if len(source_tables) != len(matrices):
            raise WordExportError("公式中的表格结构无法完整转换，请使用 LaTeX 格式。")
        for source, matrix in zip(source_tables, matrices):
            rows = list(matrix.findall(f"{{{OMML_NS}}}mr"))
            width = max((len(row.findall(f"{{{OMML_NS}}}e")) for row in rows), default=0)
            if not width:
                continue
            first_row = source.find(f"{{{MATHML_NS}}}mtr")
            cells = list(first_row) if first_row is not None else []
            default_align = source.get("columnalign", "center").split()
            props = ET.Element(f"{{{OMML_NS}}}mPr")
            columns = ET.SubElement(props, f"{{{OMML_NS}}}mcs")
            for index in range(width):
                align = cells[index].get("columnalign") if index < len(cells) else None
                align = align or default_align[min(index, len(default_align) - 1)]
                align = align if align in ("left", "center", "right") else "center"
                column = ET.SubElement(columns, f"{{{OMML_NS}}}mc")
                column_props = ET.SubElement(column, f"{{{OMML_NS}}}mcPr")
                ET.SubElement(column_props, f"{{{OMML_NS}}}count", {f"{{{OMML_NS}}}val": "1"})
                ET.SubElement(column_props, f"{{{OMML_NS}}}mcJc", {f"{{{OMML_NS}}}val": align})
            matrix.insert(0, props)
            for row in rows:
                while len(row.findall(f"{{{OMML_NS}}}e")) < width:
                    ET.SubElement(row, f"{{{OMML_NS}}}e")
        for text in tree.iter(f"{{{OMML_NS}}}t"):
            if text.text and (text.text[0].isspace() or text.text[-1].isspace()):
                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        return ET.tostring(tree, encoding="unicode")
    except WordExportError:
        raise
    except Exception as exc:
        raise WordExportError("这个公式暂时无法转换为 Word 原生公式。请保留 LaTeX，简化特殊排版后重试。") from exc


def build_clipboard_html(latex: str) -> str:
    """Return raw HTML containing MathML; caller supplies the CF_HTML header.

    Word desktop accepts MathML from an HTML clipboard in supported versions.
    Applications that ignore MathML should use the native DOCX export instead.
    """
    mathml = latex_to_mathml(latex)
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
            + mathml + '</body></html>')


def save_docx(latex: str, path: str) -> None:
    """Atomically save one editable Office Math equation in a minimal DOCX."""
    omml = latex_to_omml(latex)
    document = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{WORD_NS}" xmlns:m="{OMML_NS}">
<w:body><w:p><w:pPr><w:spacing w:after="160"/></w:pPr><m:oMathPara><m:oMathParaPr><m:jc m:val="center"/></m:oMathParaPr>{omml}</m:oMathPara></w:p>
<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr></w:body></w:document>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/></Types>'''
    relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
    doc_relationships = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/></Relationships>'''
    styles = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{WORD_NS}"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Cambria Math" w:hAnsi="Cambria Math" w:eastAsia="Microsoft YaHei"/><w:sz w:val="24"/></w:rPr></w:rPrDefault></w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>'''
    settings = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="{WORD_NS}" xmlns:m="{OMML_NS}"><m:mathPr><m:mathFont m:val="Cambria Math"/></m:mathPr></w:settings>'''
    destination = Path(path)
    if destination.suffix.lower() != ".docx":
        raise WordExportError("请将文件保存为 .docx 格式。")
    temporary = None
    try:
        candidate = str(destination.parent / (".formula-" + uuid4().hex + ".tmp"))
        with open(candidate, "xb") as handle:
            temporary = candidate
            with ZipFile(handle, "w", ZIP_DEFLATED) as archive:
                for name, data in (("[Content_Types].xml", content_types), ("_rels/.rels", relationships),
                                   ("word/document.xml", document), ("word/_rels/document.xml.rels", doc_relationships),
                                   ("word/styles.xml", styles), ("word/settings.xml", settings)):
                    archive.writestr(name, data.encode("utf-8"))
        os.replace(temporary, destination)
        temporary = None
    except OSError as exc:
        raise WordExportError("保存失败：请关闭正在打开的同名 Word 文档，或选择有写入权限的位置。") from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
