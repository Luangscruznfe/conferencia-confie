# parser_mapa.py (versão robusta)
import re, fitz


# Unidades de picking aceitas (quantidade separada)
PICK_UNIDADES = {"UN","CX","FD","CJ","DP","PC","PT","DZ","SC","KT","JG","BF","PA"}
# Unidades de peso/volume que NÃO são picking (vão ficar na descrição)
WEIGHT_UNIDADES = {"G","KG","ML","L"}


# =========================
# 1) Cabeçalho (tolerante)
# =========================
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

# =========================
# 2) Padrões de itens
# =========================
# Padrão “completo” (com EAN), aceita "C/ 12UN" opcional
RE_ITEM = re.compile(
    r"""^(?:C/\s*(?P<pack_qtd>\d+)\s*(?P<pack_unid>[A-Z]+))?\s*     # prefixo opcional "C/ 12UN"
        (?P<fabricante>[A-Z0-9À-Ú\-&\. ]+?)\s+                      # fabricante (com acento)
        (?P<codigo>\d{3,})\s+                                      # código interno (3+ dígitos)
        (?P<cod_barras>\d{8,14})\s+                                # EAN/GTIN (8–14 dígitos)
        (?P<descricao>.+?)\s+                                      # descrição
        (?P<qtd_unidades>\d+)\s*(?P<unidade>[A-Z]{1,4})\s*$        # quantidade e unidade
    """,
    re.VERBOSE | re.IGNORECASE
)

# Padrão “flex” (quando falta EAN ou fabricante, ou a ordem muda um pouco)
# Exemplos que esse padrão cobre:
#   "C/ 12UN 24916  BARRA SUCRILHOS CHOCOLATE  1 DP"
#   "KELLANOVA 24916 BARRA... 1 DP" (sem EAN)
#   "24916 7896004004495 BARRA... 1 DP" (sem fabricante)
RE_ITEM_FLEX = re.compile(
    r"""^(?:C/\s*(?P<pack_qtd>\d+)\s*(?P<pack_unid>[A-Z]+))?\s*
        (?:(?P<fabricante>[A-Z0-9À-Ú\-&\. ]+?)\s+)?                 # fabricante opcional
        (?P<codigo>\d{3,})\s+                                      # código
        (?:(?P<cod_barras>\d{8,14})\s+)?                           # EAN opcional
        (?P<descricao>.+?)\s+                                      # descrição
        (?P<qtd_unidades>\d+)\s*(?P<unidade>[A-Z]{1,4})\s*$        # qtd/unidade
    """,
    re.VERBOSE | re.IGNORECASE
)

# =========================
# 3) Extração robusta
# =========================
def extract_text_from_pdf(path_pdf: str) -> str:
    """
    1) Tenta 'text' normal.
    2) Se vier pouco texto, reconstrói por 'words' (ordena por y/x e agrupa por linha).
    3) Se ainda curto, tenta 'blocks'.
    """
    doc = fitz.open(path_pdf)
    pages_text = []

    def rebuild_from_words(page):
        words = page.get_text("words")  # [x0,y0,x1,y1,"texto",block,line,word]
        if not words:
            return ""
        words.sort(key=lambda w: (round(w[1], 1), w[0]))  # y, depois x
        lines, current_y, buf = [], None, []
        for w in words:
            y = round(w[1], 1)
            if current_y is None:
                current_y = y
            if abs(y - current_y) > 1.5:  # nova linha
                if buf:
                    lines.append(" ".join(buf))
                buf = []
                current_y = y
            buf.append(w[4])
        if buf:
            lines.append(" ".join(buf))
        return "\n".join(lines)

    for page in doc:
        t = (page.get_text("text") or "").replace("\xa0", " ").strip()
        if len(t) < 50:
            t = rebuild_from_words(page)
        if len(t) < 50:
            try:
                blocks = page.get_text("blocks") or []
                t = "\n".join((b[4] or "").strip() for b in blocks if len(b) >= 5)
            except:
                pass
        pages_text.append(t)
    doc.close()
    return "\n".join(pages_text)

# =========================
# 4) Parse do cabeçalho/pedidos
# =========================
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
        pedidos = re.findall(r"\d{3,}", blob)
    return header, pedidos

# =========================
# 5) Parse de grupos/itens
# =========================
def _match_item(line: str):
    """Tenta casar a linha com os padrões de item (com fallback)."""
    line_compact = " ".join(line.strip().split())
    m = RE_ITEM.match(line_compact)
    if not m:
        m = RE_ITEM_FLEX.match(line_compact)
    return m

