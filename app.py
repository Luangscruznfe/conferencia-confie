# 1. IMPORTAÇÕES
# =================================================================
from flask import Flask, jsonify, render_template, abort, request, Response, send_file
import cloudinary
import cloudinary.uploader
import psycopg2
import psycopg2.extras
import json
import os
import re
import pandas as pd
import io
import fitz  # PyMuPDF
from werkzeug.utils import secure_filename
from collections import defaultdict
from datetime import datetime
import shutil

# =================================================================
# CONFIGURAÇÃO DO CLOUDINARY
# =================================================================
cloudinary.config(
    cloud_name="dse1cruh5",
    api_key="513345832743713",
    api_secret="1bELqY5yvRXc6qyRqLn8jpGc228",
    secure=True
)


# =================================================================
# 2. CONFIGURAÇÃO DA APP FLASK
# =================================================================
app = Flask(__name__)
DB_FILE = "banco_de_dados.json"
UPLOAD_FOLDER = 'uploads'
BACKUP_FOLDER = 'backups'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(BACKUP_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# =================================================================
# 3. FUNÇÕES AUXILIARES
# =================================================================
print("RODANDO ESTE APP:", __file__)

def get_db_connection():
    # A Render automaticamente coloca a URL na variável de ambiente
    conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
    return conn

# Função para criar a tabela de pedidos
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # Crie a tabela se ela não existir
    cur.execute('''
        CREATE TABLE IF NOT EXISTS pedidos (
            id SERIAL PRIMARY KEY,
            numero_pedido TEXT UNIQUE NOT NULL,
            nome_cliente TEXT,
            vendedor TEXT,
            nome_da_carga TEXT,
            nome_arquivo TEXT,
            status_conferencia TEXT,
            produtos JSONB
        );
    ''')
    conn.commit()
    cur.close()
    conn.close()

# Chame esta função uma vez no início do seu app para garantir que a tabela exista
init_db()

def criar_backup():
    """
    Cria uma cópia de segurança do ficheiro de dados atual.
    """
    if not os.path.exists(DB_FILE):
        return
    try:
        if os.path.getsize(DB_FILE) > 0:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                banco_de_dados = json.load(f)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(BACKUP_FOLDER, f"backup_{timestamp}.json")
            
            with open(backup_path, 'w', encoding='utf-8') as f_bkp:
                json.dump(banco_de_dados, f_bkp, indent=4, ensure_ascii=False)
            
            backups = sorted([f for f in os.listdir(BACKUP_FOLDER) if f.startswith("backup_") and f.endswith(".json")])
            while len(backups) > 20:
                os.remove(os.path.join(BACKUP_FOLDER, backups.pop(0)))
    except (json.JSONDecodeError, FileNotFoundError):
        return

def extrair_dados_do_pdf(nome_da_carga, caminho_do_pdf=None, stream=None):
    """
    Versão final com todas as correções de extração.
    Pode ler de um caminho de arquivo ou de um stream de bytes.
    """
    try:
        # Bloco 1: Abrir o documento (já estava correto)
        if caminho_do_pdf:
            documento = fitz.open(caminho_do_pdf)
        elif stream:
            documento = fitz.open(stream=stream, filetype="pdf")
        else:
            return {"erro": "Nenhum arquivo ou stream de dados foi fornecido."}

        # Bloco 2: Lógica de Extração (agora com a indentação correta, dentro do 'try')
        produtos_finais = []
        dados_cabecalho = {}

        for i, pagina in enumerate(documento):
            if i == 0:
                def extrair_campo_regex(pattern, text):
                    match = re.search(pattern, text, re.DOTALL)
                    return match.group(1).replace('\n', ' ').strip() if match else "N/E"
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

            X_COLUNA_PRODUTO_FIM, X_COLUNA_QUANTIDADE_FIM = 340, 450
            palavras_na_tabela = [p for p in pagina.get_text("words") if p[1] > y_inicio and p[3] < y_fim]
            if not palavras_na_tabela: continue
            
            palavras_na_tabela.sort(key=lambda p: (p[1], p[0]))
            linhas_agrupadas = []
            if palavras_na_tabela:
                linha_atual = [palavras_na_tabela[0]]
                y_referencia = palavras_na_tabela[0][1]
                for j in range(1, len(palavras_na_tabela)):
                    palavra = palavras_na_tabela[j]
                    if abs(palavra[1] - y_referencia) < 5: linha_atual.append(palavra)
                    else:
                        linhas_agrupadas.append(sorted(linha_atual, key=lambda p: p[0]))
                        linha_atual = [palavra]
                        y_referencia = palavra[1]
                linhas_agrupadas.append(sorted(linha_atual, key=lambda p: p[0]))

            for palavras_linha in linhas_agrupadas:
                product_chunks, current_chunk, start_index = [], [], 0
                if palavras_linha:
                    if len(palavras_linha) > 1 and palavras_linha[0][4].isdigit() and len(palavras_linha[0][4]) <= 2:
                        current_chunk.append(palavras_linha[0])
                        for k in range(1, len(palavras_linha)):
                            word_info, word_text = palavras_linha[k], palavras_linha[k][4]
                            is_start_of_new_product = False
                            if word_text.isdigit() and len(word_text) <= 2 and k + 1 < len(palavras_linha) and palavras_linha[k+1][4].isdigit() and len(palavras_linha[k+1][4]) > 5:
                                is_start_of_new_product = True
                            if is_start_of_new_product:
                                product_chunks.append(current_chunk)
                                current_chunk = []
                            current_chunk.append(word_info)
                        product_chunks.append(current_chunk)
                    else: product_chunks.append(palavras_linha)
                
                for chunk in product_chunks:
                    nome_produto_parts, quantidade_parts, valores_parts = [], [], []
                    for x0, y0, x1, y1, palavra, _, _, _ in chunk:
                        if x0 < X_COLUNA_PRODUTO_FIM: nome_produto_parts.append(palavra)
                        elif x0 < X_COLUNA_QUANTIDADE_FIM: quantidade_parts.append(palavra)
                        else: valores_parts.append(palavra)
                    
                    if not nome_produto_parts: continue
                    if len(nome_produto_parts) > 1 and nome_produto_parts[0].isdigit() and (len(nome_produto_parts[0]) <= 2 or nome_produto_parts[1].isdigit()):
                        nome_produto_final = " ".join(nome_produto_parts[2:]) if len(nome_produto_parts) > 2 else " ".join(nome_produto_parts[1:])
                    else: nome_produto_final = " ".join(nome_produto_parts)
                    
                    quantidade_completa_str = " ".join(quantidade_parts)
                    valor_total_item = "0.00"
                    if valores_parts:
                        match_valor = re.search(r'[\d,.]+', valores_parts[-1])
                        if match_valor: valor_total_item = match_valor.group(0)
                    unidades_pacote = 1
                    match_unidades = re.search(r'C/\s*(\d+)', quantidade_completa_str, re.IGNORECASE)
                    if match_unidades: unidades_pacote = int(match_unidades.group(1))
                    
                    if nome_produto_final and quantidade_completa_str:
                        produtos_finais.append({"produto_nome": nome_produto_final, "quantidade_pedida": quantidade_completa_str,"quantidade_entregue": None, "status": "Pendente","valor_total_item": valor_total_item.replace(',', '.'),"unidades_pacote": unidades_pacote})
        
        documento.close()
        if not produtos_finais: return {"erro": "Nenhum produto pôde ser extraído do PDF."}
        
        # O 'nome_arquivo' agora precisa ser tratado de forma diferente,
        # pois não temos mais o caminho do arquivo quando usamos stream.
        # Vamos deixar isso para a rota de upload decidir.
        return {**dados_cabecalho, "produtos": produtos_finais}

    except Exception as e:
        import traceback
        return {"erro": f"Uma exceção crítica ocorreu na extração do PDF: {str(e)}\n{traceback.format_exc()}"}

def salvar_no_banco_de_dados(dados_do_pedido):
    """Salva um novo pedido no banco de dados PostgreSQL."""
    conn = get_db_connection()
    cur = conn.cursor()

    # A instrução SQL para inserir um novo pedido.
    # ON CONFLICT (numero_pedido) DO NOTHING evita erros se tentarmos inserir um pedido duplicado.
    sql = """
        INSERT INTO pedidos (
            numero_pedido, nome_cliente, vendedor, nome_da_carga, 
            nome_arquivo, status_conferencia, produtos, url_pdf
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (numero_pedido) DO NOTHING;
    """

    # Executa o comando, passando os dados de forma segura
    cur.execute(sql, (
        dados_do_pedido.get('numero_pedido'),
        dados_do_pedido.get('nome_cliente'),
        dados_do_pedido.get('vendedor'),
        dados_do_pedido.get('nome_da_carga'),
        dados_do_pedido.get('nome_arquivo'),
        dados_do_pedido.get('status_conferencia', 'Pendente'),
        json.dumps(dados_do_pedido.get('produtos', [])),  # Converte a lista de produtos para texto JSON
        dados_do_pedido.get('url_pdf') # Será None por enquanto
    ))

    conn.commit() # Salva a transação
    cur.close()
    conn.close()

    # Por enquanto, vamos desativar o backup antigo para não dar erro
    # criar_backup()

# =================================================================
# FUNÇÕES DE BANCO DE DADOS POSTGRESQL
# =================================================================
def get_db_connection():
    """Cria e retorna uma conexão com o banco de dados PostgreSQL."""
    # A Render injeta a URL do banco de dados nesta variável de ambiente
    database_url = os.environ.get('DATABASE_URL')
    if database_url is None:
        raise ValueError("A variável de ambiente DATABASE_URL não foi encontrada.")
    conn = psycopg2.connect(database_url)
    return conn

def init_db():
    """Garante que a tabela 'pedidos' exista no banco de dados."""
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

# Roda a função para garantir que a tabela exista quando o app iniciar
init_db()

# =================================================================
# 4. ROTAS DO SITE (ENDEREÇOS)
# =================================================================
@app.route("/")
def pagina_inicial():
    return render_template('conferencia.html') # <--- CÓDIGO CORRETO

@app.route("/conferencia")
def pagina_conferencia(): return render_template('conferencia.html')

@app.route("/gestao")
def pagina_gestao():
    return render_template('gestao.html')


@app.route('/conferencia/<nome_da_carga>')
def pagina_lista_pedidos(nome_da_carga): return render_template('lista_pedidos.html', nome_da_carga=nome_da_carga)

@app.route("/pedido/<pedido_id>")
def detalhe_pedido(pedido_id):
    conn = get_db_connection()
    # Usamos RealDictCursor para que o resultado venha como um dicionário
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Busca um único pedido no banco de dados pelo número
    cur.execute("SELECT * FROM pedidos WHERE numero_pedido = %s;", (pedido_id,))
    pedido_encontrado = cur.fetchone() # Pega apenas o primeiro resultado

    cur.close()
    conn.close()

    if pedido_encontrado:
        return render_template('detalhe_pedido.html', pedido=pedido_encontrado)

    return "Pedido não encontrado", 404

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
            # 1. Envia o arquivo para o Cloudinary
            print(f"Fazendo upload do arquivo '{filename}' para o Cloudinary...")
            upload_result = cloudinary.uploader.upload(
                file,
                resource_type="raw", # Importante para arquivos que não são imagens
                public_id=f"pedidos/{filename}" # Salva numa pasta "pedidos"
            )
            pdf_url = upload_result['secure_url']
            print(f"Upload concluído. URL: {pdf_url}")

            # 2. Baixa o conteúdo do PDF em memória a partir da URL
            import requests
            print(f"Baixando conteúdo do PDF da URL...")
            response = requests.get(pdf_url)
            response.raise_for_status()  # Lança um erro se o download falhar
            pdf_bytes = response.content
            print("Download do conteúdo concluído.")

            # 3. Extrai os dados passando o conteúdo em memória (stream)
            print("Extraindo dados do PDF...")
            dados_extraidos = extrair_dados_do_pdf(
                nome_da_carga=nome_da_carga, 
                stream=pdf_bytes
            )

            if "erro" in dados_extraidos:
                erros.append(f"Arquivo '{filename}': {dados_extraidos['erro']}")
                continue

            print("Extração de dados concluída.")

            # 4. Adiciona a URL do PDF e salva no banco de dados PostgreSQL
            dados_extraidos['url_pdf'] = pdf_url
            salvar_no_banco_de_dados(dados_extraidos)
            sucessos += 1
            print(f"Dados do arquivo '{filename}' salvos no banco de dados.")

        except Exception as e:
            import traceback
            print(f"ERRO CRÍTICO no processamento do arquivo {filename}: {e}")
            traceback.print_exc()
            erros.append(f"Arquivo '{filename}': Falha inesperada no processamento.")

    if erros:
        return jsonify({"sucesso": False, "erro": f"{sucessos} arquivo(s) processado(s). ERROS: {'; '.join(erros)}"})

    return jsonify({"sucesso": True, "mensagem": f"Todos os {sucessos} arquivo(s) da carga '{nome_da_carga}' foram processados."})

@app.route('/api/cargas')
def api_cargas():
    conn = get_db_connection()
    cur = conn.cursor()
    # Busca todos os nomes de carga únicos na tabela de pedidos
    cur.execute("SELECT DISTINCT nome_da_carga FROM pedidos ORDER BY nome_da_carga;")
    # O resultado vem como uma lista de tuplas, ex: [('Carga1',), ('Carga2',)]
    # Precisamos extrair o primeiro item de cada tupla.
    cargas = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(cargas)

@app.route('/api/pedidos/<nome_da_carga>')
def api_pedidos_por_carga(nome_da_carga):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) # Para retornar resultados como dicionários
    cur.execute("SELECT * FROM pedidos WHERE nome_da_carga = %s;", (nome_da_carga,))
    pedidos = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(pedidos)

