import fitz
import re
import json
import os

def extrator_finalissimo(caminho_do_pdf):
    """
    Versão finalíssima com a correção do "produto fantasma".
    """
    try:
        documento = fitz.open(caminho_do_pdf)
        pagina = documento[0]
        blocos = pagina.get_text("blocks", sort=True)
        linhas = [bloco[4].replace('\n', ' ').strip() for bloco in blocos]

        # --- Variáveis ---
        numero_pedido, nome_cliente, vendedor = "N/E", "N/E", "N/E"
        produtos = []

        # --- Extração do Cabeçalho e Vendedor ---
        for linha in linhas:
            if "Pedido:" in linha and "Cliente:" in linha:
                match_pedido = re.search(r"Pedido:\s*(\d+)", linha)
                if match_pedido: numero_pedido = match_pedido.group(1)
                
                match_cliente = re.search(r"Cliente:\s*(.*?)\s*Cond\.", linha)
                if match_cliente: nome_cliente = match_cliente.group(1).strip()
            
            if "Rua Minas Gerais" in linha and "Centro" in linha:
                match_vendedor = re.search(r'Rua Minas Gerais,\s*\d+\s+([A-Z]+)\s+Centro', linha)
                if match_vendedor:
                    vendedor = match_vendedor.group(1).strip()

        # --- Extração dos Produtos ---
        texto_produtos = ""
        capturando = False
        for linha in linhas:
            if "ITEM CÓD. BARRAS" in linha: capturando = True; continue
            if "TOTAL GERAL:" in linha: capturando = False; break
            if capturando: texto_produtos += " " + linha

        padrao_divisor = r'(?=\s*\d{1,2}\s+\d{13})'
        fatias_de_produto = [f for f in re.split(padrao_divisor, texto_produtos) if f.strip()]

        for fatia in fatias_de_produto:
            # ===== CORREÇÃO FINAL PARA O ITEM FANTASMA =====
            # Se a fatia não tiver um código de barras, não é um produto válido.
            if not re.search(r'\d{13}', fatia):
                continue
            # ===============================================

            match_nome = re.search(r"R\$\s*[\d,\.]+\s+R\$\s*[\d,\.]+\s+(.*)", fatia)
            nome = match_nome.group(1).strip() if match_nome else "Produto N/E"
            
            match_qtd = re.search(r"\d{13}\s+(.*?)\s+R\$", fatia)
            quantidade_pedida = match_qtd.group(1).strip() if match_qtd else "N/E"

            match_valor = re.findall(r"R\$\s*([\d,\.]+)", fatia)
            valor_total_item = match_valor[1] if len(match_valor) > 1 else "0.00"

            match_unidades = re.search(r"C/\s*(\d+)", fatia)
            unidades_pacote = int(match_unidades.group(1)) if match_unidades else 1
            
            if re.search(r'\d{13}', nome):
                nome = re.sub(r'\s*\d{1,2}\s+\d{13}.*$', '', nome).strip()

            produtos.append({
                "produto": nome, "quantidade_pedida": quantidade_pedida, "quantidade_entregue": None,
                "status": "Pendente", "valor_total_item": valor_total_item.replace(',', '.'),
                "unidades_pacote": unidades_pacote
            })
            
        documento.close()
        return { "numero_pedido": numero_pedido, "nome_cliente": nome_cliente, "vendedor": vendedor, "produtos": produtos, "status_conferencia": "Pendente" }
    except Exception as e:
        return {"erro": f"Ocorreu um erro: {e}"}

def salvar_no_banco_de_dados(dados_do_pedido, arquivo_db):
    if os.path.exists(arquivo_db):
        try:
            with open(arquivo_db, 'r', encoding='utf-8') as f: banco_de_dados = json.load(f)
        except json.JSONDecodeError: banco_de_dados = []
    else: banco_de_dados = []
    ids_existentes = {p['numero_pedido'] for p in banco_de_dados}
    if dados_do_pedido['numero_pedido'] not in ids_existentes:
        banco_de_dados.append(dados_do_pedido)
        with open(arquivo_db, 'w', encoding='utf-8') as f: json.dump(banco_de_dados, f, indent=4, ensure_ascii=False)
        return True
    return False

if __name__ == "__main__":
    arquivos_pdf = ["1.pdf", "2.pdf"]
    arquivo_db = "banco_de_dados.json"
    if os.path.exists(arquivo_db): os.remove(arquivo_db)
    print("Iniciando extração com a correção final do 'item fantasma'...")
    for pdf in arquivos_pdf:
        print(f"Lendo dados do arquivo: {pdf}...")
        dados = extrator_finalissimo(pdf)
        if "erro" not in dados and dados.get('produtos'):
            if salvar_no_banco_de_dados(dados, arquivo_db):
                print(f"Pedido {dados['numero_pedido']} salvo com sucesso!")
        else:
            print(f"Erro ou nenhum produto encontrado em {pdf}.")