def parse_groups_and_items(all_text: str):
    grupos, itens, current_group = [], [], None
    # linhas não vazias, “comprimidas”
    lines = [ " ".join(l.strip().split()) for l in all_text.splitlines() if l.strip() ]
    i = 0
    while i < len(lines):
        line = lines[i]

        # Grupo
        mg = RE_GRUPO.match(line)
        if mg:
            current_group = {"codigo": mg.group(1).upper(), "titulo": mg.group(2).strip()}
            grupos.append(current_group)
            i += 1
            continue

        # Item: usa o parser inteligente (que já ignora G/KG/ML/L)
        if current_group:
            parsed = try_parse_line(line)
            if not parsed and i + 1 < len(lines):
                # quebra de descrição? tenta juntar com a próxima
                joined = f"{line} {lines[i+1]}"
                parsed = try_parse_line(joined)
                if parsed:
                    i += 1  # consumiu a próxima também

            if parsed:
                parsed["grupo_codigo"] = current_group["codigo"]
                itens.append(parsed)

        i += 1

    return grupos, itens


# =========================
# 6) Orquestração
# =========================
def parse_mapa(path_pdf: str):
    text = extract_text_from_pdf(path_pdf)
    header, pedidos = parse_header_and_pedidos(text)
    grupos, itens = parse_groups_and_items(text)

    # Fallback: tenta extrair número da carga do nome do arquivo
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



def _strip_pack_suffix(s: str):
    m = re.search(r"\sC/\s*(\d+)\s*([A-Z]+)\s*$", s, re.IGNORECASE)
    if not m:
        return s, 0, ""
    return s[:m.start()].rstrip(), int(m.group(1)), (m.group(2) or "").upper()

def _strip_qty_unit(s: str):
    # Só aceita como QTD/UN se UN estiver em PICK_UNIDADES
    m = re.search(r"\s(\d+)\s*([A-Z]{1,4})\s*$", s)
    if not m:
        return s, 0, ""
    un = (m.group(2) or "").upper()
    if un not in PICK_UNIDADES:
        return s, 0, ""
    return s[:m.start()].rstrip(), int(m.group(1)), un

def try_parse_line(line: str):
    """
    Tenta parsear UMA linha de item.
    Formatos aceitos:
      - 'EAN DESCRICAO COD FAB QTD UN [C/ PACK]'
      - 'DESCRICAO COD FAB QTD UN [C/ PACK]'
      - variações com/sem EAN/fabricante
    """
    s = " ".join((line or "").strip().split())
    if not s:
        return None

    # 1) tira pack do final, se houver
    s, pack_qtd, pack_unid = _strip_pack_suffix(s)

    # 2) tira QTD/UN do final (somente se UN ∈ PICK_UNIDADES)
    s, qtd, un = _strip_qty_unit(s)

    # 3) tenta COD + FAB no final
    m = re.search(r"\s(\d{3,})\s+([A-Z0-9À-Ú\-\&\. ]+)$", s)
    codigo = ""; fabricante = ""
    if m:
        codigo = m.group(1)
        fabricante = (m.group(2) or "").strip().upper()
        s = s[:m.start()].rstrip()
    else:
        # tenta só COD no final
        m = re.search(r"\s(\d{3,})\s*$", s)
        if m:
            codigo = m.group(1)
            s = s[:m.start()].rstrip()

    # 4) EAN no começo OU no meio
    cod_barras = ""
    m = re.match(r"^(\d{8,14})\s+(.*)$", s)
    if m:
        cod_barras = m.group(1)
        s = m.group(2)
    if not cod_barras:
        mm = re.search(r"\b(\d{8,14})\b", s)
        if mm:
            cod_barras = mm.group(1)
            s = (s[:mm.start()] + " " + s[mm.end():]).strip()
            s = re.sub(r"\s{2,}", " ", s)

    descricao = s.strip()
    if not descricao:
        return None

    return {
        "fabricante": fabricante,
        "codigo": codigo,
        "cod_barras": cod_barras,
        "descricao": descricao,
        "qtd_unidades": qtd,
        "unidade": un,
        "pack_qtd": pack_qtd,
        "pack_unid": pack_unid,
    }



def debug_extrator(path_pdf: str):
    """Devolve as linhas cruas e como cada uma foi parseada (ou não)."""
    from parser_mapa import extract_text_from_pdf  # evita import circular se mover
    raw = extract_text_from_pdf(path_pdf)
    lines = [l for l in raw.splitlines()]
    out = []
    for i, ln in enumerate(lines, 1):
        parsed = try_parse_line(ln)
        out.append({"n": i, "line": ln, "parsed": parsed})
    return out
