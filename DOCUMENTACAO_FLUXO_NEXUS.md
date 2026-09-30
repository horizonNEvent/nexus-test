# Integração SAAT Nexus ONS — Documentação Completa do Fluxo

Este documento descreve detalhadamente a arquitetura, endpoints, fluxo de autenticação (MFA/2FA), identificação de escopo de agentes, consulta de apurações (AVD/AVC) e o mecanismo de download de arquivos binários por meio de URLs assinadas da plataforma **SAAT Nexus ONS**.

---

## 1. Visão Geral da Arquitetura

O sistema SAAT Nexus do Operador Nacional do Sistema Elétrico (ONS) utiliza uma arquitetura baseada em microsserviços distribuídos:

```mermaid
flowchart TD
    subgraph Client["Aplicação / Script Python (main.py)"]
        Playwright["Playwright (Login Web + MFA)"]
        HTTPClient["Requests HTTP Client"]
    end

    subgraph ONS_IdP["IdP ONS (Keycloak / SINtegre)"]
        SSO["sso.ons.org.br\n/auth/realms/ONS"]
        TokenEndpoint["/protocol/openid-connect/token"]
    end

    subgraph ONS_API["API de Negócio (Integra ONS)"]
        MeAgents["GET /apuracaotransmissao/v2/me/agents"]
        AvcAvd["GET /apuracaotransmissao/v2/avc-avd"]
        Attachments["GET /apuracaotransmissao/v2/payment-attachments\n(Retorna apenas relações e fileId)"]
        Reports["POST /apuracaotransmissao/v2/avc-avd/report\n(Relatórios PDF / XLSX)"]
    end

    subgraph ONS_Files["Serviço de Arquivos (ons-nexus-files)"]
        FilesApi["GET /files-api/files/download?ids={fileId}\n(nexus.ons.org.br/files-api)"]
    end

    subgraph AWS_S3["Armazenamento de Binários (Storage)"]
        S3["AWS S3 Bucket\n(ons-plataforma-unica-prod-files.s3.amazonaws.com)"]
    end

    Playwright -->|1. Autentica usuário/senha + OTP| SSO
    SSO -->|2. Emite tokens JWT| TokenEndpoint
    TokenEndpoint -->|Salva nexus_token.json| Client
    HTTPClient -->|3. Consulta agentes e AVDs| ONS_API
    HTTPClient -->|4. Consulta relações de anexos| Attachments
    Attachments -->|5. Retorna fileId| HTTPClient
    HTTPClient -->|6. Solicita URL assinada| FilesApi
    FilesApi -->|7. Retorna URL pré-assinada do S3| HTTPClient
    HTTPClient -->|8. Download direto do binário| S3
```

### URLs e Domínios Base

| Serviço                       | Domínio / URL Base                                          | Finalidade                                                                       |
| :----------------------------- | :----------------------------------------------------------- | :------------------------------------------------------------------------------- |
| **IdP / SSO**            | `https://sso.ons.org.br/auth/realms/ONS`                   | Autenticação centralizada SINtegre / Keycloak (OAuth2 / OIDC).                 |
| **Portal Web**           | `https://nexus.ons.org.br`                                 | Interface SPA do SAAT Nexus.                                                     |
| **API Principal**        | `https://integra.ons.org.br/api/apuracaotransmissao/v2`    | Consulta de agentes, AVDs, AVCs e relações de anexos.                          |
| **Serviço de Arquivos** | `https://nexus.ons.org.br/files-api`                       | Microsserviço`ons-nexus-files` responsável pela geração de URLs assinadas. |
| **Storage de Binários** | `https://ons-plataforma-unica-prod-files.s3.amazonaws.com` | Bucket AWS S3 onde os arquivos físicos (XML, PDF, ZIP) ficam armazenados.       |

---

## 2. Princípio de Separação de Dados e Arquivos

Conforme definido na especificação OAS3 da plataforma SAAT Nexus:

> *"O binário nunca passa por esta API: upload e download acontecem no serviço ons-nexus-files por URL assinada; aqui ficam só as relações (fileId)."*

Isso significa que:

