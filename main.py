"""
Teste da API SAAT Nexus ONS: lista AVDs/anexos e tenta baixar os documentos
de uma transmissora credora numa competência.

Uso (credenciais SEMPRE por variável de ambiente, nunca no código):
    export ONS_USER="seu.login@empresa.com.br"
    export ONS_PASS="..."
    export ONS_AMSE="4284"          # AMSE ativo (quem paga: AETE)
    export ONS_CREDOR="1175"        # transmissora credora (PANTANAL)
    export ONS_COMPETENCIA="2026-08"
    python testar_nexus.py

Cada resposta é salva em ./saida/*.json para você conferir os campos reais
(os nomes de campo usados aqui são suposições até validar os schemas).
"""
import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import bs4
from dotenv import load_dotenv
import requests

# Carrega variáveis do arquivo .env
load_dotenv(Path(__file__).parent / ".env")

BASE = "https://integra.ons.org.br/api"
SSO_BASE = "https://sso.ons.org.br/auth/realms/ONS"
API = f"{BASE}/apuracaotransmissao/v2"
FILES_API = "https://nexus.ons.org.br/files-api"
SAIDA = Path("saida")
SAIDA.mkdir(exist_ok=True)

USER = os.getenv("ONS_USER") or os.getenv("login") or os.getenv("LOGIN")
PASS = os.getenv("ONS_PASS") or os.getenv("senha") or os.getenv("SENHA")
CLIENT_ID = os.getenv("ONS_CLIENT_ID") or os.getenv("client_id") or os.getenv("CLIENT_ID") or "SAAT"
AMSE = os.getenv("ONS_AMSE") or os.getenv("amse") or "AW1"
CREDOR = os.getenv("ONS_CREDOR") or os.getenv("credor") or "1175"
COMPETENCIA = os.getenv("ONS_COMPETENCIA") or os.getenv("competencia") or "2026-08"

TOKEN_ENV = os.getenv("ONS_TOKEN") or os.getenv("token")

if not TOKEN_ENV and (not USER or not PASS):
    print("Erro: Credenciais não encontradas. Defina ONS_USER/ONS_PASS (ou login/senha) ou ONS_TOKEN no arquivo .env.")
    sys.exit(1)


def salvar(nome, dados):
    (SAIDA / f"{nome}.json").write_text(
        json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  -> saida/{nome}.json")


def chamar(metodo, url, headers, **kw):
    r = requests.request(metodo, url, headers=headers, timeout=60, **kw)
    print(f"{metodo} {url} -> {r.status_code}")
    try:
        corpo = r.json()
    except ValueError:
        corpo = {"raw": r.text[:2000]}
    if not r.ok:
        print("  erro:", json.dumps(corpo, ensure_ascii=False)[:500])
    return r.status_code, corpo


def obter_token():
    # 0) Token fixo direto de variável de ambiente (se fornecido)
    token_env = os.getenv("ONS_TOKEN") or os.getenv("token")
    if token_env:
        return token_env

    # 1) Cache local com refresh_token
    token_file = Path("nexus_token.json")
    if token_file.exists():
        try:
            cached = json.loads(token_file.read_text(encoding="utf-8"))
            refresh_token = cached.get("refresh_token")
            if refresh_token:
                r_ref = requests.post(
                    f"{SSO_BASE}/protocol/openid-connect/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "refresh_token": refresh_token,
                    },
                    timeout=30,
                )
                if r_ref.ok:
                    new_tokens = r_ref.json()
                    token_file.write_text(json.dumps(new_tokens, indent=2), encoding="utf-8")
                    print("  [Auth] Token renovado com sucesso a partir do refresh_token.")
                    return new_tokens["access_token"]
        except Exception:
            pass

    # 2) Fluxo com Playwright (Chrome automatizado para bypass de proteções e captura de 2FA)
    print("Iniciando navegador Chrome para login no SAAT Nexus ONS...")
    from playwright.sync_api import sync_playwright

    tokens_captured = {}
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)

        context = browser.new_context()
        page = context.new_page()

        def handle_response(response):
            if "/protocol/openid-connect/token" in response.url and response.status == 200:
                try:
                    data = response.json()
                    if "access_token" in data:
                        tokens_captured.update(data)
                except Exception:
                    pass

        page.on("response", handle_response)

        print("  -> Carregando https://nexus.ons.org.br/ ...")
        page.goto("https://nexus.ons.org.br/", wait_until="networkidle", timeout=60000)

        # Preenche usuário e senha
        page.wait_for_selector("input#username", timeout=20000)
        page.fill("input#username", USER)
        page.fill("input#password", PASS)

        print("  -> Submetendo credenciais...")
        page.click("input#kc-login")

        # Aguarda a tela de OTP
        try:
            page.wait_for_selector("input[name=otp]", timeout=15000)
        except Exception:
            err = page.locator(".kc-feedback-text, .alert-error, .pf-m-error")
            if err.count() > 0:
                msg = err.first.inner_text()
                browser.close()
                raise RuntimeError(f"Erro no login do ONS: {msg}")
            browser.close()
            raise RuntimeError("Não foi possível alcançar a tela do código de autenticação (OTP).")

        print("\n" + "=" * 55)
        print("  [MFA / 2FA] ONS solicitou o código de verificação!")
        print("=" * 55)
        otp_code = input("Digite o código de 6 dígitos do aplicativo autenticador: ").strip()

        print(f"  -> Enviando código {otp_code}...")
        page.fill("input[name=otp]", otp_code)
        page.click("input[name=login], input[type=submit]")

        # Aguarda a troca do token
        for _ in range(30):
            if "access_token" in tokens_captured:
                break
            page.wait_for_timeout(500)

        if "access_token" not in tokens_captured:
            page.wait_for_timeout(3000)
            token_val = page.evaluate("() => localStorage.getItem('token') || sessionStorage.getItem('token')")
            if token_val:
                tokens_captured["access_token"] = token_val

        browser.close()

    if "access_token" not in tokens_captured:
        raise RuntimeError("Não foi possível capturar o access_token após o 2FA. Verifique se o código estava correto.")

    token_file.write_text(json.dumps(tokens_captured, indent=2), encoding="utf-8")
    print("  [Auth] Autenticado com sucesso! Token salvo em nexus_token.json.\n")
    return tokens_captured["access_token"]