@app.route('/api/item/update', methods=['POST'])
def update_item_status():
    dados_recebidos = request.json
    if not os.path.exists(DB_FILE):
        return jsonify({"sucesso": False, "erro": "Banco de dados não encontrado"})
    
    with open(DB_FILE, 'r+', encoding='utf-8') as f:
        banco_de_dados = json.load(f)
        status_final = "Erro"
        
        for pedido in banco_de_dados:
            if pedido['numero_pedido'] == dados_recebidos['pedido_id']:
                for produto in pedido['produtos']:
                    # CORREÇÃO: Usando 'produto_nome' para a comparação
                    if produto['produto_nome'] == dados_recebidos['produto_nome']:
                        qtd_entregue_str = dados_recebidos['quantidade_entregue']
                        observacao_texto = dados_recebidos.get('observacao', '')
                        produto['quantidade_entregue'] = qtd_entregue_str
                        produto['observacao'] = observacao_texto
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
                todos_conferidos = all(p['status'] != 'Pendente' for p in pedido['produtos'])
                if todos_conferidos: pedido['status_conferencia'] = 'Finalizado'
                break
        f.seek(0)
        json.dump(banco_de_dados, f, indent=4, ensure_ascii=False)
        f.truncate()
    criar_backup()
    return jsonify({"sucesso": True, "status_final": status_final})

