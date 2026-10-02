"""Camada web interativa para consulta do SAAT Nexus ONS."""
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

load_dotenv(Path(__file__).parent / ".env")

BASE = "https://integra.ons.org.br/api"
API = f"{BASE}/apuracaotransmissao/v2"
FILES_API = "https://nexus.ons.org.br/files-api"
TOKEN_FILE = Path(__file__).parent / "nexus_token.json"
DATABASE_FILE = Path(__file__).parent / "nexus_credentials.db"
KEY_FILE = Path(__file__).parent / "nexus_credentials.key"
SSO_TOKEN_URL = "https://sso.ons.org.br/auth/realms/ONS/protocol/openid-connect/token"

app = Flask(__name__)


@app.errorhandler(Exception)
def api_error(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": str(error) or "Erro interno no servidor."}), 500
    raise error


def env_value(*names, default=None):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def credential_cipher():
    key = os.getenv("NEXUS_CREDENTIAL_KEY")
    if key:
        return Fernet(key.encode())
    if not KEY_FILE.exists():
        KEY_FILE.write_bytes(Fernet.generate_key())
    return Fernet(KEY_FILE.read_bytes())


def init_credentials_database():
    with sqlite3.connect(DATABASE_FILE) as database:
        database.execute("CREATE TABLE IF NOT EXISTS credentials (id INTEGER PRIMARY KEY CHECK (id = 1), username BLOB NOT NULL, password BLOB NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")


def saved_credentials():
    init_credentials_database()
    with sqlite3.connect(DATABASE_FILE) as database:
        row = database.execute("SELECT username, password FROM credentials WHERE id = 1").fetchone()
    if not row:
        return None
    try:
        cipher = credential_cipher()
        return cipher.decrypt(row[0]).decode(), cipher.decrypt(row[1]).decode()
    except (InvalidToken, UnicodeDecodeError):
        raise RuntimeError("Nao foi possivel descriptografar as credenciais salvas.")


def save_credentials(username, password):
    if not username or not password:
        raise ValueError("Usuario e senha sao obrigatorios.")
    init_credentials_database()
    cipher = credential_cipher()
    with sqlite3.connect(DATABASE_FILE) as database:
        database.execute("INSERT INTO credentials (id, username, password) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET username=excluded.username, password=excluded.password, updated_at=CURRENT_TIMESTAMP", (cipher.encrypt(username.encode()), cipher.encrypt(password.encode())))


def login_with_mfa(otp):
    credentials = saved_credentials()
    if not credentials:
        raise RuntimeError("Salve usuario e senha antes de entrar.")
    if not otp or len(otp) != 6 or not otp.isdigit():
        raise ValueError("Informe o codigo OTP de 6 digitos.")
    username, password = credentials
    from playwright.sync_api import sync_playwright

    captured = {}
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()

            def handle_response(response):
                if "/protocol/openid-connect/token" in response.url and response.status == 200:
                    try:
                        payload = response.json()
                        if payload.get("access_token"):
                            captured.update(payload)
                    except (ValueError, TypeError):
                        pass

            page.on("response", handle_response)
            page.goto("https://nexus.ons.org.br/", wait_until="networkidle", timeout=60000)
            page.wait_for_selector("input#username", timeout=20000)
            page.fill("input#username", username)
            page.fill("input#password", password)
            page.click("input#kc-login")
            page.wait_for_selector("input[name=otp]", timeout=20000)
            page.fill("input[name=otp]", otp)
            page.click("input[name=login], input[type=submit]")
            for _ in range(30):
                if captured.get("access_token"):
                    break
                page.wait_for_timeout(500)
        finally:
            browser.close()
    if not captured.get("access_token"):
        raise RuntimeError("O ONS nao retornou um token. Confira o OTP informado.")
    TOKEN_FILE.write_text(json.dumps(captured, indent=2), encoding="utf-8")


@app.get("/api/credentials")
def credentials_status():
    try:
        credentials = saved_credentials()
        return jsonify({"saved": bool(credentials), "username": credentials[0] if credentials else ""})
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 500


@app.post("/api/credentials")
def credentials_save():
    data = request.get_json(silent=True) or {}
    try:
        save_credentials(data.get("username", "").strip(), data.get("password", ""))
        return jsonify({"saved": True, "username": data["username"].strip()})
    except (ValueError, sqlite3.Error) as error:
        return jsonify({"error": str(error)}), 400


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    try:
        if data.get("username") and data.get("password"):
            save_credentials(data["username"].strip(), data["password"])
        authenticate(data.get("otp", "").strip())
        return jsonify({"authenticated": True})
    except (RuntimeError, ValueError, sqlite3.Error) as error:
        return jsonify({"error": str(error)}), 400


def get_token():
    token = env_value("ONS_TOKEN", "token")
    if token:
        return token
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("access_token")
        except (OSError, json.JSONDecodeError):
            return None
    return None


def token_is_valid():
    token = get_token()
    if not token:
        return False
    agent = env_value("ONS_AMSE", "amse", default="4284")
    try:
        response = requests.get(f"{API}/me/agents", headers={"Authorization": f"Bearer {token}", "X-Active-Agent": agent}, timeout=20)
        return response.ok
    except requests.RequestException:
        return False


def refresh_saved_token():
    if not TOKEN_FILE.exists():
        return False
    try:
        saved = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        refresh_token = saved.get("refresh_token")
        if not refresh_token:
            return False
        response = requests.post(SSO_TOKEN_URL, data={"grant_type": "refresh_token", "client_id": env_value("ONS_CLIENT_ID", "client_id", default="SAAT"), "refresh_token": refresh_token}, timeout=30)
        if not response.ok:
            return False
        TOKEN_FILE.write_text(json.dumps(response.json(), indent=2), encoding="utf-8")
        return True
    except (OSError, ValueError, requests.RequestException):
        return False


def authenticate(otp):
    if token_is_valid() or refresh_saved_token():
        return
    login_with_mfa(otp)


@app.get("/api/auth-status")
def auth_status():
    has_session = bool(get_token())
    return jsonify({"authenticated": has_session, "otpRequired": not has_session})


def client_headers(agent):
    token = get_token()
    if not token:
        raise RuntimeError("Token nao configurado. Defina ONS_TOKEN ou execute o login pelo main.py.")
    return {"Authorization": f"Bearer {token}", "X-Active-Agent": agent}


def nexus_request(method, path, agent, **kwargs):
    response = requests.request(method, f"{API}{path}", headers=client_headers(agent), timeout=60, **kwargs)
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:1000]}
    if not response.ok:
        message = payload.get("message", payload.get("error", response.reason)) if isinstance(payload, dict) else response.reason
        raise RuntimeError(f"ONS retornou {response.status_code}: {message}")
    return payload