token = obter_token()

AMSE_MAP = {
    "AW1": "4284",
    "AW2": "4292",
    "AW3": "4319",
}
AMSE_RAW = os.getenv("ONS_AMSE") or os.getenv("amse") or "4284"
AMSE = AMSE_MAP.get(AMSE_RAW.upper(), AMSE_RAW)

H = {"Authorization": f"Bearer {token}", "X-Active-Agent": AMSE}

# 2) Quem sou eu / quais AMSEs posso assumir
print("\n--- Consultando Agentes Permitidos ---")
_, me = chamar("GET", f"{API}/me/agents", H)
salvar("01_me_agents", me)

# 3) AVC/AVD da competência
print(f"\n--- Consultando Cobranças AVD (Competência: {COMPETENCIA}, Agente: {AMSE}) ---")
_, avds = chamar(
    "GET",
    f"{API}/avc-avd",
    H,
    params={"referenceMonth": COMPETENCIA, "pageSize": 200},
)
salvar("02_avc_avd", avds)

itens = avds.get("items", avds if isinstance(avds, list) else [])
print(f"  Total de AVDs encontrados para {COMPETENCIA}: {len(itens)}")

# 4) Busca por anexos em qualquer transmissora
print("\n--- Verificando Anexos Disponíveis nas Transmissoras ---")
anexos_encontrados = []
transmissoras_com_anexo = []

