# parser_mapa.py
import re, fitz

HEADER_PATTERNS = {
    "numero_carga": re.compile(r"N[uú]mero da Carga:\s*(\d+)", re.IGNORECASE),
    "motorista": re.compile(r"Motorista:\s*(.+)"),
    "descricao_romaneio": re.compile(r"Desc\. Romaneio:\s*(.+)"),
    "peso_total": re.compile(r"Peso Total:\s*([\d\.,]+)"),
    "entregas": re.compile(r"Entregas:\s*(\d+)"),
    "data_emissao": re.compile(r"Data Emiss[aã]o:\s*([\d/]{10})", re.IGNORECASE),
}
RE_PEDIDOS_INICIO = re.compile(r"^Pedidos:\s*(.*)")
RE_GRUPO = re.compile(r"^([A-Z]{3}\d+)\s*-\s*(.+)$")
RE_ITEM = re.compile(
    r"""^(?:C/\s*(?P<pack_qtd>\d+)\s*(?P<pack_unid>[A-Z]+))?
        \s*(?P<fabricante>[A-Z0-9 \-&\.]+?)
        \s*(?P<codigo>\d{3,})
        \s*(?P<cod_barras>\d{8,14})
        \s*(?P<descricao>.*?)
        \s*(?P<qtd_unidades>\d+)\s*(?P<unidade>[A-Z]{2,3})\s*$""",
    re.VERBOSE
)

def extract_text_from_pdf(path_pdf: str) -> str:
    doc = fitz.open(path_pdf)
    texts = [page.get_text("text") for page in doc]
    doc.close()
    return "\n".join(texts)

def parse_header_and_pedidos(all_text: str):
    header = {}
    for k, rgx in HEADER_PATTERNS.items():
        m = rgx.search(all_text)
        if m:
            header[k] = m.group(1).strip()

    pedidos, cap, cap_on = [], [], False
    for line in all_text.splitlines():
        ls = line.strip()
        if not cap_on:
            m = RE_PEDIDOS_INICIO.match(ls)
            if m:
                cap_on = True
                cap.append(m.group(1))
        else:
            if ls.startswith("Fabric.") or RE_GRUPO.match(ls):
                break
            cap.append(ls)

    if cap:
        blob = " ".join(cap).replace("Pedidos:", " ")
        pedidos = re.findall(r"\d{3,}", blob)

    return header, pedidos

def parse_groups_and_items(all_text: str):
    grupos, itens, current_group = [], [], None
    for raw in all_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        mg = RE_GRUPO.match(line)
        if mg:
            current_group = {"codigo": mg.group(1), "titulo": mg.group(2)}
            grupos.append(current_group)
            continue
        mi = RE_ITEM.match(line)
        if mi and current_group:
            d = mi.groupdict()
            itens.append({
                "grupo_codigo": current_group["codigo"],
                "fabricante": (d.get("fabricante") or "").strip(),
                "codigo": (d.get("codigo") or "").strip(),
                "cod_barras": (d.get("cod_barras") or "").strip(),
                "descricao": (d.get("descricao") or "").strip(),
                "qtd_unidades": int(d.get("qtd_unidades") or 0),
                "unidade": (d.get("unidade") or "").strip(),
                "pack_qtd": int(d.get("pack_qtd") or 0),
                "pack_unid": (d.get("pack_unid") or "").strip(),
            })
    return grupos, itens

def parse_mapa(path_pdf: str):
    text = extract_text_from_pdf(path_pdf)
    header, pedidos = parse_header_and_pedidos(text)
    grupos, itens = parse_groups_and_items(text)
    if not header.get("numero_carga"):
        raise ValueError("Não encontrei 'Número da Carga' no PDF.")
    if not itens:
        raise ValueError("Não encontrei itens no PDF.")
    return header, pedidos, grupos, itens