1. **Nenhum arquivo binário** é retornado como payload direto dos endpoints de apuração ou pagamentos.
2. A API de negócio manipula exclusivamente **metadados e ponteiros** (`fileId`, `batch`, `validationStatus`, dados da NF-e).
3. O download e o upload físicos dependem de uma solicitação ao microsserviço `ons-nexus-files` para obter uma URL pré-assinada do S3 com validade temporária (tipicamente 300 segundos).

---

## 3. Métodos de Autenticação na API SAAT Nexus ONS

A arquitetura de segurança do ONS (baseada em Keycloak / OIDC e OAuth 2.0) e a especificação OpenAPI da plataforma SAAT Nexus contemplam **6 métodos de autenticação**.

### 3.1. Tabela Comparativa dos Métodos

| Método | Precisa de Navegador? | Exige 2FA / MFA? | Como é Obtido | Uso Recomendado |
| :--- | :---: | :---: | :--- | :--- |
| **1. Client Credentials (M2M)** | ❌ Não | ❌ Não | Chamado formal com a TI do ONS | Ideal para produção, rotinas 24/7 e ERPs |
| **2. Auth Code + PKCE** | Sim (apenas 1ª vez) | Sim (apenas 1ª vez) | Automatizado no [main.py](main.py) | Melhor opção imediata com credenciais de e-mail |
| **3. Refresh Token** | ❌ Não | ❌ Não | Automático via `nexus_token.json` | Mantém o script rodando sem pedir MFA |
| **4. Direct Grant (Password)** | ❌ Não | Depende | Bloqueado pelo ONS se houver 2FA ativo | Apenas se o ONS dispensar 2FA na conta |
| **5. Token Manual (.env)** | ❌ Não | ❌ Já autenticado | Copiado do DevTools (F12) | Testes rápidos de desenvolvimento |
| **6. Sessão de Suporte/Admin** | ❌ Não | ❌ Sessão interna | Rota `/v1/admin/session/login` | Exclusivo para operadores internos do ONS |

---

### 3.2. Método 1: Conta de Serviço / M2M (`grant_type=client_credentials`) — *Recomendado para Produção*

* **Como funciona:** Autenticação puramente entre servidores (Machine-to-Machine) via protocolo OAuth 2.0.
* **Quando usar:** Quando a empresa possui uma aplicação ou ERP (como SAP, Protheus, etc.) rodando em servidor ou rotina noturna que precisa consultar a API sem nenhuma intervenção humana.
* **Requisito:** Um `client_id` e um `client_secret` de aplicação homologados formalmente junto à TI/Suporte do ONS para o AMSE da sua empresa (não é o login de e-mail).
* **Endpoint:** `POST https://sso.ons.org.br/auth/realms/ONS/protocol/openid-connect/token`
* **Exemplo de Requisição HTTP:**
  ```http
  POST /auth/realms/ONS/protocol/openid-connect/token HTTP/1.1
  Host: sso.ons.org.br
  Content-Type: application/x-www-form-urlencoded

  grant_type=client_credentials&client_id=SEU_CLIENT_ID&client_secret=SEU_CLIENT_SECRET
  ```
* **Vantagens:**
  * ✅ **Não possui 2FA / MFA** (não solicita código OTP do celular).
  * ✅ 100% automatizável via simples requisição HTTP (`requests` ou `curl`).

---

### 3.3. Método 2: Navegador Automatizado com MFA (`authorization_code` com PKCE) — *Implementado no `main.py`*

* **Como funciona:** O script simula o fluxo oficial do navegador utilizado pelo portal web do Nexus.
* **Quando usar:** Quando a empresa possui apenas credenciais de operador do SINtegre (`login=...`, `senha=...`), com 2FA obrigatório via aplicativo autenticador móvel (TOTP de 6 dígitos).
* **Como o `main.py` executa:**
  1. O Playwright abre o Chrome em background na página do Keycloak (`sso.ons.org.br`).
  2. Preenche automaticamente usuário e senha do `.env`.
  3. Solicita uma única vez no terminal o código de 6 dígitos gerado pelo seu app autenticador.
  4. Intercepta a resposta da rota `/token` e extrai o `access_token` e o `refresh_token`.
