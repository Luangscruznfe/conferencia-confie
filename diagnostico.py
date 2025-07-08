import fitz
import os

# ===== ATENÇÃO: COLOQUE O NOME DO SEU NOVO PDF AQUI =====
NOME_DO_ARQUIVO_PDF = "15.pdf"
# ==========================================================

def script_diagnostico_final(caminho_do_pdf):
    """
    Lê o PDF e imprime os blocos de texto página por página,
    linha por linha, mostrando a posição e o conteúdo.
    """
    print(f"--- INICIANDO DIAGNÓSTICO PARA O ARQUIVO: {caminho_do_pdf} ---")
    
    if not os.path.exists(caminho_do_pdf):
        print(f"\n❌ ERRO: Arquivo '{caminho_do_pdf}' não encontrado.")
        print("Verifique se o nome do arquivo está correto e se está na mesma pasta que este script.")
        return

    try:
        documento = fitz.open(caminho_do_pdf)

        for num_pagina, pagina in enumerate(documento, start=1):
            print(f"\n===== PÁGINA {num_pagina} =====")
            blocos = pagina.get_text("blocks", sort=True)

            for i, bloco in enumerate(blocos):
                x0, y0, x1, y1, texto = bloco[:5]
                texto = texto.replace('\n', ' ').strip()
                if texto:
                    print(f"[{i+1:02d}] ({x0:.1f}, {y0:.1f}) → {texto}")
        
        print("\n=== FIM DO DIAGNÓSTICO ===")
        print("Copie TODO o resultado acima (a partir de '===== PÁGINA') e cole aqui na conversa para eu analisar.")

    except Exception as e:
        print(f"\n❌ ERRO: Ocorreu uma exceção durante a leitura do PDF:\n{e}")

# --- Execução do Script ---
if __name__ == "__main__":
    script_diagnostico_final(NOME_DO_ARQUIVO_PDF)
