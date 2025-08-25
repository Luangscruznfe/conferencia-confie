# parser_mapa.py  (versão flex)
import re, fitz

# Cabeçalho — aceita variações como "Nº da Carga", "Desc Romaneio" (com/sem ponto), etc.
HEADER_PATTERNS = {
    "numero_carga": re.compile(
        r"(?:N[uú]mero\s+da\s+Carga|Numero\s+da\s+Carga|N[º°]\s*da?\s*Carg[ae])\s*:\s*([A-Za-z0-9\-\/\.]+)",
        re.IGNORECASE
    ),
    "motorista": re.compile(r"Motorista\s*:\s*(.+)", re.IGNORECASE),
    "descricao_romaneio": re.compile(r"Desc\.?\s*Romaneio\s*:\s*(.+)", re.IGNORECASE),
    "peso_total": re.compile(r"Peso\s+Total\s*:\s*([\d\.,]+)", re.IGNORECASE),
    "entregas": re.compile(r"Entregas\s*:\s*(\d+)", re.IGNORECASE),
    "data_emissao": re.compile(r"Data(?:\s+de)?\s*Emiss[aã]o\s*:\s*([\d/]{8,10})", re.IGNORECASE),
}

RE_PEDIDOS_INICIO = re.compile(r"^Pedidos:\s*(.*)", re.IGNORECASE)
RE_GRUPO = re.compile(r"^([A-Z]{3}\d+)\s*-\s*(.+)$", re.IGNORECASE)

# Itens — mais flexível:
# - aceita linhas com ou sem "C/ 12UN"
# - fabricante com acentos
# - unidade com 1–4 letras (UN, CX, FD, CJ, DP, etc.)
# - descrição menos rígida
RE_ITEM = re.compile(
    r"""^(?:C/\s*(?P<pack_qtd>\d+)\s*(?P<pack_unid>[A-Z]+))?\s*           # prefixo opcional "C/ 12UN"
        (?P<fabricante>[A-Z0-9À-Ú\-&\. ]+?)\s+                             # fabricante (com acento)
        (?P<codigo>\d{3,})\s+                                             # código interno (3+ dígitos)
        (?P<cod_barras>\d{8,14})\s+                                       # EAN/GTIN (8–14 dígitos)
        (?P<descricao>.+?)\s+                                             # descrição
        (?P<qtd_unidades>\d+)\s*(?P<unidade>[A-Z]{1,4})\s*$               # quantidade e unidade
    """,
    re.VERBOSE | re.IGNORECASE
)

def extract_text_from_pdf(path_pdf: str) -> str:
    doc = fitz.open(path_pdf)
    texts = []
    for page in doc:
        # texto “linear”; se algum mapa vier com colunas, dá pra trocar por "blocks"/"words"
        texts.append(page.get_text("text"))
    doc.close()
    # normaliza espaços esquisitos
    return "\n".join(t.replace("\xa0", " ") for t in texts)

def parse_header_and_pedidos(all_text: str):
    header = {}
    for k, rgx in HEADER_PATTERNS.items():
        m = rgx.search(all_text)
        if m:
            header[k] = (m.group(1) or "").strip()

    pedidos, cap, cap_on = [], [], False
    for line in all_text.splitlines():
        ls = line.strip()
        if not cap_on:
            m = RE_PEDIDOS_INICIO.match(ls)
            if m:
                cap_on = True
                cap.append(m.group(1))
        else:
            if ls.lower().startswith("fabric.") or RE_GRUPO.match(ls):
                break
            cap.append(ls)

    if cap:
        blob = " ".join(cap).replace("Pedidos:", " ")
        # números de pedidos (3+ dígitos)
        pedidos = re.findall(r"\d{3,}", blob)

    return header, pedidos

def parse_groups_and_items(all_text: str):
    grupos, itens, current_group = [], [], None
    for raw in all_text.splitlines():
        line = " ".join(raw.strip().split())  # comprime espaços múltiplos
        if not line:
            continue

        mg = RE_GRUPO.match(line)
        if mg:
            current_group = {"codigo": mg.group(1).upper(), "titulo": mg.group(2).strip()}
            grupos.append(current_group)
            continue

        mi = RE_ITEM.match(line)
        if mi and current_group:
            d = mi.groupdict()
            # limpa cod_barras de possíveis espaços
            ean = (d.get("cod_barras") or "").replace(" ", "")
            item = {
                "grupo_codigo": current_group["codigo"],
                "fabricante": (d.get("fabricante") or "").strip().upper(),
                "codigo": (d.get("codigo") or "").strip(),
                "cod_barras": ean,
                "descricao": (d.get("descricao") or "").strip(),
                "qtd_unidades": int(d.get("qtd_unidades") or 0),
                "unidade": (d.get("unidade") or "").strip().upper(),
                "pack_qtd": int(d.get("pack_qtd") or 0),
                "pack_unid": (d.get("pack_unid") or "").strip().upper(),
            }
            itens.append(item)

    return grupos, itens

def parse_mapa(path_pdf: str):
    text = extract_text_from_pdf(path_pdf)
    header, pedidos = parse_header_and_pedidos(text)
    grupos, itens = parse_groups_and_items(text)

    # Fallback: tenta extrair número da carga do próprio nome do arquivo se faltar
    if not header.get("numero_carga"):
        fname = (path_pdf or "").split("/")[-1]
        m = re.search(r"(\d{4,})", fname)
        if m:
            header["numero_carga"] = m.group(1)

    if not header.get("numero_carga"):
        raise ValueError("Não encontrei 'Número da Carga' no PDF (tentei variações).")
    if not itens:
        raise ValueError("Não encontrei itens no PDF.")

    return header, pedidos, grupos, itens