def as_items(payload):
    if isinstance(payload, list):
        return payload
    return payload.get("items", []) if isinstance(payload, dict) else []


def load_attachments(items, agent, competence):
    pairs = set()
    queries = []
    for item in items:
        creditor = item.get("counterpartyAmseId")
        payer = item.get("ownerAmseId")
        reference = item.get("referenceMonth", competence)
        pair = (creditor, payer, reference)
        if not creditor or not payer or pair in pairs:
            continue
        pairs.add(pair)
        queries.append((creditor, payer, reference, item.get("creditorLegalName", creditor)))

    def fetch_pair(query):
        creditor, payer, reference, creditor_name = query
        try:
            payload = nexus_request("GET", "/payment-attachments", agent, params={"creditorAmseId": creditor, "payerAmseId": payer, "referenceMonth": reference})
            return [{**attachment, "creditorName": creditor_name, "referenceMonth": reference} for attachment in as_items(payload)]
        except (RuntimeError, requests.RequestException):
            return []

    with ThreadPoolExecutor(max_workers=min(8, len(queries) or 1)) as executor:
        batches = executor.map(fetch_pair, queries)
        attachments = [attachment for batch in batches for attachment in batch]
    return attachments


@app.get("/")
def dashboard():
    return render_template("dashboard.html")


