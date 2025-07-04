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

# =================================================================
# 2. CONFIGURAÇÃO DA APP FLASK
# =================================================================
app = Flask(__name__)
print("RODANDO ESTE APP:", __file__)

# =================================================================
# 3. FUNÇÕES AUXILIARES E DE BANCO DE DADOS
# =================================================================

def get_db_connection():
    """Cria e retorna uma conexão com o banco de dados PostgreSQL."""
    conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
    return conn

def init_db():
    """Garante que a tabela 'pedidos' exista no banco de dados com TODAS as colunas."""
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
    """Função auxiliar para extrair texto usando regex."""
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).replace('\n', ' ').strip() if match else "N/E"

def extrair_dados_do_pdf(nome_da_carga, nome_arquivo, stream=None, caminho_do_pdf=None):
    """Versão final que extrai dados de um PDF a partir de um arquivo ou stream."""
    try:
        if caminho_do_pdf:
            documento = fitz.open(caminho_do_pdf)
        elif stream:
            documento = fitz.open(stream=stream, filetype="pdf")
        else:
            return {"erro": "Nenhum arquivo ou stream de dados foi fornecido."}

        produtos_finais = []
        dados_cabecalho = {}

        for i, pagina in enumerate(documento):
            if i == 0:
                texto_completo_pagina = pagina.get_text("text")
                numero_pedido = extrair_campo_regex(r"Pedido:\s*(\d+)", texto_completo_pagina)
                if numero_pedido == "N/E": numero_pedido = extrair_campo_regex(r"Pedido\s+(\d+)", texto_completo_pagina)
                nome_cliente = extrair_campo_regex(r"Cliente:\s*(.*?)(?:\s*Cond\. Pgto:|\n)", texto_completo_pagina)
                vendedor = "N/E"
                try:
                    vendedor_rect_list = pagina.search_for("Vendedor")
                    if vendedor_rect_list:
                        vendedor_rect = vendedor_rect_list[0]
                        search_area = fitz.Rect(vendedor_rect.x0 - 15, vendedor_rect.y1, vendedor_rect.x1 + 15, vendedor_rect.y1 + 20)
                        vendedor_words = pagina.get_text("words", clip=search_area)
                        if vendedor_words: vendedor = vendedor_words[0][4]
                except Exception:
                    vendedor = extrair_campo_regex(r"Vendedor\s*([A-ZÀ-Ú]+)", texto_completo_pagina)
                dados_cabecalho = {"numero_pedido": numero_pedido, "nome_cliente": nome_cliente, "vendedor": vendedor}

            y_inicio, y_fim = 0, pagina.rect.height
            y_inicio_list = pagina.search_for("ITEM CÓD. BARRAS")
            if y_inicio_list: y_inicio = y_inicio_list[0].y1
            else: y_inicio = 50
            
            y_fim_list = pagina.search_for("TOTAL GERAL")
            if y_fim_list: y_fim = y_fim_list[0].y0
            else:
                footer_list = pagina.search_for("POR GENTILEZA CONFERIR")
                if footer_list: y_fim = footer_list[0].y0 - 5
            
            if y_inicio >= y_fim and y_fim != pagina.rect.height: continue

            palavras_na_tabela = [p for p in pagina.get_text("words") if p[1] > y_inicio and p[3] < y_fim]
            if not palavras_na_tabela: continue
            
            palavras_na_tabela.sort(key=lambda p: (p[1], p[0]))
            linhas_agrupadas = []
            if palavras_na_tabela:
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

            for linha in linhas_agrupadas:
                linha_texto = " ".join([palavra[4] for palavra in linha])
                
                if any(cabecalho in linha_texto.upper() for cabecalho in ['ITEM CÓD', 'DESCRIÇÃO', 'BARRAS']): continue
                partes = linha_texto.split()
                if len(partes) < 2: continue

                if partes[0].isdigit() and len(partes[0]) > 5:
                    nome_produto_final = " ".join(partes)
                else:
                    nome_produto_final = linha_texto

                quantidade_pedida, valor_total_item, unidades_pacote = "1", "0,00", 1
                
                if partes[-1].replace(',', '').replace('.', '').isdigit() and len(partes) > 1 and partes[-2].replace(',', '').replace('.', '').isdigit():
                    quantidade_pedida = partes[-2]
                    valor_total_item = partes[-1]
                elif partes[-1].isdigit() and len(partes) > 1:
                    quantidade_pedida = partes[-1]

                match_unidades = re.search(r'C/\s*(\d+)', nome_produto_final, re.IGNORECASE)
                if match_unidades: unidades_pacote = int(match_unidades.group(1))

                produtos_finais.append({"produto_nome": nome_produto_final, "quantidade_pedida": quantidade_pedida, "quantidade_entregue": None, "status": "Pendente", "valor_total_item": valor_total_item.replace(',', '.'), "unidades_pacote": unidades_pacote})

        documento.close()
        
        if not produtos_finais: 
            return {"erro": "Nenhum produto pôde ser extraído do PDF."}
        
        return {**dados_cabecalho, "produtos": produtos_finais, "status_conferencia": "Pendente", "nome_da_carga": nome_da_carga, "nome_arquivo": nome_arquivo}

    except Exception as e:
        import traceback
        return {"erro": f"Uma exceção crítica ocorreu na extração do PDF: {str(e)}\n{traceback.format_exc()}"}

def salvar_no_banco_de_dados(dados_do_pedido):
    """Salva um novo pedido no banco de dados PostgreSQL."""
    conn = get_db_connection()
    cur = conn.cursor()
    sql = """
        INSERT INTO pedidos (numero_pedido, nome_cliente, vendedor, nome_da_carga, nome_arquivo, status_conferencia, produtos, url_pdf)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (numero_pedido) DO NOTHING;
    """
    cur.execute(sql, (
        dados_do_pedido.get('numero_pedido'), dados_do_pedido.get('nome_cliente'), dados_do_pedido.get('vendedor'),
        dados_do_pedido.get('nome_da_carga'), dados_do_pedido.get('nome_arquivo'),
        dados_do_pedido.get('status_conferencia', 'Pendente'), json.dumps(dados_do_pedido.get('produtos', [])),
        dados_do_pedido.get('url_pdf')
    ))
    conn.commit()
    cur.close()
    conn.close()

# =================================================================
# 4. ROTAS DO SITE (ENDEREÇOS)
# =================================================================
# Roda a função para garantir que a tabela exista quando o app iniciar
init_db()

@app.route("/")
def pagina_inicial():
    return render_template('conferencia.html')

@app.route("/gestao")
def pagina_gestao():
    return render_template('gestao.html')

# ... (RESTO DAS SUAS ROTAS) ...
# Substitua suas rotas existentes pelas versões abaixo, se necessário.

@app.route('/api/upload/<nome_da_carga>', methods=['POST'])
def upload_files(nome_da_carga):
    if 'files[]' not in request.files:
        return jsonify({"sucesso": False, "erro": "Nenhum arquivo enviado."}), 400
    
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
    cur.execute("SELECT * FROM pedidos WHERE nome_da_carga = %s;", (nome_da_carga,))
    pedidos = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(pedidos)

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

# ... (O resto das suas rotas API que ainda usam DB_FILE precisam ser migradas) ...

# =================================================================
# 5. RODA O APP
# =================================================================
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)