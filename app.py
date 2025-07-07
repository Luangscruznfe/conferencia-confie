# =================================================================
# 1. IMPORTAÇÕES
# =================================================================
from flask import Flask, jsonify, render_template, abort, request, Response
import cloudinary, cloudinary.uploader, cloudinary.api
import psycopg2, psycopg2.extras
import json, os, re, io, fitz, shutil, requests
from werkzeug.utils import secure_filename
from collections import defaultdict
from datetime import datetime
import pandas as pd

# =================================================================
# 2. CONFIGURAÇÃO DA APP FLASK
# =================================================================
app = Flask(__name__)
print("RODANDO ESTE APP:", __file__)

# =================================================================
# 3. FUNÇÕES AUXILIARES E DE BANCO DE DADOS
# =================================================================

def get_db_connection():
    conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS pedidos (
            id SERIAL PRIMARY KEY,
            numero_pedido TEXT UNIQUE NOT NULL,
            nome_cliente TEXT,
            vendedor TEXT,
            nome_da_carga TEXT,
            nome_arquivo TEXT,
            status_conferencia TEXT,
            produtos JSONB,
            url_pdf TEXT
        );
    ''')
    conn.commit()
    cur.close()
    conn.close()

def extrair_campo_regex(pattern, text):
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).replace('\n', ' ').strip() if match else "N/E"

def extrair_dados_do_pdf(nome_da_carga, nome_arquivo, stream=None, caminho_do_pdf=None):
    """
    Versão específica para o formato do PDF analisado - extrai quantidades corretamente.
    """
    try:
        if caminho_do_pdf:
            documento = fitz.open(caminho_do_pdf)
        elif stream:
            documento = fitz.open(stream=stream, filetype="pdf")
        else:
            return {"erro": "Nenhum arquivo ou stream de dados foi fornecido."}

        # --- Extração de Cabeçalho (Mantida, pois já está robusta) ---
        dados_cabecalho = {}
        pagina_um = documento[0]
        texto_completo_pagina = pagina_um.get_text("text")
        numero_pedido = extrair_campo_regex(r"Pedido:\s*(\d+)", texto_completo_pagina)
        nome_cliente = "N/E"
        try:
            search_list = pagina_um.search_for("Nome Fant.:")
            if search_list:
                rect = search_list[0]
                search_area = fitz.Rect(rect.x1, rect.y0, pagina_um.rect.width - 20, rect.y1 + 5)
                nome_cliente = pagina_um.get_text("text", clip=search_area).strip()
        except Exception: pass
        if nome_cliente == "N/E":
             try:
                search_list = pagina_um.search_for("Cliente:")
                if len(search_list) > 1:
                    rect = search_list[1]
                    search_area = fitz.Rect(rect.x1, rect.y0, pagina_um.rect.width - 20, rect.y1 + 5)
                    nome_cliente = pagina_um.get_text("text", clip=search_area).strip().split('\n')[0]
             except Exception: pass
        vendedor = "N/E"
        try:
            search_list = pagina_um.search_for("Vendedor")
            if search_list:
                rect = search_list[0]
                search_area = fitz.Rect(rect.x0 - 20, rect.y1, rect.x1 + 80, rect.y1 + 20)
                vendedor_words = pagina_um.get_text("words", clip=search_area)
                if vendedor_words: vendedor = sorted(vendedor_words, key=lambda w: w[0])[0][4]
        except Exception: pass
        dados_cabecalho = {"numero_pedido": numero_pedido, "nome_cliente": nome_cliente, "vendedor": vendedor}

        # ETAPA 1: Abordagem de Força Bruta para garantir a leitura de todas as páginas
        todas_as_palavras = []
        for page in documento:
            todas_as_palavras.extend(page.get_text("words"))
        
        texto_completo = " ".join([w[4] for w in todas_as_palavras])
        
        # ETAPA 2: Parser específico para o formato do PDF
        produtos_finais = []
        # Limpa o rodapé antes de começar
        if "TOTAL GERAL:" in texto_completo:
            texto_completo = texto_completo.split("TOTAL GERAL:")[0]

        # Divide por linhas de produto usando o padrão: ITEM + CÓDIGO_BARRAS
        produtos_brutos = re.split(r'(?=\d{1,3}\s+\d{12,14})', texto_completo)

        for produto_str in produtos_brutos:
            linha = produto_str.strip()
            # Filtra lixo e linhas que não são produtos
            if not re.match(r'^\d{1,3}\s+\d{12,14}', linha):
                continue

            # Analisando o padrão específico do PDF:
            # ITEM CÓD_BARRAS [C/XXUN] QUANTIDADE UNIDADE R$ VALOR_UNIT R$ VALOR_TOTAL NOME_PRODUTO
            
            # Extrai partes básicas
            match_inicial = re.match(r'^(\d+)\s+(\d{12,14})\s+(.*)', linha)
            if not match_inicial: continue
            
            item_id = match_inicial.group(1)
            codigo_barras = match_inicial.group(2)
            resto = match_inicial.group(3)
            
            # Remove valores monetários
            precos = re.findall(r'R\$\s*[\d,.]+', resto)
            valor_total_item = "0.00"
            if precos:
                valor_total_item = precos[-1].replace('R$', '').strip()
            
            # Remove todos os preços
            sem_precos = re.sub(r'R\$\s*[\d,.]+', '', resto).strip()
            
            # Processa a linha baseado no padrão específico observado
            quantidade_pedida = "1"
            unidades_pacote = 1
            nome_produto_final = ""
            
            # Padrão 1: Linha com informação de embalagem no início (ex: "C/ 21UN 1 DP CHICLE TRIDENT...")
            match_embalagem_inicio = re.match(r'^C/\s*(\d+)UN\s+(\d+)\s+(DP|FD|CJ|UN|CX|PC|ED)\s+(.*)', sem_precos, re.IGNORECASE)
            if match_embalagem_inicio:
                unidades_pacote = int(match_embalagem_inicio.group(1))
                quantidade_pedida = match_embalagem_inicio.group(2)
                unidade = match_embalagem_inicio.group(3)
                nome_produto_final = match_embalagem_inicio.group(4)
            else:
                # Padrão 2: Linha sem informação de embalagem no início (ex: "1 UN BALA 7 BELO...")
                match_qtd_simples = re.match(r'^(\d+)\s+(UN|CX|PC|FD|DP|CJ|ED)\s+(.*)', sem_precos, re.IGNORECASE)
                if match_qtd_simples:
                    quantidade_pedida = match_qtd_simples.group(1)
                    unidade = match_qtd_simples.group(2)
                    nome_produto_final = match_qtd_simples.group(3)
                else:
                    # Padrão 3: Linha com embalagem no meio/final (ex: "CHICLE POOSH HORTELA 200G C/40")
                    # Primeiro extrai informação de embalagem se existir
                    match_embalagem_meio = re.search(r'C/\s*(\d+)(?:UN)?\b', sem_precos, re.IGNORECASE)
                    if match_embalagem_meio:
                        unidades_pacote = int(match_embalagem_meio.group(1))
                        # Remove a informação de embalagem
                        sem_embalagem = re.sub(r'C/\s*\d+(?:UN)?\b', '', sem_precos, flags=re.IGNORECASE).strip()
                    else:
                        sem_embalagem = sem_precos
                    
                    # Procura quantidade no início da string limpa
                    match_qtd_inicio = re.match(r'^(\d+)\s+(UN|CX|PC|FD|DP|CJ|ED)\s+(.*)', sem_embalagem, re.IGNORECASE)
                    if match_qtd_inicio:
                        quantidade_pedida = match_qtd_inicio.group(1)
                        nome_produto_final = match_qtd_inicio.group(3)
                    else:
                        # Se não encontrar quantidade específica, assume 1 e pega todo o texto como nome
                        quantidade_pedida = "1"
                        # Remove possíveis unidades soltas no início
                        nome_produto_final = re.sub(r'^(UN|CX|PC|FD|DP|CJ|ED)\s+', '', sem_embalagem, flags=re.IGNORECASE).strip()
                        if not nome_produto_final:
                            nome_produto_final = sem_embalagem
            
            # Limpa o nome do produto
            nome_produto_final = nome_produto_final.strip()
            
            # Verifica se o nome é válido
            if len(nome_produto_final) < 3:
                continue

            # Formata a quantidade para exibição
            quantidade_display = f"{quantidade_pedida} UN"
            
            produtos_finais.append({
                "produto_nome": nome_produto_final,
                "quantidade_pedida": quantidade_display,
                "quantidade_entregue": None,
                "status": "Pendente",
                "valor_total_item": valor_total_item.replace(',', '.'),
                "unidades_pacote": unidades_pacote
            })

        documento.close()
        
        if not produtos_finais: 
            return {"erro": "Nenhum produto pôde ser extraído do PDF."}
        
        return {
            **dados_cabecalho,
            "produtos": produtos_finais,
            "status_conferencia": "Pendente",
            "nome_da_carga": nome_da_carga,
            "nome_arquivo": nome_arquivo
        }

    except Exception as e:
        import traceback
        return {"erro": f"Uma exceção crítica na extração do PDF: {str(e)}\n{traceback.format_exc()}"}

def salvar_no_banco_de_dados(dados_do_pedido):
    """Salva um novo pedido no banco de dados PostgreSQL."""
    conn = get_db_connection()
    cur = conn.cursor()
    sql = "INSERT INTO pedidos (numero_pedido, nome_cliente, vendedor, nome_da_carga, nome_arquivo, status_conferencia, produtos, url_pdf) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (numero_pedido) DO NOTHING;"
    cur.execute(sql, (dados_do_pedido.get('numero_pedido'), dados_do_pedido.get('nome_cliente'), dados_do_pedido.get('vendedor'), dados_do_pedido.get('nome_da_carga'), dados_do_pedido.get('nome_arquivo'), dados_do_pedido.get('status_conferencia', 'Pendente'), json.dumps(dados_do_pedido.get('produtos', [])), dados_do_pedido.get('url_pdf')))
    conn.commit()
    cur.close()
    conn.close()

# =================================================================
# 4. ROTAS DO SITE (ENDEREÇOS)
# =================================================================
init_db()

@app.route("/")
def pagina_inicial():
    return render_template('conferencia.html')

@app.route("/conferencia")
def pagina_conferencia():
    return render_template('conferencia.html')

@app.route("/gestao")
def pagina_gestao():
    return render_template('gestao.html')

@app.route('/conferencia/<nome_da_carga>')
def pagina_lista_pedidos(nome_da_carga):
    return render_template('lista_pedidos.html', nome_da_carga=nome_da_carga)

@app.route("/pedido/<pedido_id>")
def detalhe_pedido(pedido_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM pedidos WHERE numero_pedido = %s;", (pedido_id,))
    pedido_encontrado = cur.fetchone()
    cur.close()
    conn.close()
    if pedido_encontrado:
        return render_template('detalhe_pedido.html', pedido=pedido_encontrado)
    return "Pedido não encontrado", 404

# --- ROTAS DE API ---

@app.route('/api/upload/<nome_da_carga>', methods=['POST'])
def upload_files(nome_da_carga):
    if 'files[]' not in request.files: return jsonify({"sucesso": False, "erro": "Nenhum arquivo enviado."}), 400
    files = request.files.getlist('files[]')
    erros, sucessos = [], 0
    for file in files:
        if file.filename == '': continue
        filename = secure_filename(file.filename)
        try:
            pdf_bytes = file.read()
            dados_extraidos = extrair_dados_do_pdf(nome_da_carga=nome_da_carga, nome_arquivo=filename, stream=pdf_bytes)
            if "erro" in dados_extraidos:
                erros.append(f"Arquivo '{filename}': {dados_extraidos['erro']}")
                continue
            upload_result = cloudinary.uploader.upload(pdf_bytes, resource_type="raw", public_id=f"pedidos/{filename}")
            dados_extraidos['url_pdf'] = upload_result['secure_url']
            salvar_no_banco_de_dados(dados_extraidos)
            sucessos += 1
        except Exception as e:
            import traceback
            erros.append(f"Arquivo '{filename}': Falha inesperada no processamento. {traceback.format_exc()}")
    if erros: return jsonify({"sucesso": False, "erro": f"{sucessos} arquivo(s) processado(s). ERROS: {'; '.join(erros)}"})
    return jsonify({"sucesso": True, "mensagem": f"Todos os {sucessos} arquivo(s) da carga '{nome_da_carga}' foram processados."})

@app.route('/api/cargas')
def api_cargas():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT nome_da_carga FROM pedidos WHERE nome_da_carga IS NOT NULL ORDER BY nome_da_carga;")
    cargas = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(cargas)

@app.route('/api/pedidos/<nome_da_carga>')
def api_pedidos_por_carga(nome_da_carga):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM pedidos WHERE nome_da_carga = %s;", (nome_da_carga,))
    pedidos = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(pedidos)

@app.route('/api/item/update', methods=['POST'])
def update_item_status():
    dados_recebidos = request.json
    status_final = "Erro"
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pedidos WHERE numero_pedido = %s;", (dados_recebidos['pedido_id'],))
        pedido = cur.fetchone()
        if not pedido: return jsonify({"sucesso": False, "erro": "Pedido não encontrado."}), 404
        produtos_atualizados = pedido['produtos']
        todos_conferidos = True
        for produto in produtos_atualizados:
            if produto['produto_nome'] == dados_recebidos['produto_nome']:
                qtd_entregue_str = dados_recebidos['quantidade_entregue']
                produto['quantidade_entregue'] = qtd_entregue_str
                produto['observacao'] = dados_recebidos.get('observacao', '')
                qtd_pedida_str = produto.get('quantidade_pedida', '0')
                unidades_pacote = int(produto.get('unidades_pacote', 1))
                match_pacotes = re.match(r'(\d+)', qtd_pedida_str)
                pacotes_pedidos = int(match_pacotes.group(1)) if match_pacotes else 0
                total_unidades_pedidas = pacotes_pedidos * unidades_pacote
                try:
                    qtd_entregue_int = int(qtd_entregue_str)
                    if qtd_entregue_int == total_unidades_pedidas: status_final = "Confirmado"
                    elif qtd_entregue_int == 0: status_final = "Corte Total"
                    else: status_final = "Corte Parcial"
                except (ValueError, TypeError): status_final = "Corte Parcial"
                produto['status'] = status_final
                break
        for produto in produtos_atualizados:
            if produto['status'] == 'Pendente':
                todos_conferidos = False
                break
        novo_status_conferencia = 'Finalizado' if todos_conferidos else 'Pendente'
        sql_update = "UPDATE pedidos SET produtos = %s, status_conferencia = %s WHERE numero_pedido = %s;"
        cur.execute(sql_update, (json.dumps(produtos_atualizados), novo_status_conferencia, dados_recebidos['pedido_id']))
        conn.commit()
        return jsonify({"sucesso": True, "status_final": status_final})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"sucesso": False, "erro": str(e)}), 500
    finally:
        if conn: cur.close(); conn.close()

@app.route('/api/cortes')
def api_cortes():
    cortes_agrupados = defaultdict(list)
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pedidos WHERE status_conferencia = 'Finalizado';")
        pedidos = cur.fetchall()
        for pedido in pedidos:
            produtos = pedido.get('produtos', []) if pedido.get('produtos') is not None else []
            if not isinstance(produtos, list): continue
            nome_carga = pedido.get('nome_da_carga', 'Sem Carga')
            for produto in produtos:
                if produto.get('status') in ['Corte Parcial', 'Corte Total']:
                    item_corte = {"numero_pedido": pedido.get('numero_pedido'), "nome_cliente": pedido.get('nome_cliente'), "vendedor": pedido.get('vendedor'), "observacao": produto.get('observacao', ''), "produto": produto}
                    cortes_agrupados[nome_carga].append(item_corte)
        return jsonify(cortes_agrupados)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"erro": str(e)}), 500
    finally:
        if conn: cur.close(); conn.close()
        
@app.route('/api/gerar-relatorio')
def gerar_relatorio():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pedidos;")
        pedidos = cur.fetchall()
        if not pedidos: return "Nenhum pedido encontrado para gerar o relatório.", 404
        dados_para_excel = []
        for pedido in pedidos:
            produtos = pedido.get('produtos', []) if pedido.get('produtos') is not None else []
            if not isinstance(produtos, list): continue
            for produto in produtos:
                if produto.get('status') in ['Corte Parcial', 'Corte Total']:
                    try:
                        valor_total = float(str(produto.get('valor_total_item', '0')).replace(',', '.'))
                        unidades_pacote = int(produto.get('unidades_pacote', 1))
                        qtd_pedida_str = produto.get('quantidade_pedida', '0')
                        match = re.match(r'(\d+)', qtd_pedida_str)
                        pacotes_pedidos = int(match.group(1)) if match else 0
                        preco_por_pacote = valor_total / pacotes_pedidos if pacotes_pedidos > 0 else 0
                        preco_unidade = preco_por_pacote / unidades_pacote if unidades_pacote > 0 else 0
                        unidades_pedidas = pacotes_pedidos * unidades_pacote
                        qtd_entregue_str = str(produto.get('quantidade_entregue', '0'))
                        unidades_entregues = int(qtd_entregue_str) if qtd_entregue_str.isdigit() else 0
                        valor_corte = (unidades_pedidas - unidades_entregues) * preco_unidade
                        dados_para_excel.append({'Pedido': pedido.get('numero_pedido'), 'Cliente': pedido.get('nome_cliente'), 'Vendedor': pedido.get('vendedor'), 'Produto': produto.get('produto_nome', ''), 'Quantidade Pedida': produto.get('quantidade_pedida', ''), 'Quantidade Entregue': produto.get('quantidade_entregue', ''), 'Status': produto.get('status', ''), 'Observação': produto.get('observacao', ''), 'Valor Total Item': produto.get('valor_total_item'), 'Valor do Corte Estimado': round(valor_corte, 2)})
                    except (ValueError, TypeError, AttributeError) as e:
                        print(f"Erro ao calcular corte para o produto {produto.get('produto_nome', 'N/A')}: {e}")
                        continue
        if not dados_para_excel: return "Nenhum item com corte encontrado para gerar o relatório."
        df = pd.DataFrame(dados_para_excel)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Cortes')
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment;filename=cortes_relatorio.xlsx"})
    except Exception as e:
        import traceback; traceback.print_exc()
        return f"Erro ao gerar relatório: {e}", 500
    finally:
        if conn: cur.close(); conn.close()

@app.route('/api/resetar-dia', methods=['POST'])
def resetar_dia():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE pedidos RESTART IDENTITY;")
        conn.commit()
        return jsonify({"sucesso": True, "mensagem": "Todos os pedidos foram apagados. O sistema está pronto para um novo dia."})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"sucesso": False, "erro": str(e)}), 500
    finally:
        if conn: cur.close(); conn.close()

# =================================================================
# 5. RODA O APP
# =================================================================
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)