* **Vantagens:**
  * ✅ Funciona imediatamente sem depender de abertura de chamados ou autorizações prévias do ONS.

---

### 3.4. Método 3: Renovação Silenciosa via Refresh Token (`grant_type=refresh_token`)

* **Como funciona:** Uma vez autenticado pelo Método 2, o Keycloak gera um `refresh_token` de longa duração que fica salvo no arquivo `nexus_token.json`.
* **Como é executado:**
  ```http
  POST /auth/realms/ONS/protocol/openid-connect/token HTTP/1.1
  Host: sso.ons.org.br
  Content-Type: application/x-www-form-urlencoded

  grant_type=refresh_token&client_id=SAAT&refresh_token=SEU_REFRESH_TOKEN_SALVO
  ```
* **Vantagens:**
  * ✅ **Não precisa de navegador.**
  * ✅ **Não pede senha e NÃO pede código 2FA/MFA.**
  * ✅ O script faz isso silenciosamente via HTTP em menos de 1 segundo.
* **Comportamento no projeto:** O `main.py` já executa esse método de forma automática: ele só pede o código MFA se o `refresh_token` tiver expirado após dias sem uso.

---

### 3.5. Método 4: Direct Grant via HTTP (`grant_type=password`)

* **Como funciona:** Envio direto de usuário e senha no corpo de uma requisição HTTP `POST` para o endpoint `/token` do Keycloak:
  ```http
  POST /auth/realms/ONS/protocol/openid-connect/token HTTP/1.1
  Host: sso.ons.org.br
  Content-Type: application/x-www-form-urlencoded

  grant_type=password&client_id=SAAT&username=ivan.fernandes@...&password=...
  ```
* **Situação no ONS:**
  * ⚠️ Para contas do SINtegre onde o 2FA/MFA foi habilitado, o Keycloak do ONS **bloqueia** esta rota com `invalid_grant: Invalid user credentials`, pois ela não suporta o desafio visual interativo do OTP.
  * ✅ Só funciona diretamente se a conta no ONS tiver o 2FA dispensado administrativamente.

---

### 3.6. Método 5: Injeção Manual de Token JWT da Sessão Ativa (`Bearer <token>`)

* **Como funciona:**
  1. O operador faz login normalmente pelo navegador no site `https://nexus.ons.org.br`.
  2. Abre as ferramentas de desenvolvedor do navegador (`F12`) ➔ aba **Application (Aplicativo)** ➔ **Local Storage** ➔ copia o valor da chave `token`.
  3. Define no arquivo `.env`:
     ```env
     token=eyJhbGciOiJSUzI1NiIs...
     ```
* **Vantagens:**
  * ✅ O script pula totalmente qualquer etapa de login e consome as APIs imediatamente.
* **Desvantagens:**
  * ⚠️ O token JWT expira em poucos minutos/horas e precisará ser copiado manualmente de tempos em tempos.

---

### 3.7. Método 6: Sessão Administrativa de Suporte (`/v1/admin/session/login`)

* **Como funciona:** Endpoint previsto no Swagger da API do Nexus:
  * `POST https://integra.ons.org.br/api/v1/admin/session/login`
* **Finalidade:** Utilizado exclusivamente por operadores internos de suporte e administração do ONS para rotas administrativas (`/v1/admin/*`). Não se aplica a agentes de mercado.

---

## 4. Identificação dos Agentes e Escopo (ABAC)

O backend do SAAT Nexus utiliza controle de acesso baseado em atributos (ABAC). Toda chamada às rotas de negócio deve conter dois headers:

```http
Authorization: Bearer <JWT_ACCESS_TOKEN>
X-Active-Agent: <CODIGO_NUMERICO_AMSE>
```

### 4.1. Consulta de Agentes Atribuídos ao Usuário

- **Método / URL:** `GET https://integra.ons.org.br/api/apuracaotransmissao/v2/me/agents`
- **JSON de Retorno Real:**