@app.get("/api/agents")
def agents():
    try:
        agent = request.args.get("agent") or env_value("ONS_AMSE", "amse", default="4284")
        return jsonify({"items": as_items(nexus_request("GET", "/me/agents", agent))})
    except (RuntimeError, requests.RequestException) as error:
        return jsonify({"error": str(error)}), 502


@app.get("/api/dashboard")
def dashboard_data():
    competence = request.args.get("referenceMonth") or env_value("ONS_COMPETENCIA", "competencia", default="2026-08")
    agent = request.args.get("agent") or env_value("ONS_AMSE", "amse", default="4284")
    try:
        avd_items = as_items(nexus_request("GET", "/avc-avd", agent, params={"referenceMonth": competence, "pageSize": 200}))
        status_counts = {}
        for item in avd_items:
            status = item.get("status") or item.get("validationStatus") or "Sem status"
            status_counts[status] = status_counts.get(status, 0) + 1
        return jsonify({"competence": competence, "agent": agent, "items": avd_items, "attachments": [], "summary": {"total": len(avd_items), "attachments": 0, "withValue": sum(1 for item in avd_items if item.get("amount") is not None or item.get("value") is not None), "statuses": status_counts}})
    except (RuntimeError, requests.RequestException) as error:
        return jsonify({"error": str(error)}), 502


@app.get("/api/attachments")
def attachments_data():
    competence = request.args.get("referenceMonth") or env_value("ONS_COMPETENCIA", "competencia", default="2026-08")
    agent = request.args.get("agent") or env_value("ONS_AMSE", "amse", default="4284")
    try:
        items = as_items(nexus_request("GET", "/avc-avd", agent, params={"referenceMonth": competence, "pageSize": 200}))
        attachments = load_attachments(items, agent, competence)
        return jsonify({"items": attachments, "total": len(attachments)})
    except (RuntimeError, requests.RequestException) as error:
        return jsonify({"error": str(error)}), 502


@app.get("/api/files/<file_id>/download")
def download_file(file_id):
    agent = request.args.get("agent") or env_value("ONS_AMSE", "amse", default="4284")
    requested_name = secure_filename(request.args.get("name", "documento")) or "documento"
    try:
        response = requests.get(f"{FILES_API}/files/download", headers=client_headers(agent), params={"ids": file_id}, timeout=30)
        response.raise_for_status()
        results = response.json().get("results", [])
        if not results or not results[0].get("ok") or not results[0].get("url"):
            raise RuntimeError("O servico de arquivos nao retornou uma URL assinada.")
        binary = requests.get(results[0]["url"], timeout=60)
        binary.raise_for_status()
        return send_file(BytesIO(binary.content), as_attachment=True, download_name=requested_name, mimetype=binary.headers.get("Content-Type"))
    except (RuntimeError, requests.RequestException, ValueError) as error:
        return jsonify({"error": str(error)}), 502


@app.post("/api/report")
def report():
    data = request.get_json(silent=True) or {}
    competence = data.get("referenceMonth") or env_value("ONS_COMPETENCIA", "competencia", default="2026-08")
    agent = data.get("agent") or env_value("ONS_AMSE", "amse", default="4284")
    file_format = data.get("format", "pdf").lower()
    if file_format not in {"pdf", "xlsx"}:
        return jsonify({"error": "Formato deve ser pdf ou xlsx."}), 400
    try:
        response = requests.post(f"{API}/avc-avd/report", headers=client_headers(agent), json={"format": file_format, "referenceMonth": competence, "type": "AVD", "agents": [agent]}, timeout=120)
        response.raise_for_status()
        output = Path(__file__).parent / "saida"
        output.mkdir(exist_ok=True)
        target = output / f"relatorio_{agent}_{competence}.{file_format}"
        target.write_bytes(response.content)
        return send_file(target, as_attachment=True, download_name=target.name)
    except (RuntimeError, requests.RequestException) as error:
        return jsonify({"error": str(error)}), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False, use_reloader=False)