# Agrupa contrapartes únicas para consultar (omite payDay pois no ONS costuma ser null)
pares_consultados = set()
for item in itens:
    credor_id = item.get("counterpartyAmseId")
    pagador_id = item.get("ownerAmseId")
    ref_mes = item.get("referenceMonth", COMPETENCIA)
    chave = (credor_id, pagador_id, ref_mes)
    if chave in pares_consultados or not credor_id or not pagador_id:
        continue
    pares_consultados.add(chave)

    status_code, resp = chamar(
        "GET",
        f"{API}/payment-attachments",
        H,
        params={
            "creditorAmseId": credor_id,
            "payerAmseId": pagador_id,
            "referenceMonth": ref_mes,
        },
    )
    if status_code == 200:
        lista_anexos = resp.get("items", [])
        if lista_anexos:
            transmissora_nome = item.get("creditorLegalName", credor_id)
            print(f"\n  [!] Encontrado {len(lista_anexos)} anexo(s) na transmissora: {transmissora_nome}")
            anexos_encontrados.extend(lista_anexos)
            transmissoras_com_anexo.append({"transmissora": transmissora_nome, "anexos": lista_anexos})

            # Download de cada arquivo via URL assinada do serviço ons-nexus-files
            for anexo in lista_anexos:
                file_id = anexo.get("fileId")
                file_name = anexo.get("fileName") or f"{file_id}.bin"
                tipo = anexo.get("type", "anexo")
                chave_nfe = (anexo.get("invoice") or {}).get("accessKey")
                print(f"      -> Baixando arquivo: {file_name} ({tipo}) [fileId: {file_id}]")
                if chave_nfe:
                    print(f"         Chave NFe: {chave_nfe}")

                try:
                    r_dl = requests.get(
                        f"{FILES_API}/files/download",
                        headers=H,
                        params={"ids": file_id},
                        timeout=30,
                    )
                    if r_dl.ok:
                        dl_data = r_dl.json()
                        results = dl_data.get("results", [])
                        if results and results[0].get("ok") and "url" in results[0]:
                            s3_url = results[0]["url"]
                            r_bin = requests.get(s3_url, timeout=60)
                            if r_bin.ok:
                                caminho_arquivo = SAIDA / file_name
                                caminho_arquivo.write_bytes(r_bin.content)
                                print(f"         [OK] Salvo em: {caminho_arquivo} ({len(r_bin.content)} bytes)")
                            else:
                                print(f"         [Erro] Falha no download do S3: {r_bin.status_code}")
                        else:
                            print(f"         [Erro] Resposta sem URL válida: {dl_data}")
                    else:
                        print(f"         [Erro] Falha ao obter URL assinada ({r_dl.status_code}): {r_dl.text}")
                except Exception as e:
                    print(f"         [Erro] Falha na requisição do arquivo: {e}")

salvar("04_payment_attachments", anexos_encontrados)
print(f"\n  Total de anexos individuais encontrados nas transmissoras: {len(anexos_encontrados)}")

# 5) Preview da exportação de documentos
ids_para_exportar = [a.get("id") for a in itens if a.get("id")][:50]
if ids_para_exportar:
    print(f"\n--- Gerando Exportação de Documentos (Lote de {len(ids_para_exportar)} linhas) ---")
    _, preview = chamar("POST", f"{API}/avc-avd/documents/export/preview", H,
                        json={"ids": ids_para_exportar})
    salvar("05_export_preview", preview)

    # 6) Exportação do ZIP consolidado com planilha e anexos
    r_exp = requests.post(
        f"{API}/avc-avd/documents/export",
        headers=H,
        json={"ids": ids_para_exportar},
        timeout=120,
    )
    print(f"POST {API}/avc-avd/documents/export -> {r_exp.status_code}")
    if r_exp.ok:
        destino_zip = SAIDA / f"documentos_{AMSE}_{COMPETENCIA}.zip"
        destino_zip.write_bytes(r_exp.content)
        print(f"  -> Arquivo ZIP salvo com sucesso: {destino_zip}")
        salvar("06_export", {"status": "ok", "tamanho_bytes": len(r_exp.content), "arquivo": str(destino_zip)})
    else:
        print("  Erro na exportação do ZIP:", r_exp.text[:500])

# 7) Download dos Relatórios Oficiais (PDF e XLSX)
print(f"\n--- Baixando Relatórios Oficiais da Competência {COMPETENCIA} ---")
for formato in ["pdf", "xlsx"]:
    r_rep = requests.post(
        f"{API}/avc-avd/report",
        headers=H,
        json={
            "format": formato,
            "referenceMonth": COMPETENCIA,
            "type": "AVD",
            "agents": [AMSE],
        },
        timeout=120,
    )
    print(f"POST {API}/avc-avd/report ({formato}) -> {r_rep.status_code}")
    if r_rep.ok:
        arquivo_relatorio = SAIDA / f"relatorio_oficial_{AMSE}_{COMPETENCIA}.{formato}"
        arquivo_relatorio.write_bytes(r_rep.content)
        print(f"  -> Relatório {formato.upper()} salvo: {arquivo_relatorio}")