```json
{
  "items": [
    {
      "code": "4284",                                  // Código ONS do Agente
      "role": "payer",                                 // Papel (Pagador)
      "legalName": "ANEMUS WIND 1 PARTICIPACOES S.A.",    // Razão Social
      "cnpj": "29481536000158",                        // CNPJ da Empresa
      "shortName": "EOL ANEMUS WIND 1"                // Nome Fantasia / Usina
    },
    {
      "code": "4292",
      "role": "payer",
      "legalName": "ANEMUS WIND 2 PARTICIPAÇÕES S.A",
      "cnpj": "29492546000199",
      "shortName": "EOL ANEMUS WIND 2"
    },
    {
      "code": "4319",
      "role": "payer",
      "legalName": "ANEMUS WIND 3 PARTICIPACOES S.A",
      "cnpj": "38350307000195",
      "shortName": "EOL ANEMUS WIND 3"
    }
  ],
  "total": 3
}
```

### 4.2. Gotcha Importante: Código Numérico vs. Sigla

- Passar a sigla do agente (ex: `"AW1"`) resulta em **HTTP 403 Forbidden**:
  ```json
  {"detail": "AMSE ativo fora do escopo do usuário"}
  ```
- O header `X-Active-Agent` **deve ser o código numérico** (ex: `"4284"`).

---

## 5. Consulta de Cobranças AVD / AVC (Valores, Vencimento e Partes)

Para listar os documentos de apuração da competência:

- **Método / URL:** `GET https://integra.ons.org.br/api/apuracaotransmissao/v2/avc-avd`
- **Query Parameters:**
  - `referenceMonth`: Mês de competência no formato `YYYY-MM` (ex: `2026-08`).
  - `pageSize`: Quantidade de itens por página (ex: `200`).

### 5.1. JSON de Retorno Real da Apuração (Exemplo da Transmissora Pantanal)

```json
{
  "id": "742e4535-a29a-4c25-9082-9f8018045c0f:4c306ba7-1335-4aba-a374-98ab3375d158:25",
  "docId": "742e4535-a29a-4c25-9082-9f8018045c0f",
  "type": "AVD",
  "number": 42325,
  "referenceMonth": "2026-08",
  "assessmentType": "ordinary",
  "publishedAt": "2026-09-01T10:51:37.106636+00:00", // Data de publicação/emissão
  "status": "document_available",

  // --- DADOS DA TRANSMISSORA CREDORA ---
  "creditorLegalName": "PANTANAL",                     // Nome Fantasia da Transmissora
  "counterpartyLegalName": "PANTANAL",
  "creditorCode": "1175",                              // Código ONS da Transmissora
  "counterpartyCode": "1175",
  "counterpartyCnpj": "18726961000143",                // CNPJ da Transmissora
  "counterpartyAmseId": "4c306ba7-1335-4aba-a374-98ab3375d158", // ID interno ONS

  // --- DADOS DA SUA EMPRESA (PAGADORA) ---
  "payerLegalName": "EOL ANEMUS WIND 1",               // Nome da Usina Pagadora
  "payerCode": "4284",                                 // Código ONS da sua Empresa
  "ownerAmseId": "220d0e18-0da3-4cb5-a72b-507e675a47a9", // ID interno ONS

  // --- DADOS FINANCEIROS E PRAZOS ---
  "amount": "12.05",                                   // Valor total apurado pelo ONS
  "dueDate": "2026-09-25",                             // Data de Vencimento
  "installment": 25                                    // Dia do vencimento
}
```

### 5.2. Outro Exemplo Real (AXIA NORTE / ETE)

```json
{
  "id": "742e4535-a29a-4c25-9082-9f8018045c0f:00c08e89-25b5-4ca5-b7c1-987a462388f2:25",
  "docId": "742e4535-a29a-4c25-9082-9f8018045c0f",
  "type": "AVD",
  "number": 42325,
  "referenceMonth": "2026-08",
  "creditorLegalName": "AXIA NORTE (ETE)",
  "creditorCode": "1111",                              // Código ONS: 1111
  "counterpartyCnpj": "00357038000116",                // CNPJ AXIA NORTE
  "payerLegalName": "EOL ANEMUS WIND 1",
  "payerCode": "4284",
  "amount": "1898.82",                                 // Valor: R$ 1.898,82
  "dueDate": "2026-09-25",
  "installment": 25,
  "status": "document_available"
}
```