@app.route('/api/cortes')
def api_cortes():
    if not os.path.exists(DB_FILE): return jsonify({})
    with open(DB_FILE, 'r', encoding='utf-8') as f:
        try: pedidos = json.load(f)
        except json.JSONDecodeError: return jsonify({})
    cortes_agrupados = defaultdict(list)
    for pedido in pedidos:
        nome_carga = pedido.get('nome_da_carga', 'Sem Carga')
        for produto in pedido['produtos']:
            if produto['status'] in ['Corte Parcial', 'Corte Total']:
                if nome_carga not in cortes_agrupados: cortes_agrupados[nome_carga] = []
                # CORREÇÃO: Passando o objeto produto inteiro que agora contém 'produto_nome'
                item = {"numero_pedido": pedido['numero_pedido'], "nome_cliente": pedido['nome_cliente'], "vendedor": pedido['vendedor'], "observacao": produto.get('observacao', ''),
                        "produto": produto}
                cortes_agrupados[nome_carga].append(item)
    return jsonify(cortes_agrupados)

@app.route('/api/backups-listar')
def listar_backups():
    arquivos = sorted([f for f in os.listdir(BACKUP_FOLDER) if f.endswith('.json')], reverse=True)
    return jsonify(arquivos)

@app.route('/api/restaurar-backup/<nome_backup>', methods=['POST'])
def restaurar_backup(nome_backup):
    caminho_backup = os.path.join(BACKUP_FOLDER, nome_backup)
    if not os.path.exists(caminho_backup):
        return jsonify({"sucesso": False, "erro": "Backup não encontrado."}), 404
    try:
        shutil.copyfile(caminho_backup, DB_FILE)
        return jsonify({"sucesso": True, "mensagem": f"Backup '{nome_backup}' restaurado com sucesso."})
    except Exception as e:
        return jsonify({"sucesso": False, "erro": str(e)}), 500

