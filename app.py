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
from zipfile import ZipFile
import io
import pandas as pd
import fitz
import re
import sys
import logging



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


def extrair_dados_do_pdf(stream, nome_da_carga, nome_arquivo):
    try:
        import fitz
        import re
        documento = fitz.open(stream=stream, filetype="pdf")
        produtos_finais = []
        dados_cabecalho = {}
        
        inicio_extracao = False

        for i, pagina in enumerate(documento):
            if i == 0:
                def extrair_campo_regex(pattern, text):
                    match = re.search(pattern, text, re.DOTALL)
                    return match.group(1).replace('\n', ' ').strip() if match else "N/E"

                texto_completo_pagina = pagina.get_text("text")
                numero_pedido = extrair_campo_regex(r"Pedido:\s*(\d+)", texto_completo_pagina)
                if numero_pedido == "N/E":
                    numero_pedido = extrair_campo_regex(r"Pedido\s+(\d+)", texto_completo_pagina)

                nome_cliente = extrair_campo_regex(r"Cliente:\s*(.*?)(?:\s*Cond\\. Pgto:|\n)", texto_completo_pagina)

                vendedor = "N/E"
                try:
                    vendedor_rect_list = pagina.search_for("Vendedor")
                    if vendedor_rect_list:
                        vendedor_rect = vendedor_rect_list[0]
                        search_area = fitz.Rect(
                            vendedor_rect.x0 - 15,
                            vendedor_rect.y1,
                            vendedor_rect.x1 + 15,
                            vendedor_rect.y1 + 20
                        )
                        vendedor_words = pagina.get_text("words", clip=search_area)
                        if vendedor_words:
                            vendedor = vendedor_words[0][4]
                except Exception:
                    vendedor = extrair_campo_regex(r"Vendedor\s*([A-ZÀ-Ú]+)", texto_completo_pagina)

                dados_cabecalho = {
                    "numero_pedido": numero_pedido,
                    "nome_cliente": nome_cliente,
                    "vendedor": vendedor
                }

            palavras_na_tabela = pagina.get_text("words")
            if not palavras_na_tabela:
                continue

            palavras_na_tabela.sort(key=lambda p: (p[1], p[0]))

            linhas_agrupadas = []
            linha_atual = [palavras_na_tabela[0]]
            y_referencia = palavras_na_tabela[0][1]
            for j in range(1, len(palavras_na_tabela)):
                palavra = palavras_na_tabela[j]
                if abs(palavra[1] - y_referencia) < 5:
                    linha_atual.append(palavra)
                else:
                    linhas_agrupadas.append(sorted(linha_atual, key=lambda p: p[0]))
                    linha_atual = [palavra]
                    y_referencia = palavra[1]
            linhas_agrupadas.append(sorted(linha_atual, key=lambda p: p[0]))

            for palavras_linha in linhas_agrupadas:
                texto_linha = " ".join([p[4] for p in palavras_linha])
                if "ITEM CÓD. BARRAS" in texto_linha:
                    inicio_extracao = True
                    continue
                elif "**POR GENTILEZA" in texto_linha:
                    inicio_extracao = False
                    continue
                if not inicio_extracao:
                    continue

                product_chunks = []
                current_chunk = []
                if len(palavras_linha) > 1 and palavras_linha[0][4].isdigit() and len(palavras_linha[0][4]) <= 2:
                    current_chunk.append(palavras_linha[0])
                    for k in range(1, len(palavras_linha)):
                        word_info = palavras_linha[k]
                        word_text = word_info[4]
                        is_start_of_new_product = False
                        if (
                            word_text.isdigit()
                            and len(word_text) <= 2
                            and k + 1 < len(palavras_linha)
                            and palavras_linha[k + 1][4].isdigit()
                            and len(palavras_linha[k + 1][4]) > 5
                        ):
                            is_start_of_new_product = True
                        if is_start_of_new_product:
                            product_chunks.append(current_chunk)
                            current_chunk = []
                        current_chunk.append(word_info)
                    product_chunks.append(current_chunk)
                else:
                    product_chunks.append(palavras_linha)

                for chunk in product_chunks:
                    nome_produto_parts = []
                    quantidade_parts = []
                    valores_parts = []

                    for x0, y0, x1, y1, palavra, *_ in chunk:
                        if x0 < 340:
                            nome_produto_parts.append(palavra)
                        elif x0 < 450:
                            quantidade_parts.append(palavra)
                        else:
                            valores_parts.append(palavra)

                    if not nome_produto_parts:
                        continue

                    if (
                        len(nome_produto_parts) > 2
                        and nome_produto_parts[0].isdigit()
                        and len(nome_produto_parts[0]) <= 2
                    ):
                        nome_produto_final = " ".join(nome_produto_parts[1:])
                    else:
                        nome_produto_final = " ".join(nome_produto_parts)

                    quantidade_completa_str = " ".join(quantidade_parts)

                    valor_total_item = "0.00"
                    if valores_parts:
                        match_valor = re.search(r'[\d,.]+', valores_parts[-1])
                        if match_valor:
                            valor_total_item = match_valor.group(0)

                    unidades_pacote = 1
                    match_unidades = re.search(r'C/\s*(\d+)', quantidade_completa_str, re.IGNORECASE)
                    if match_unidades:
                        unidades_pacote = int(match_unidades.group(1))

                    if nome_produto_final and quantidade_completa_str:
                        produtos_finais.append({
                            "produto_nome": nome_produto_final,
                            "quantidade_pedida": quantidade_completa_str,
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
        return {"erro": f"Erro na extração do PDF: {str(e)}\n{traceback.format_exc()}"}



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
    cur.execute("SELECT * FROM pedidos WHERE nome_da_carga = %s ORDER BY id DESC;", (nome_da_carga,))
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

@app.route('/api/upload-zip/<nome_da_carga>', methods=['POST'])
def upload_zip(nome_da_carga):
    if 'file' not in request.files:
        return jsonify({"erro": "Nenhum arquivo ZIP enviado."}), 400

    zip_file = request.files['file']
    if not zip_file.filename.endswith('.zip'):
        return jsonify({"erro": "Formato inválido. Envie um arquivo .zip."}), 400

    try:
        with ZipFile(zip_file) as zip_ref:
            arquivos_pdf = [f for f in zip_ref.namelist() if f.lower().endswith('.pdf')]
            if not arquivos_pdf:
                return jsonify({"erro": "Nenhum PDF encontrado dentro do ZIP."}), 400

            sucessos, erros = 0, []

            for nome_pdf in arquivos_pdf:
                with zip_ref.open(nome_pdf) as pdf_file:
                    pdf_bytes = pdf_file.read()
                    dados = extrair_dados_do_pdf(stream=pdf_bytes, nome_da_carga=nome_da_carga, nome_arquivo=nome_pdf)
                    if "erro" in dados:
                        erros.append(f"{nome_pdf}: {dados['erro']}")
                        continue
                    upload_result = cloudinary.uploader.upload(pdf_bytes, resource_type="raw", public_id=f"pedidos/{nome_pdf}")
                    dados['url_pdf'] = upload_result['secure_url']
                    salvar_no_banco_de_dados(dados)
                    sucessos += 1

            if erros:
                return jsonify({"sucesso": False, "mensagem": f"{sucessos} PDF(s) processado(s).", "erros": erros})

            return jsonify({"sucesso": True, "mensagem": f"{sucessos} PDF(s) processado(s) com sucesso."})
    except Exception as e:
        import traceback
        return jsonify({"erro": f"Erro ao processar ZIP: {str(e)}\n{traceback.format_exc()}"}), 500

# =================================================================
# 5. RODA O APP
# =================================================================
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)

