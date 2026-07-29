# FastAPI backend для `NU-BSE/attestation`

Стартовый серверный контур, совместимый с текущими маршрутами мобильного клиента:

- `POST /attest/nonce`;
- `POST /attest/verify`;
- `GET /.well-known/jwks.json`;
- `GET /healthz` и `GET /readyz`.

## Что уже реализовано

- FastAPI с lifespan-инициализацией;
- Bearer JWT вместо небезопасного `x-user-id`;
- атомарный одноразовый nonce в Redis через Lua;
- PostgreSQL-реестр устройств и журнал результатов;
- реальный вызов Google Play Integrity `decodeIntegrityToken`;
- fail-closed allowlist Android package certificate SHA-256;
- RFC 8785 / JCS-канонизация операции;
- совместимый `SHA-256(nonce + payloadHash)` request hash;
- velocity/risk-сигналы в Redis;
- классификатор, перенесённый из TypeScript;
- постоянный локальный Ed25519 JIT-ключ и JWKS;
- Alembic-миграция;
- базовые unit-тесты.

## Важное ограничение первой версии

Строгий ASN.1-разбор расширения Android Key Description пока не реализован. Поэтому:

- Play Integrity может дать уровень `STANDARD` для нефинансового действия;
- `Strong Integrity` не считается эквивалентом StrongBox;
- `rootOfTrust` остаётся `UNVERIFIED`;
- переводы и добавление получателя блокируются до полноценной проверки Key Attestation.

Это намеренный fail-closed режим.

## Запуск локально

```bash
cd attestation-fastapi-backend
cp .env.example .env
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e ".[dev]"
python scripts/generate_dev_keys.py
docker compose up -d postgres redis
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Swagger будет доступен на `http://localhost:8000/docs`.

## Локальный access token

Только при `ALLOW_DEV_TOKEN_ENDPOINT=true`:

```bash
curl -X POST http://localhost:8000/dev/token \
  -H 'Content-Type: application/json' \
  -d '{"userId":"demo-user"}'
```

Далее передавайте токен:

```text
Authorization: Bearer <accessToken>
```

## Получение nonce

```bash
curl -X POST http://localhost:8000/attest/nonce \
  -H 'Authorization: Bearer <accessToken>' \
  -H 'Content-Type: application/json' \
  -d '{"clientTs": 1760000000000}'
```

## Google Play Integrity

Заполните в `.env`:

```text
ANDROID_PACKAGE_NAME=com.attestation.security
ANDROID_CERT_SHA256=<SHA-256 production signing certificate>
PLAY_INTEGRITY_PROJECT_NUMBER=<Google Cloud project number>
GOOGLE_SERVICE_ACCOUNT_FILE=/absolute/path/service-account.json
```

Service account должен иметь доступ к Play Integrity API. Файл ключа нельзя коммитить в репозиторий.

## Следующий этап

1. Полный ASN.1 parser Android Key Description.
2. Проверка подписей всей X.509-цепочки до доверенных Google Hardware Attestation Roots.
3. Точное сравнение `attestationChallenge`.
4. Декодирование `attestationSecurityLevel`, `rootOfTrust`, `verifiedBootState`, `osVersion` и patch levels.
5. TEE fallback при отсутствии StrongBox.
6. Apple App Attest и WebAuthn persistent storage.
7. KMS/HSM signer с публикацией нескольких `kid` при ротации.
"# attestation-backend" 
"# creepy-backend" 