@app.route('/api/resetar-dia', methods=['POST'])
def resetar_dia():
    try:
        criar_backup()
        if os.path.exists(DB_FILE): os.remove(DB_FILE)
        for filename in os.listdir(app.config['UPLOAD_FOLDER']):
            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return jsonify({"sucesso": True, "mensagem": "Sistema limpo para o próximo dia. Um backup final foi criado."})
    except Exception as e:
        return jsonify({"sucesso": False, "erro": str(e)}), 500

@app.route('/api/gerar-relatorio')
def gerar_relatorio():
    if not os.path.exists(DB_FILE): return "Banco de dados não encontrado.", 404
    with open(DB_FILE, 'r', encoding='utf-8') as f:
        try: pedidos = json.load(f)
        except json.JSONDecodeError: return "Banco de dados vazio ou corrompido.", 404
    dados_para_excel = []
    for pedido in pedidos:
        for produto in pedido['produtos']:
            if produto['status'] in ['Corte Parcial', 'Corte Total']:
                try:
                    valor_total = float(produto['valor_total_item'])
                    unidades_pacote = int(produto['unidades_pacote'])
                    match = re.match(r'(\d+)', produto['quantidade_pedida'])
                    pacotes_pedidos = int(match.group(1)) if match else 0
                    preco_por_pacote = valor_total / pacotes_pedidos if pacotes_pedidos > 0 else 0
                    preco_unidade = preco_por_pacote / unidades_pacote if unidades_pacote > 0 else 0
                    unidades_pedidas = pacotes_pedidos * unidades_pacote
                    unidades_entregues = int(produto.get('quantidade_entregue')) if produto.get('quantidade_entregue') is not None else 0
                    valor_corte = (unidades_pedidas - unidades_entregues) * preco_unidade
                    
                    dados_para_excel.append({
                        'Pedido': pedido['numero_pedido'],
                        'Cliente': pedido['nome_cliente'],
                        'Vendedor': pedido['vendedor'],
                        # CORREÇÃO: Usando 'produto_nome' para consistência
                        'Produto': produto.get('produto_nome', ''),
                        'Quantidade Pedida': produto['quantidade_pedida'],
                        'Quantidade Entregue': produto.get('quantidade_entregue', ''),
                        'Status': produto['status'],
                        'Observação': produto.get('observacao', ''),
                        'Valor Total Item': produto['valor_total_item'],
                        'Valor do Corte Estimado': round(valor_corte, 2)
                    })
                except (ValueError, TypeError, AttributeError) as e:
                    print(f"Erro ao calcular corte para o produto {produto.get('produto_nome', 'N/A')}: {e}")
                    continue
    if not dados_para_excel: return "Nenhum item com corte encontrado para gerar o relatório."
    df = pd.DataFrame(dados_para_excel)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Cortes')
    return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment;filename=cortes_relatorio.xlsx"})


# =================================================================
# NOVA ROTA PARA DOWNLOAD DO BACKUP
# =================================================================
@app.route('/backups/<nome_backup>')
def download_backup(nome_backup):
    caminho = os.path.join(BACKUP_FOLDER, nome_backup)
    if not os.path.exists(caminho):
        return "Arquivo não encontrado", 404
    return send_file(caminho, as_attachment=True)

# =================================================================
# RODA O APP
# =================================================================
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)