---

## 6. Consulta de Relações de Anexos (`payment-attachments`)

Com as contrapartes identificadas, consultam-se os anexos associados:

- **Método / URL:** `GET https://integra.ons.org.br/api/apuracaotransmissao/v2/payment-attachments`
- **Query Parameters:**
  - `creditorAmseId`: UUID da transmissora credora (`counterpartyAmseId`).
  - `payerAmseId`: UUID do agente pagador (`ownerAmseId`).
  - `referenceMonth`: Mês de competência (`YYYY-MM`).

> [!WARNING]
> **Atenção ao parâmetro `payDay`:**
> Ao consultar a API passando `payDay=25`, a resposta retornava uma lista vazia `[]`.
> Investigando o registro real, verificou-se que o campo `payDay` no banco de dados do ONS é gravado como `null`.
> **Regra:** Não envie o parâmetro `payDay` na requisição para garantir a captura de todos os anexos cadastrados.

### 6.1. JSON de Retorno Real do Anexo (Pantanal)

```json
[
  {
    "id": "3a89daf3-8c15-4fc2-8b18-adb20d10c4a7",
    "fileId": "01a0e947-76b8-7563-8d38-1843f8679342", // ID do binário para download
    "fileName": "NOTAFISCAL_4284_082026_D25.xml",     // Nome do arquivo
    "batch": 67,
    "creditorAmseId": "4c306ba7-1335-4aba-a374-98ab3375d158",
    "payerAmseId": "220d0e18-0da3-4cb5-a72b-507e675a47a9",
    "referenceMonth": "2026-08",
    "payDay": null,
    "type": "nota_fiscal",                            // Tipo de anexo
    "createdAt": "2026-09-28T18:29:30.409058+00:00",  // Data/hora do upload
    "validationStatus": "ok",                         // Status de validação ONS
    "validationIssues": [],                           // Pendências se houver
    "source": "upload",

    // --- DADOS EXTRAÍDOS DA NOTA FISCAL ---
    "invoice": {
      "accessKey": "52260918726961000143550020001193151000000802", // Chave de 44 dígitos da NF-e
      "grossAmount": "12.05"                                       // Valor bruto da NF para conciliação
    }
  }
]
```

---

## 7. Download de Binários pelo Serviço `ons-nexus-files`

### 7.1. Obtenção da URL Pré-Assinada

Com o `fileId` obtido no passo anterior, fazemos uma requisição para o endpoint de download:

- **Método / URL:** `GET https://nexus.ons.org.br/files-api/files/download`
- **Headers:**
  - `Authorization: Bearer <JWT_ACCESS_TOKEN>`
  - `X-Active-Agent: <CODIGO_NUMERICO_AMSE>`
- **Query Parameters:**
  - `ids`: O UUID do `fileId` (ex: `01a0e947-76b8-7563-8d38-1843f8679342`).

### 7.2. JSON de Retorno Real do Serviço de Arquivos:

```json
{
  "results": [
    {
      "id": "01a0e947-76b8-7563-8d38-1843f8679342",
      "ok": true,
      "url": "https://ons-plataforma-unica-prod-files.s3.amazonaws.com/ons-files/01a0e947-76b8-7563-8d38-1843f8679342?response-content-disposition=attachment%3B%20filename%3D%22NOTAFISCAL_4284_082026_D25.xml%22&X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=ASIAVZ2CBFN5V5UWH6ZC%2F20260929%2Fsa-east-1%2Fs3%2Faws4_request&X-Amz-Date=20260929T145628Z&X-Amz-Expires=300&X-Amz-SignedHeaders=host&X-Amz-Signature=b9961fe3999c7d610410d9861cd4b425a9cec914343ff0973f6f7f3773584b55"
    }
  ]
}
```

### 7.3. Download Físico do Arquivo

O script realiza um `GET` HTTP na URL `results[0].url` e grava os bytes diretamente em disco no arquivo `saida/NOTAFISCAL_4284_082026_D25.xml`.

---

