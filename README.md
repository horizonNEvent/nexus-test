# Automação SAAT Nexus ONS

Repositório de automação para autenticação (Keycloak SSO com suporte a MFA/2FA), consulta de apurações (AVD/AVC) e download automatizado de relatórios e anexos fiscais (XML/PDF) na plataforma **SAAT Nexus ONS**.

---

## 📖 Documentação Completa

Para a explicação técnica aprofundada da arquitetura de microsserviços do ONS, separação de dados vs. binários (`fileId` / S3), formato dos headers e endpoints, consulte:

👉 **[DOCUMENTACAO_FLUXO_NEXUS.md](DOCUMENTACAO_FLUXO_NEXUS.md)**

---

## 🚀 Início Rápido

### 1. Instalação das Dependências

```bash
pip install requests python-dotenv playwright beautifulsoup4
playwright install chromium
```

### 2. Configuração do `.env`

Preencha as credenciais da sua conta no arquivo `.env`:

```env
login=seu.usuario@empresa.com.br
senha=SuaSenhaForte
ONS_AMSE=4284
ONS_COMPETENCIA=2026-08
```

### 3. Execução

```bash
python main.py
```

- **Primeira execução:** O Chrome em background carrega a tela do ONS Keycloak e pedirá no terminal o código OTP de 6 dígitos do app autenticador.
- **Execuções seguintes:** O token é salvo em `nexus_token.json` e renovado automaticamente via `refresh_token`, sem solicitar o código novamente.

---

## 📁 Arquivos Gerados (`saida/`)

Ao final da execução, os arquivos são disponibilizados na pasta `saida/`:
- `NOTAFISCAL_4284_082026_D25.xml`: XML da Nota Fiscal baixado via URL assinada do S3.
- `relatorio_oficial_4284_2026-08.pdf`: Relatório Oficial emitido pelo ONS em PDF.
- `relatorio_oficial_4284_2026-08.xlsx`: Relatório Oficial emitido pelo ONS em Excel.
- `documentos_4284_2026-08.zip`: Pacote ZIP consolidado de exportação.
- `01_me_agents.json`, `02_avc_avd.json`, `04_payment_attachments.json`: Metadados retornados pela API.
