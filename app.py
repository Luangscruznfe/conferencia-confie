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
    """Garante que a tabela 'pedidos' exista no banco de dados com TODAS as colunas."""
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
            produtos JSONB,
            url_pdf TEXT
        );
    ''')
    conn.commit()
    cur.close()
    conn.close()

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

# Substitua sua função inteira por esta
# Dentro da sua função extrair_dados_do_pdf
def extrair_dados_do_pdf(nome_da_carga, nome_arquivo, stream=None, caminho_do_pdf=None):
    try:
        # ... (código para abrir o documento) ...

        for i, pagina in enumerate(documento):
            
            # ADICIONE ESTES PRINTS PARA VER O TEXTO EXTRAÍDO
            texto_completo_pagina = pagina.get_text("text")
            print("---- DEBUG: INÍCIO DO TEXTO DA PÁGINA ----")
            print(texto_completo_pagina)
            print("---- DEBUG: FIM DO TEXTO DA PÁGINA ----")

            # ... (sua lógica para extrair cabeçalho) ...
            
            y_inicio, y_fim = 0, pagina.rect.height
            y_inicio_list = pagina.search_for("ITEM CÓD. BARRAS")
            y_fim_list = pagina.search_for("TOTAL GERAL")

            if y_inicio_list: y_inicio = y_inicio_list[0].y1
            else: y_inicio = 50
            
            if y_fim_list: y_fim = y_fim_list[0].y0
            else:
                footer_list = pagina.search_for("POR GENTILEZA CONFERIR")
                if footer_list: y_fim = footer_list[0].y0 - 5
            
            # ADICIONE ESTE PRINT PARA VER AS COORDENADAS
            print(f"---- DEBUG: Coordenadas calculadas -> Y_INICIO={y_inicio}, Y_FIM={y_fim}")
            
            palavras_na_tabela = [p for p in pagina.get_text("words") if p[1] > y_inicio and p[3] < y_fim]
            
            # ADICIONE ESTE PRINT PARA VER QUANTAS PALAVRAS FORAM ENCONTRADAS
            print(f"---- DEBUG: Palavras encontradas na área da tabela: {len(palavras_na_tabela)}")
            
            # ... (resto da sua função) ...

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

# Substitua sua rota de upload inteira por esta
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

            dados_extraidos = extrair_dados_do_pdf(
                nome_da_carga=nome_da_carga, 
                nome_arquivo=filename, # Passa o nome do arquivo
                stream=pdf_bytes
            )
            
            if "erro" in dados_extraidos:
                erros.append(f"Arquivo '{filename}': {dados_extraidos['erro']}")
                continue

            upload_result = cloudinary.uploader.upload(
                pdf_bytes,
                resource_type="raw",
                public_id=f"pedidos/{filename}"
            )
            
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