## 8. Guia de Validação e Conciliação Automatizada para os Próximos Ciclos

Com essas estruturas de dados retornadas pelo SAAT Nexus, qualquer sistema de backoffice ou ERP pode realizar a automação completa do ciclo de apuração:

| Etapa                                        | Campo no JSON                                         | Validação / Ação Recomendada                                                             |
| :------------------------------------------- | :---------------------------------------------------- | :------------------------------------------------------------------------------------------- |
| **1. Identificação do Pagador**      | `payerCode` (`4284`)                              | Mapear para a empresa / filial correta no plano de contas do ERP.                            |
| **2. Identificação da Transmissora** | `creditorCode` (`1175`) e `counterpartyCnpj`    | Identificar o fornecedor cadastrado no ERP pelo CNPJ ou código ONS.                         |
| **3. Conciliação de Valores**        | `avd.amount` vs. `attachment.invoice.grossAmount` | Validar se o valor apurado pelo ONS (`12.05`) é idêntico ao emitido na NF-e (`12.05`). |
| **4. Agendamento Financeiro**          | `dueDate` (`2026-09-25`)                          | Inserir a data de vencimento exata no Contas a Pagar.                                        |
| **5. Validação Fiscal SEFAZ**        | `invoice.accessKey`                                 | Consultar a chave de 44 dígitos na SEFAZ para confirmar autorização e protocolo.          |

---

## 9. Relatórios Oficiais e Exportação Consolidada

Além dos anexos individuais, o script baixa relatórios consolidados emitidos pelo ONS:

1. **Relatório Oficial em PDF e XLSX:**

   - **Endpoint:** `POST https://integra.ons.org.br/api/apuracaotransmissao/v2/avc-avd/report`
   - **Payload:**
     ```json
     {
       "format": "pdf", // ou "xlsx"
       "referenceMonth": "2026-08",
       "type": "AVD",
       "agents": ["4284"]
     }
     ```
   - **Destino:** `saida/relatorio_oficial_4284_2026-08.pdf` e `saida/relatorio_oficial_4284_2026-08.xlsx`.
2. **Pacote ZIP de Exportação de Documentos:**

   - **Endpoint:** `POST https://integra.ons.org.br/api/apuracaotransmissao/v2/avc-avd/documents/export`
   - **Payload:** `{"ids": ["<id_avd_1>", "<id_avd_2>", ...]}`
   - **Destino:** `saida/documentos_4284_2026-08.zip`.

---

## 10. Estrutura de Arquivos Gerados

Após a execução completa do [main.py](main.py), a pasta `saida/` contém:

```
saida/
├── 01_me_agents.json                 # Lista de AMSEs atribuídos ao usuário autenticado
├── 02_avc_avd.json                   # Todas as apurações AVD/AVC da competência
├── 04_payment_attachments.json       # Metadados e relações dos anexos encontrados
├── 05_export_preview.json            # Preview do lote de exportação
├── 06_export.json                    # Status do lote exportado
├── NOTAFISCAL_4284_082026_D25.xml   # Arquivo XML original da Nota Fiscal (Pantanal)
├── documentos_4284_2026-08.zip      # ZIP com planilha consolidada e anexos
├── relatorio_oficial_4284_2026-08.pdf # Relatório Oficial emitido pelo ONS (PDF)
└── relatorio_oficial_4284_2026-08.xlsx# Relatório Oficial emitido pelo ONS (Excel)
```

---

## 11. Como Executar

### 11.1. Pré-requisitos

Instalar as dependências Python:

```bash
pip install requests python-dotenv playwright beautifulsoup4
playwright install chromium
```

### 10.2. Configuração do `.env`

Crie ou ajuste o arquivo `.env`:

```env
login=seu.usuario@empresa.com.br
senha=SuaSenhaForte
ONS_AMSE=4284
ONS_COMPETENCIA=2026-08
```

### 10.3. Execução

```bash
python main.py
```

- Na **primeira execução**, digite o código de 6 dígitos do app autenticador quando solicitado.
- Nas **execuções seguintes**, o script reutilizará o token salvo em cache (`nexus_token.json`) sem pedir MFA.
