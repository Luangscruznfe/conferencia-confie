import fitz
import os

# ===== ATENÇÃO: COLOQUE O NOME DO SEU NOVO PDF AQUI =====
NOME_DO_ARQUIVO_PDF = "24.pdf"
# ==========================================================

def script_diagnostico_final(caminho_do_pdf):
    """
    Este script lê um PDF e imprime sua estrutura de texto linha por linha.
    """
    print(f"--- INICIANDO DIAGNÓSTICO PARA O ARQUIVO: {caminho_do_pdf} ---")
    
    if not os.path.exists(caminho_do_pdf):
        print(f"\nERRO: Arquivo '{caminho_do_pdf}' não encontrado nesta pasta.")
        print("Por favor, verifique se o nome do arquivo está correto e se ele está na mesma pasta que este script.")
        return

    try:
        documento = fitz.open(caminho_do_pdf)
        pagina = documento[0]
        blocos = pagina.get_text("blocks", sort=True)
        
        print("\n--- LINHAS RECONSTRUÍDAS PELO SCRIPT ---")
        
        for i, bloco in enumerate(blocos):
            texto_do_bloco = bloco[4].replace('\n', ' ').strip()
            print(f"[LINHA {i+1:02d}]: {texto_do_bloco}")
            
        print("------------------------------------------\n")
        print("Fim do diagnóstico.")
        print("Por favor, copie TODO o resultado acima (a partir de '--- LINHAS RECONSTRUÍDAS ---') e cole na nossa conversa.")

    except Exception as e:
        print(f"\nOcorreu um erro inesperado durante o diagnóstico: {e}")

# --- Execução do Script ---
if __name__ == "__main__":
    script_diagnostico_final(NOME_DO_ARQUIVO_PDF)