# =================================================================
# 1. IMPORTAÇÕES
# =================================================================
from flask import Flask, jsonify, render_template, abort, request, Response
from parser_mapa import parse_mapa
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

    # === Tabela já existente (seu app atual) ===
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

    # === NOVO: Tabelas do Mapa de Separação ===
    cur.execute('''
        CREATE TABLE IF NOT EXISTS cargas (
          id SERIAL PRIMARY KEY,
          numero_carga TEXT UNIQUE NOT NULL,
          motorista TEXT,
          descricao_romaneio TEXT,
          peso_total NUMERIC,
          entregas INTEGER,
          data_emissao TEXT,
          criado_em TIMESTAMP DEFAULT NOW()
        );
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS carga_pedidos (
          id SERIAL PRIMARY KEY,
          numero_carga TEXT REFERENCES cargas(numero_carga) ON DELETE CASCADE,
          pedido_numero TEXT
        );
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS carga_grupos (
          id SERIAL PRIMARY KEY,
          numero_carga TEXT REFERENCES cargas(numero_carga) ON DELETE CASCADE,
          grupo_codigo TEXT,
          grupo_titulo TEXT
        );
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS carga_itens (
          id SERIAL PRIMARY KEY,
          numero_carga TEXT REFERENCES cargas(numero_carga) ON DELETE CASCADE,
          grupo_codigo TEXT,
          fabricante TEXT,
          codigo TEXT,
          cod_barras TEXT,
          descricao TEXT,
          qtd_unidades INTEGER,
          unidade TEXT,
          pack_qtd INTEGER,
          pack_unid TEXT,
          observacao TEXT DEFAULT '',
          separado BOOLEAN DEFAULT FALSE,
          forcar_conferido BOOLEAN DEFAULT FALSE,
          faltou BOOLEAN DEFAULT FALSE,
          sobrando INTEGER DEFAULT 0
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

                nome_cliente = extrair_campo_regex(r"Cliente:\s*(.*?)(?:\s*Cond\. Pgto:|\n)", texto_completo_pagina)

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
                            "unidades_pacote": unidades_pacote,
                            "forced_confirmed": False  # NOVO: começa falso
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
    if 'files[]' not in request.files: 
        return jsonify({"sucesso": False, "erro": "Nenhum arquivo enviado."}), 400
    files = request.files.getlist('files[]')
    erros, sucessos = [], 0
    for file in files:
        if file.filename == '': 
            continue
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
    if erros: 
        return jsonify({"sucesso": False, "erro": f"{sucessos} arquivo(s) processado(s). ERROS: {'; '.join(erros)}"})
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

# =====================  (ALTERADO)  =====================
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
        if not pedido: 
            return jsonify({"sucesso": False, "erro": "Pedido não encontrado."}), 404

        produtos_atualizados = pedido['produtos']
        todos_conferidos = True

        for produto in produtos_atualizados:
            if produto['produto_nome'] == dados_recebidos['produto_nome']:
                qtd_entregue_str = dados_recebidos['quantidade_entregue']
                produto['quantidade_entregue'] = qtd_entregue_str
                produto['observacao'] = dados_recebidos.get('observacao', '')

                # >>> NOVO: se estiver forçado, sempre fica Confirmado e não recalcula
                if bool(produto.get('forced_confirmed', False)):
                    status_final = "Confirmado"
                    produto['status'] = status_final
                    break

                # cálculo normal
                qtd_pedida_str = produto.get('quantidade_pedida', '0')
                unidades_pacote = int(produto.get('unidades_pacote', 1))
                match_pacotes = re.match(r'(\d+)', qtd_pedida_str)
                pacotes_pedidos = int(match_pacotes.group(1)) if match_pacotes else 0
                total_unidades_pedidas = pacotes_pedidos * unidades_pacote

                try:
                    qtd_entregue_int = int(qtd_entregue_str)
                    if qtd_entregue_int == total_unidades_pedidas: 
                        status_final = "Confirmado"
                    elif qtd_entregue_int == 0: 
                        status_final = "Corte Total"
                    else: 
                        status_final = "Corte Parcial"
                except (ValueError, TypeError): 
                    status_final = "Corte Parcial"

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
        if conn: 
            cur.close(); 
            conn.close()

# =====================  (NOVO)  =====================
@app.route('/api/item/force', methods=['POST'])
def force_item():
    """
    Alterna o 'forced_confirmed' do produto.
    Quando forçado: status = 'Confirmado'.
    Ao desfazer: status = 'Pendente' (o conferente decide depois).
    """
    dados = request.json
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pedidos WHERE numero_pedido = %s;", (dados['pedido_id'],))
        pedido = cur.fetchone()
        if not pedido:
            return jsonify({"sucesso": False, "erro": "Pedido não encontrado."}), 404

        produtos = pedido['produtos']
        novo_forced = None
        novo_status = None

        for produto in produtos:
            if produto.get('produto_nome') == dados.get('produto_nome'):
                atual = bool(produto.get('forced_confirmed', False))
                produto['forced_confirmed'] = not atual
                novo_forced = produto['forced_confirmed']
                if produto['forced_confirmed']:
                    produto['status'] = 'Confirmado'
                else:
                    # opcional: você pode recalcular aqui se quiser
                    produto['status'] = 'Pendente'
                novo_status = produto['status']
                break

        cur.execute("UPDATE pedidos SET produtos = %s WHERE numero_pedido = %s;",
                    (json.dumps(produtos), dados['pedido_id']))
        conn.commit()
        return jsonify({"sucesso": True, "forced_confirmed": novo_forced, "status": novo_status})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"sucesso": False, "erro": str(e)}), 500
    finally:
        if conn:
            cur.close(); conn.close()

@app.route('/api/cortes')
def api_cortes():
    # só inclui Corte Parcial/Total (itens confirmados — inclusive forçados — ficam de fora) :contentReference[oaicite:1]{index=1}
    cortes_agrupados = defaultdict(list)
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pedidos WHERE status_conferencia = 'Finalizado';")
        pedidos = cur.fetchall()
        for pedido in pedidos:
            produtos = pedido.get('produtos', []) if pedido.get('produtos') is not None else []
            if not isinstance(produtos, list): 
                continue
            nome_carga = pedido.get('nome_da_carga', 'Sem Carga')
            for produto in produtos:
                if produto.get('status') in ['Corte Parcial', 'Corte Total']:
                    cortes_agrupados[nome_carga].append({
                        "numero_pedido": pedido.get('numero_pedido'),
                        "nome_cliente": pedido.get('nome_cliente'),
                        "vendedor": pedido.get('vendedor'),
                        "observacao": produto.get('observacao', ''),
                        "produto": produto
                    })
        return jsonify(cortes_agrupados)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"erro": str(e)}), 500
    finally:
        if conn: 
            cur.close(); conn.close()

@app.route('/api/gerar-relatorio')
def gerar_relatorio():
    # idem: só considera Corte Parcial/Total (confirmados/forçados não entram) :contentReference[oaicite:2]{index=2}
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pedidos;")
        pedidos = cur.fetchall()
        if not pedidos: 
            return "Nenhum pedido encontrado para gerar o relatório.", 404

        dados_para_excel = []
        for pedido in pedidos:
            produtos = pedido.get('produtos', []) if pedido.get('produtos') is not None else []
            if not isinstance(produtos, list): 
                continue
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

                        dados_para_excel.append({
                            'Pedido': pedido.get('numero_pedido'),
                            'Cliente': pedido.get('nome_cliente'),
                            'Vendedor': pedido.get('vendedor'),
                            'Produto': produto.get('produto_nome', ''),
                            'Quantidade Pedida': produto.get('quantidade_pedida', ''),
                            'Quantidade Entregue': produto.get('quantidade_entregue', ''),
                            'Status': produto.get('status', ''),
                            'Observação': produto.get('observacao', ''),
                            'Valor Total Item': produto.get('valor_total_item'),
                            'Valor do Corte Estimado': round(valor_corte, 2)
                        })
                    except (ValueError, TypeError, AttributeError) as e:
                        print(f"Erro ao calcular corte para o produto {produto.get('produto_nome', 'N/A')}: {e}")
                        continue

        if not dados_para_excel: 
            return "Nenhum item com corte encontrado para gerar o relatório."

        df = pd.DataFrame(dados_para_excel)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Cortes')
        return Response(
            output.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment;filename=cortes_relatorio.xlsx"}
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return f"Erro ao gerar relatório: {e}", 500
    finally:
        if conn: 
            cur.close(); conn.close()

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
        if conn: 
            cur.close(); conn.close()

@app.route('/mapa/upload', methods=['GET', 'POST'])
def mapa_upload():
    if request.method == 'GET':
        return '''
        <form method="post" enctype="multipart/form-data" style="padding:20px">
          <h3>Upload do Mapa de Separação (PDF)</h3>
          <input type="file" name="pdf" accept="application/pdf" required />
          <button type="submit">Enviar</button>
        </form>
        '''

    f = request.files.get('pdf')
    if not f:
        return "Envie um PDF", 400

    path_tmp = f"/tmp/{f.filename}"
    f.save(path_tmp)

        try:
        header, pedidos_map, grupos, itens = parse_mapa(path_tmp)
    except Exception as e:
        # retorna o erro detalhado pro front
        return (f"Erro ao ler mapa: {str(e)}", 400)

    conn = get_db_connection()
    cur = conn.cursor()

    # UPSERT da carga
    cur.execute("""
        INSERT INTO cargas (numero_carga, motorista, descricao_romaneio, peso_total, entregas, data_emissao)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (numero_carga) DO UPDATE SET
            motorista=EXCLUDED.motorista,
            descricao_romaneio=EXCLUDED.descricao_romaneio,
            peso_total=EXCLUDED.peso_total,
            entregas=EXCLUDED.entregas,
            data_emissao=EXCLUDED.data_emissao
    """, (
        header.get("numero_carga"),
        header.get("motorista"),
        header.get("descricao_romaneio"),
        str(header.get("peso_total") or "").replace('.', '').replace(',', '.'),
        int(header.get("entregas") or 0),
        header.get("data_emissao"),
    ))

    # Sincroniza tabelas filhas
    cur.execute("DELETE FROM carga_pedidos WHERE numero_carga=%s", (header["numero_carga"],))
    cur.execute("DELETE FROM carga_grupos  WHERE numero_carga=%s", (header["numero_carga"],))
    cur.execute("DELETE FROM carga_itens   WHERE numero_carga=%s", (header["numero_carga"],))

    for p in pedidos_map:
        cur.execute("INSERT INTO carga_pedidos (numero_carga, pedido_numero) VALUES (%s,%s)",
                    (header["numero_carga"], p))

    for g in grupos:
        cur.execute("""
            INSERT INTO carga_grupos (numero_carga, grupo_codigo, grupo_titulo)
            VALUES (%s,%s,%s)
        """, (header["numero_carga"], g["codigo"], g["titulo"]))

    for it in itens:
        cur.execute("""
            INSERT INTO carga_itens
                (numero_carga, grupo_codigo, fabricante, codigo, cod_barras, descricao,
                 qtd_unidades, unidade, pack_qtd, pack_unid)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            header["numero_carga"], it["grupo_codigo"], it["fabricante"], it["codigo"],
            it["cod_barras"], it["descricao"], it["qtd_unidades"], it["unidade"],
            it["pack_qtd"], it["pack_unid"]
        ))

    conn.commit()
    cur.close(); conn.close()

    return jsonify({
        "ok": True,
        "numero_carga": header["numero_carga"],
        "pedidos": pedidos_map,
        "grupos": len(grupos),
        "itens": len(itens)
    })

@app.route('/api/mapas')
def api_mapas():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT numero_carga, motorista, data_emissao, criado_em
        FROM cargas
        ORDER BY criado_em DESC
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify([
        {"numero_carga": r[0], "motorista": r[1], "data_emissao": r[2],
         "criado_em": r[3].isoformat() if r[3] else None}
    for r in rows])

@app.route('/mapa')
def mapa_lista():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT numero_carga, motorista, data_emissao
        FROM cargas
        ORDER BY criado_em DESC
    """)
    mapas = cur.fetchall()
    cur.close(); conn.close()

    html = ['''<!DOCTYPE html><html lang="pt-br"><head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mapas</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    </head><body class="bg-dark text-light"><div class="container mt-4">
    <nav class="mb-3">
      <a class="btn btn-outline-light me-2" href="/conferencia">Conferência</a>
      <a class="btn btn-outline-light me-2" href="/gestao">Gestão</a>
      <a class="btn btn-warning" href="/mapa/upload">Importar novo mapa</a>
    </nav>
    <h2 class="mb-3">🗺️ Mapas de Separação</h2>
    <p class="text-secondary">Escolha um mapa para iniciar a separação.</p>
    <div class="list-group">''']
    if mapas:
        for num, mot, data in mapas:
            html.append(f'''
              <a class="list-group-item list-group-item-action d-flex justify-content-between align-items-center bg-dark text-light"
                 href="/mapa/{num}">
                <div>
                  <div class="fw-bold">{num}</div>
                  <small class="text-secondary">Motorista: {mot or '-'} | Emissão: {data or '-'}</small>
                </div>
                <span class="bi bi-chevron-right"></span>
              </a>''')
    else:
        html.append('''<div class="alert alert-secondary">Nenhum mapa importado ainda.
        Use a aba <b>Gestão</b> para subir um PDF.</div>''')
    html.append('</div></div></body></html>')
    return ''.join(html)

# ========== MAPA: APIs de listagem e atualização (NOVO) ==========

@app.route('/api/mapa/<numero_carga>')
def api_mapa_detalhe(numero_carga):
    """Retorna grupos e itens da carga, para montar a tela de separação."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # grupos
    cur.execute("""
        SELECT grupo_codigo, grupo_titulo
        FROM carga_grupos
        WHERE numero_carga = %s
        ORDER BY grupo_codigo;
    """, (numero_carga,))
    grupos = cur.fetchall()

    # itens
    cur.execute("""
        SELECT id, grupo_codigo, fabricante, codigo, cod_barras, descricao,
               qtd_unidades, unidade, pack_qtd, pack_unid,
               observacao, separado, forcar_conferido, faltou, sobrando
        FROM carga_itens
        WHERE numero_carga = %s
        ORDER BY grupo_codigo, descricao;
    """, (numero_carga,))
    itens = cur.fetchall()

    cur.close(); conn.close()
    return jsonify({"grupos": grupos, "itens": itens})


@app.route('/api/mapa/item/atualizar', methods=['POST'])
def api_mapa_item_atualizar():
    """Atualiza flags do item (separado, faltou, forçado), observação e sobrando."""
    data = request.json or {}
    item_id = data.get('item_id')
    if not item_id:
        return jsonify({"ok": False, "erro": "item_id é obrigatório"}), 400

    campos = {
        "separado": bool(data.get('separado', False)),
        "faltou": bool(data.get('faltou', False)),
        "forcar_conferido": bool(data.get('forcar_conferido', False)),
        "observacao": data.get('observacao', '') or '',
        "sobrando": int(data.get('sobrando') or 0)
    }

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE carga_itens
           SET separado=%s, faltou=%s, forcar_conferido=%s,
               observacao=%s, sobrando=%s
         WHERE id=%s
    """, (campos["separado"], campos["faltou"], campos["forcar_conferido"],
          campos["observacao"], campos["sobrando"], item_id))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"ok": True})


@app.route('/api/mapa/grupo/marcar', methods=['POST'])
def api_mapa_grupo_marcar():
    """Marca/Desmarca um grupo inteiro como 'separado' (checkbox em massa)."""
    data = request.json or {}
    numero_carga = data.get('numero_carga')
    grupo_codigo = data.get('grupo_codigo')
    separado = bool(data.get('separado', True))
    if not numero_carga or not grupo_codigo:
        return jsonify({"ok": False, "erro": "numero_carga e grupo_codigo obrigatórios"}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE carga_itens
           SET separado=%s
         WHERE numero_carga=%s AND grupo_codigo=%s
    """, (separado, numero_carga, grupo_codigo))
    afetados = cur.rowcount
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"ok": True, "itens_afetados": afetados})

@app.route('/mapa/<numero_carga>')
def mapa_detalhe(numero_carga):
    """Tela de separação por grupos/itens, com checkboxes e ações."""
    # HTML simples, carrega dados via /api/mapa/<numero_carga> e monta a UI no front
    html = f"""
    <!DOCTYPE html><html lang="pt-br"><head>
      <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Mapa {numero_carga}</title>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
      <style>
        body {{ background:#0f1115; color:#e8e8e8; }}
        .card {{ background:#161a22; border-color:#263043; }}
        .tag {{ font-size:.75rem; opacity:.8; }}
        .item-row.separado {{ background:rgba(40,167,69,.12); }}
        .item-row.faltou {{ background:rgba(220,53,69,.12); }}
        .item-row.forcado {{ outline:1px dashed #ffc107; }}
        .sticky-top-bar {{ position:sticky; top:0; z-index:1020; background:#0f1115; padding:.75rem 0; }}
        .search-input::placeholder {{ color:#9aa3b2; }}
        .hover-row:hover {{ background:#1b2130; }}
        .small-mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }}
      </style>
    </head><body>
      <div class="container py-3">
        <nav class="mb-3">
          <a class="btn btn-outline-light me-2" href="/mapa">← Mapas</a>
          <a class="btn btn-outline-light me-2" href="/gestao">Gestão</a>
          <a class="btn btn-outline-light" href="/conferencia">Conferência</a>
        </nav>

        <div class="sticky-top-bar">
          <h3 class="mb-2">Mapa <span class="text-info">{numero_carga}</span></h3>
          <div class="row g-2">
            <div class="col-md-6">
              <input id="busca" class="form-control search-input" placeholder="Buscar por código, EAN ou descrição..." />
            </div>
            <div class="col-md-6 text-md-end">
              <span id="resumo" class="text-secondary"></span>
            </div>
          </div>
        </div>

        <div id="grupos" class="mt-3"></div>
      </div>

      <script>
      const NUMERO_CARGA = {json.dumps(numero_carga)};
      let STATE = {{ grupos: [], itens: [] }};

      function badge(txt, cls) {{
        return `<span class="badge ${'{'}cls{'}'} ms-1 tag">${'{'}txt{'}'}</span>`;
      }}

      function pintaLinha(it) {{
        let cls = "item-row hover-row";
        if (it.separado) cls += " separado";
        if (it.faltou) cls += " faltou";
        if (it.forcar_conferido) cls += " forcado";
        return cls;
      }}

      function render() {{
        const wrap = document.getElementById('grupos');
        const q = (document.getElementById('busca').value || '').toLowerCase().trim();
        let total = 0, marcados = 0;

        let html = '';
        for (const g of STATE.grupos) {{
          const items = STATE.itens.filter(x => x.grupo_codigo === g.grupo_codigo)
                                   .filter(x => !q || (String(x.codigo||'').includes(q) ||
                                                       String(x.cod_barras||'').includes(q) ||
                                                       String(x.descricao||'').toLowerCase().includes(q)));
          if (!items.length) continue;

          // header do grupo
          html += `
            <div class="card mb-3">
              <div class="card-header d-flex justify-content-between align-items-center">
                <div><strong>${'{'}g.grupo_codigo{'}'}</strong> — ${'{'}g.grupo_titulo{'}'}</div>
                <div class="d-flex gap-2">
                  <button class="btn btn-sm btn-success" onclick="marcarGrupo('${'{'}g.grupo_codigo{'}'}', true)">Marcar grupo</button>
                  <button class="btn btn-sm btn-outline-light" onclick="marcarGrupo('${'{'}g.grupo_codigo{'}'}', false)">Desmarcar</button>
                </div>
              </div>
              <div class="list-group list-group-flush">`;

          // itens do grupo
          for (const it of items) {{
            total++;
            if (it.separado) marcados++;
            html += `
              <div class="list-group-item ${'{'}pintaLinha(it){'}'}">
                <div class="d-flex flex-column flex-md-row justify-content-between gap-2">
                  <div class="flex-grow-1">
                    <div class="fw-semibold">${'{'}it.descricao || ''{'}'}</div>
                    <div class="text-secondary small">
                      <span class="small-mono">Cód: ${'{'}it.codigo || '-'{'}'}</span> ·
                      <span class="small-mono">EAN: ${'{'}it.cod_barras || '-'{'}'}</span> ·
                      <span>${'{'}(it.fabricante||'').toUpperCase(){'}'}</span>
                      ${'{'}it.forcar_conferido ? badge('FORÇADO', 'bg-warning text-dark') : ''{'}'}
                      ${'{'}it.faltou ? badge('FALTOU', 'bg-danger') : ''{'}'}
                      ${'{'}it.separado ? badge('SEPARADO', 'bg-success') : ''{'}'}
                    </div>
                    <div class="text-secondary small mt-1">
                      Pedido: <span class="small-mono">${'{'}it.qtd_unidades{'}'} ${'{'}it.unidade || ''{'}'}${'{'}it.pack_qtd ? ' (C/ '+it.pack_qtd+' '+(it.pack_unid||'')+')' : ''{'}'}</span>
                    </div>
                  </div>
                  <div class="d-flex flex-column align-items-start align-items-md-end gap-2">
                    <div class="form-check">
                      <input class="form-check-input" type="checkbox" ${'{'}it.separado ? 'checked' : ''{'}'}
                             onchange="toggleItem(${ '{'}it.id{'}' }, {{separado: this.checked}})">
                      <label class="form-check-label">Separado</label>
                    </div>
                    <div class="form-check">
                      <input class="form-check-input" type="checkbox" ${'{'}it.faltou ? 'checked' : ''{'}'}
                             onchange="toggleItem(${ '{'}it.id{'}' }, {{faltou: this.checked}})">
                      <label class="form-check-label">Faltou</label>
                    </div>
                    <div class="form-check">
                      <input class="form-check-input" type="checkbox" ${'{'}it.forcar_conferido ? 'checked' : ''{'}'}
                             onchange="toggleItem(${ '{'}it.id{'}' }, {{forcar_conferido: this.checked}})">
                      <label class="form-check-label">Forçar conferido</label>
                    </div>
                    <div class="input-group input-group-sm">
                      <span class="input-group-text">Sobrando</span>
                      <input type="number" class="form-control" value="${'{'}it.sobrando || 0{'}'}"
                             onchange="toggleItem(${ '{'}it.id{'}' }, {{sobrando: parseInt(this.value||0)}})">
                    </div>
                    <div class="input-group input-group-sm">
                      <span class="input-group-text">Obs</span>
                      <input type="text" class="form-control" value="${'{'}it.observacao || ''{'}'}"
                             onchange="toggleItem(${ '{'}it.id{'}' }, {{observacao: this.value}})">
                    </div>
                  </div>
                </div>
              </div>`;
          }}

          html += `</div></div>`;
        }}
        wrap.innerHTML = html || '<div class="alert alert-secondary">Nenhum item para exibir.</div>';
        document.getElementById('resumo').textContent = total ? (marcados + '/' + total + ' itens marcados') : '';
      }}

      async function carregar() {{
        const r = await fetch('/api/mapa/' + encodeURIComponent(NUMERO_CARGA));
        const data = await r.json();
        STATE.grupos = data.grupos || [];
        STATE.itens  = data.itens  || [];
        render();
      }}

      async function toggleItem(id, patch) {{
        // Atualiza o objeto local
        const idx = STATE.itens.findIndex(x => x.id === id);
        if (idx >= 0) Object.assign(STATE.itens[idx], patch);
        render();
        // Persiste no servidor
        const body = Object.assign({{ item_id: id }}, patch);
        await fetch('/api/mapa/item/atualizar', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(body)
        }});
      }}

      async function marcarGrupo(grupo, flag) {{
        // Otimista: marca no cliente
        for (const it of STATE.itens) if (it.grupo_codigo === grupo) it.separado = !!flag;
        render();
        await fetch('/api/mapa/grupo/marcar', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ numero_carga: NUMERO_CARGA, grupo_codigo: grupo, separado: !!flag }})
        }});
      }}

      document.getElementById('busca').addEventListener('input', render);
      carregar();
      </script>
    </body></html>
    """
    return html

