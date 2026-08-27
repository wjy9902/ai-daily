from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

import httpx

from ai_daily.persona_models import (
    AuthorizationRecord,
    OperationReceipt,
    PersonaEdition,
    ReleaseAttestation,
    RenderReceipt,
    canonical_json,
)

WECHAT_API = "https://api.weixin.qq.com/cgi-bin"


class WechatPublicationError(RuntimeError):
    pass


class WechatHTTPError(WechatPublicationError):
    def __init__(self, status_code: int, endpoint: str) -> None:
        super().__init__(f"WeChat HTTP error {status_code} at {endpoint}")
        self.status_code = status_code
        self.endpoint = endpoint


class WechatAPIError(WechatPublicationError):
    RETRYABLE_CODES: ClassVar[frozenset[int]] = frozenset(
        {-1, 40001, 40014, 42001, 45009}
    )

    def __init__(self, errcode: int) -> None:
        super().__init__(f"WeChat API error {errcode}")
        self.errcode = errcode
        self.retryable = errcode in self.RETRYABLE_CODES


class WechatResponseError(WechatPublicationError):
    pass


class WechatPublicationUnknown(WechatPublicationError):
    def __init__(self, message: str, receipt: OperationReceipt | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


def sign_authorization(unsigned: dict[str, Any], key: str) -> AuthorizationRecord:
    provisional = AuthorizationRecord.model_validate({**unsigned, "signature": "0" * 64})
    payload = provisional.model_dump(mode="json")
    payload.pop("signature")
    return provisional.model_copy(update={"signature": _sign(payload, key)})


def hmac_keys_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(_decode_hmac_key(left), _decode_hmac_key(right))


def verify_authorization(
    record: AuthorizationRecord,
    key: str,
    *,
    column_id: str,
    account_stable_id: str,
    action: str,
    now: datetime | None = None,
) -> None:
    payload = record.model_dump(mode="json")
    signature = payload.pop("signature")
    if not hmac.compare_digest(signature, _sign(payload, key)):
        raise WechatPublicationError("authorization signature mismatch")
    moment = now or datetime.now(UTC)
    if record.revoked_at or not record.valid_from <= moment < record.expires_at:
        raise WechatPublicationError("authorization is inactive")
    if record.environment != "production":
        raise WechatPublicationError("authorization is not for production")
    if record.column_id != column_id or record.account_stable_id != account_stable_id:
        raise WechatPublicationError("authorization scope mismatch")
    if action not in record.allowed_actions:
        raise WechatPublicationError("operation is not authorized")


def attest_release(
    edition: PersonaEdition,
    receipt: RenderReceipt,
    authorization: AuthorizationRecord,
    account_fingerprint: str,
    key_id: str,
    key: str,
) -> ReleaseAttestation:
    unsigned = {
        "schema_version": 1,
        "column_id": edition.column_id,
        "target_date": edition.target_date.isoformat(),
        "publication_slot": (
            f"{authorization.account_stable_id}:{edition.column_id}:"
            f"{edition.target_date.isoformat()}"
        ),
        "edition_payload_sha256": edition.payload_sha256,
        "markdown_sha256": receipt.markdown_sha256,
        "html_sha256": receipt.html_sha256,
        "renderer_version": receipt.renderer_version,
        "template_version": receipt.template_version,
        "authorization_id": authorization.authorization_id,
        "account_stable_id": authorization.account_stable_id,
        "account_fingerprint": account_fingerprint,
        "key_id": key_id,
    }
    provisional = ReleaseAttestation.model_validate({**unsigned, "signature": "0" * 64})
    payload = provisional.model_dump(mode="json")
    payload.pop("signature")
    return provisional.model_copy(update={"signature": _sign(payload, key)})


def verify_attestation(
    attestation: ReleaseAttestation,
    edition: PersonaEdition,
    receipt: RenderReceipt,
    key: str,
) -> None:
    payload = attestation.model_dump(mode="json")
    signature = payload.pop("signature")
    if not hmac.compare_digest(signature, _sign(payload, key)):
        raise WechatPublicationError("release attestation signature mismatch")
    expected = (
        edition.payload_sha256,
        receipt.markdown_sha256,
        receipt.html_sha256,
        receipt.renderer_version,
        receipt.template_version,
    )
    actual = (
        attestation.edition_payload_sha256,
        attestation.markdown_sha256,
        attestation.html_sha256,
        attestation.renderer_version,
        attestation.template_version,
    )
    if actual != expected:
        raise WechatPublicationError("release attestation does not bind rendered bytes")


def account_fingerprint(app_id: str, stable_id: str) -> str:
    return hashlib.sha256(f"{stable_id}\0{app_id}".encode()).hexdigest()


class PublicationSlots:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """CREATE TABLE IF NOT EXISTS slots (
                publication_slot TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                retryable INTEGER NOT NULL DEFAULT 0,
                attempt_id TEXT NOT NULL,
                remote_id TEXT,
                updated_at TEXT NOT NULL
                )"""
            )

    def claim(self, publication_slot: str, attempt_id: str) -> None:
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT state, retryable FROM slots WHERE publication_slot = ?",
                (publication_slot,),
            ).fetchone()
            reclaimable = row and (
                (row[0] == "failed" and row[1] == 1) or row[0] == "prepared"
            )
            if row and not reclaimable:
                raise WechatPublicationError(
                    f"publication slot already claimed with state={row[0]}"
                )
            database.execute(
                """INSERT INTO slots(publication_slot,state,retryable,attempt_id,updated_at)
                VALUES(?, 'prepared', 0, ?, ?)
                ON CONFLICT(publication_slot) DO UPDATE SET
                state='prepared', retryable=0, attempt_id=excluded.attempt_id,
                remote_id=NULL, updated_at=excluded.updated_at""",
                (publication_slot, attempt_id, datetime.now(UTC).isoformat()),
            )

    def update(
        self,
        publication_slot: str,
        state: str,
        *,
        retryable: bool = False,
        remote_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        with self._connect() as database:
            ownership = " AND attempt_id=?" if attempt_id is not None else ""
            parameters: tuple[Any, ...] = (
                state,
                int(retryable),
                remote_id,
                datetime.now(UTC).isoformat(),
                publication_slot,
                *((attempt_id,) if attempt_id is not None else ()),
            )
            cursor = database.execute(
                """UPDATE slots SET state=?, retryable=?,
                remote_id=COALESCE(?, remote_id), updated_at=?
                WHERE publication_slot=?"""
                + ownership,
                parameters,
            )
            if cursor.rowcount != 1:
                raise WechatPublicationError("publication slot disappeared or ownership changed")

    def get(self, publication_slot: str) -> dict[str, Any] | None:
        with self._connect() as database:
            row = database.execute(
                """SELECT state,retryable,attempt_id,remote_id,updated_at
                FROM slots WHERE publication_slot=?""",
                (publication_slot,),
            ).fetchone()
        if row is None:
            return None
        return {
            "state": row[0],
            "retryable": bool(row[1]),
            "attempt_id": row[2],
            "remote_id": row[3],
            "updated_at": row[4],
        }

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)


class WechatClient:
    def __init__(self, app_id: str, app_secret: str, client: httpx.AsyncClient) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.client = client

    async def probe(self) -> dict[str, Any]:
        token = await self.access_token()
        draft = await self._get("draft/count", token)
        freepublish = await self._post(
            "freepublish/batchget", token, {"offset": 0, "count": 1, "no_content": 1}
        )
        return {
            "token": "ok",
            "draft": _capability(draft),
            "freepublish_read": _capability(freepublish),
            "mass_send": "disabled_by_design",
        }

    async def create_draft(self, article: dict[str, Any], token: str) -> tuple[str, dict[str, Any]]:
        payload = {"articles": [article]}
        response = await self._post("draft/add", token, payload)
        _raise_api_error(response)
        media_id = str(response.get("media_id", ""))
        if not media_id:
            raise WechatResponseError("draft/add returned no media_id")
        return media_id, payload

    async def verify_draft(
        self, media_id: str, expected: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        token = token or await self.access_token()
        response = await self._post("draft/get", token, {"media_id": media_id})
        _raise_api_error(response)
        items = response.get("news_item") or []
        if len(items) != 1 or not isinstance(items[0], dict):
            raise WechatPublicationError("remote draft must contain exactly one article")
        remote = items[0]
        for field in (
            "title",
            "author",
            "digest",
            "content_source_url",
            "thumb_media_id",
            "need_open_comment",
            "only_fans_can_comment",
        ):
            if remote.get(field) != expected.get(field):
                raise WechatPublicationError(f"remote draft metadata mismatch: {field}")
        content = str(remote.get("content", ""))
        if canonical_draft_html_sha256(content) != canonical_draft_html_sha256(
            str(expected["content"])
        ):
            raise WechatPublicationError("remote draft HTML does not match submitted HTML")
        return response

    async def find_draft_by_marker(self, marker: str) -> str | None:
        token = await self.access_token()
        offset = 0
        total = 1
        while offset < total:
            response = await self._post(
                "draft/batchget",
                token,
                {"offset": offset, "count": 20, "no_content": 0},
            )
            _raise_api_error(response)
            total = int(response.get("total_count", 0) or 0)
            if total > 10_000:
                raise WechatPublicationError(
                    "draft inventory exceeds safe reconciliation scan limit"
                )
            items = response.get("item") or []
            for item in items:
                articles = (item.get("content") or {}).get("news_item") or []
                if any(
                    marker in str(article.get(field, ""))
                    for article in articles
                    for field in ("content", "content_source_url")
                ):
                    return str(item.get("media_id"))
            if not items:
                break
            offset += len(items)
        return None

    async def access_token(self) -> str:
        response = await self.client.post(
            f"{WECHAT_API}/stable_token",
            json={
                "grant_type": "client_credential",
                "appid": self.app_id,
                "secret": self.app_secret,
                "force_refresh": False,
            },
        )
        _raise_http_status(response, "stable_token")
        payload = response.json()
        if not isinstance(payload, dict):
            raise WechatResponseError("token endpoint returned a non-object response")
        _raise_api_error(payload)
        token = str(payload.get("access_token", ""))
        if not token:
            raise WechatPublicationError("token endpoint returned no access_token")
        return token

    async def _get(self, endpoint: str, token: str) -> dict[str, Any]:
        response = await self.client.get(f"{WECHAT_API}/{endpoint}", params={"access_token": token})
        _raise_http_status(response, endpoint)
        payload = response.json()
        if not isinstance(payload, dict):
            raise WechatResponseError(f"{endpoint} returned a non-object response")
        return payload

    async def _post(self, endpoint: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            f"{WECHAT_API}/{endpoint}",
            params={"access_token": token},
            json=payload,
        )
        _raise_http_status(response, endpoint)
        result = response.json()
        if not isinstance(result, dict):
            raise WechatResponseError(f"{endpoint} returned a non-object response")
        return result


async def publish_draft(
    *,
    client: WechatClient,
    slots: PublicationSlots,
    edition: PersonaEdition,
    html: str,
    receipt: RenderReceipt,
    authorization: AuthorizationRecord,
    attestation: ReleaseAttestation,
    auth_key: str,
    release_key: str,
    account_stable_id: str,
    cover_media_id: str,
    author: str,
) -> OperationReceipt:
    if hmac_keys_equal(auth_key, release_key):
        raise WechatPublicationError("authorization and release keys must differ")
    verify_authorization(
        authorization,
        auth_key,
        column_id=edition.column_id,
        account_stable_id=account_stable_id,
        action="create_draft",
    )
    verify_attestation(attestation, edition, receipt, release_key)
    if not edition.hash_is_valid():
        raise WechatPublicationError("edition hash is invalid")
    if hashlib.sha256(html.encode("utf-8")).hexdigest() != receipt.html_sha256:
        raise WechatPublicationError("submitted HTML does not match render receipt")
    if attestation.account_stable_id != account_stable_id:
        raise WechatPublicationError("attestation account mismatch")
    if attestation.authorization_id != authorization.authorization_id:
        raise WechatPublicationError("attestation authorization mismatch")
    if attestation.account_fingerprint != account_fingerprint(client.app_id, account_stable_id):
        raise WechatPublicationError("attestation account fingerprint mismatch")
    attempt_id = uuid.uuid4().hex
    slot = attestation.publication_slot
    request = {"articles": [draft_article_payload(edition, html, cover_media_id, author)]}
    request_sha256 = hashlib.sha256(canonical_json(request)).hexdigest()
    slots.claim(slot, attempt_id)
    try:
        token = await client.access_token()
    except httpx.RequestError as error:
        slots.update(slot, "failed", retryable=True, attempt_id=attempt_id)
        raise WechatPublicationError("token request failed before draft send") from error
    except WechatAPIError as error:
        slots.update(slot, "failed", retryable=error.retryable, attempt_id=attempt_id)
        raise
    except WechatResponseError:
        slots.update(slot, "failed", retryable=True, attempt_id=attempt_id)
        raise
    except Exception:
        slots.update(slot, "failed", retryable=False, attempt_id=attempt_id)
        raise
    slots.update(slot, "sending", attempt_id=attempt_id)
    try:
        media_id, request = await client.create_draft(request["articles"][0], token)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
        slots.update(slot, "failed", retryable=True, attempt_id=attempt_id)
        raise WechatPublicationError("draft connection failed before send") from error
    except httpx.RequestError as error:
        slots.update(slot, "unknown", attempt_id=attempt_id)
        raise WechatPublicationUnknown(
            "draft result is unknown; reconcile before retry",
            _unknown_receipt(attempt_id, slot, request_sha256, attestation, error),
        ) from error
    except WechatAPIError as error:
        slots.update(slot, "failed", retryable=error.retryable, attempt_id=attempt_id)
        raise
    except WechatResponseError as error:
        slots.update(slot, "unknown", attempt_id=attempt_id)
        raise WechatPublicationUnknown(
            "draft response is ambiguous; reconcile before retry",
            _unknown_receipt(attempt_id, slot, request_sha256, attestation, error),
        ) from error
    except WechatHTTPError as error:
        if error.status_code >= 500:
            slots.update(slot, "unknown", attempt_id=attempt_id)
            raise WechatPublicationUnknown(
                "draft result is unknown; reconcile before retry",
                _unknown_receipt(attempt_id, slot, request_sha256, attestation, error),
            ) from error
        slots.update(slot, "failed", retryable=False, attempt_id=attempt_id)
        raise
    except ValueError as error:
        slots.update(slot, "unknown", attempt_id=attempt_id)
        raise WechatPublicationUnknown(
            "draft response could not be decoded; reconcile before retry",
            _unknown_receipt(attempt_id, slot, request_sha256, attestation, error),
        ) from error
    except Exception:
        slots.update(slot, "failed", retryable=False, attempt_id=attempt_id)
        raise
    slots.update(slot, "submitted", remote_id=media_id, attempt_id=attempt_id)
    try:
        response = await client.verify_draft(media_id, request["articles"][0], token)
    except (httpx.RequestError, WechatResponseError) as error:
        slots.update(slot, "unknown", remote_id=media_id, attempt_id=attempt_id)
        raise WechatPublicationUnknown(
            "draft exists but verification is unknown; reconcile before retry",
            _unknown_receipt(attempt_id, slot, request_sha256, attestation, error).model_copy(
                update={"remote_id": media_id}
            ),
        ) from error
    except WechatHTTPError as error:
        if error.status_code >= 500:
            slots.update(slot, "unknown", remote_id=media_id, attempt_id=attempt_id)
            raise WechatPublicationUnknown(
                "draft exists but verification is unknown; reconcile before retry",
                _unknown_receipt(attempt_id, slot, request_sha256, attestation, error).model_copy(
                    update={"remote_id": media_id}
                ),
            ) from error
        slots.update(
            slot,
            "remote_mismatch",
            retryable=False,
            remote_id=media_id,
            attempt_id=attempt_id,
        )
        raise
    except Exception:
        slots.update(
            slot,
            "remote_mismatch",
            retryable=False,
            remote_id=media_id,
            attempt_id=attempt_id,
        )
        raise
    slots.update(slot, "verified", remote_id=media_id, attempt_id=attempt_id)
    return OperationReceipt(
        kind="wechat_draft",
        attempt_id=attempt_id,
        publication_slot=slot,
        operation="create_draft",
        state="verified",
        request_sha256=request_sha256,
        created_at=datetime.now(UTC),
        account_fingerprint=attestation.account_fingerprint,
        response_sha256=hashlib.sha256(canonical_json(response)).hexdigest(),
        remote_id=media_id,
    )


async def reconcile_draft(
    *,
    client: WechatClient,
    slots: PublicationSlots,
    edition: PersonaEdition,
    receipt: RenderReceipt,
    authorization: AuthorizationRecord,
    attestation: ReleaseAttestation,
    auth_key: str,
    release_key: str,
    account_stable_id: str,
    html: str,
    cover_media_id: str,
    author: str,
) -> OperationReceipt:
    if hmac_keys_equal(auth_key, release_key):
        raise WechatPublicationError("authorization and release keys must differ")
    verify_authorization(
        authorization,
        auth_key,
        column_id=edition.column_id,
        account_stable_id=account_stable_id,
        action="reconcile_draft",
    )
    verify_attestation(attestation, edition, receipt, release_key)
    if not edition.hash_is_valid():
        raise WechatPublicationError("edition hash is invalid")
    if hashlib.sha256(html.encode("utf-8")).hexdigest() != receipt.html_sha256:
        raise WechatPublicationError("submitted HTML does not match render receipt")
    if attestation.account_stable_id != account_stable_id:
        raise WechatPublicationError("attestation account mismatch")
    if attestation.account_fingerprint != account_fingerprint(client.app_id, account_stable_id):
        raise WechatPublicationError("attestation account fingerprint mismatch")
    slot = attestation.publication_slot
    current = slots.get(slot)
    reconcilable = {
        "sending",
        "submitted",
        "unknown",
        "remote_mismatch",
        "verified",
    }
    legacy_failed_remote = bool(
        current and current["state"] == "failed" and current.get("remote_id")
    )
    if current is None or (current["state"] not in reconcilable and not legacy_failed_remote):
        raise WechatPublicationError("publication slot state cannot be reconciled safely")
    media_id = current.get("remote_id") or await client.find_draft_by_marker(edition.payload_sha256)
    if media_id is None:
        raise WechatPublicationUnknown("no matching remote draft found; slot remains unknown")
    expected = draft_article_payload(edition, html, cover_media_id, author)
    response = await client.verify_draft(media_id, expected)
    slots.update(
        slot,
        "verified",
        remote_id=media_id,
        attempt_id=str(current["attempt_id"]),
    )
    return OperationReceipt(
        kind="wechat_reconcile",
        attempt_id=str(current["attempt_id"]),
        publication_slot=slot,
        operation="reconcile_draft",
        state="verified",
        request_sha256=hashlib.sha256(canonical_json({"articles": [expected]})).hexdigest(),
        created_at=datetime.now(UTC),
        account_fingerprint=attestation.account_fingerprint,
        response_sha256=hashlib.sha256(canonical_json(response)).hexdigest(),
        remote_id=media_id,
    )


def _sign(payload: dict[str, Any], key: str) -> str:
    return hmac.new(_decode_hmac_key(key), canonical_json(payload), hashlib.sha256).hexdigest()


def _decode_hmac_key(key: str) -> bytes:
    try:
        key_bytes = bytes.fromhex(key)
    except ValueError:
        key_bytes = b""
    if len(key_bytes) != 32:
        raise WechatPublicationError(
            "HMAC key must be exactly 32 bytes encoded as 64 hex characters"
        )
    return key_bytes


def _unknown_receipt(
    attempt_id: str,
    slot: str,
    request_sha256: str,
    attestation: ReleaseAttestation,
    error: Exception,
) -> OperationReceipt:
    return OperationReceipt(
        kind="wechat_draft",
        attempt_id=attempt_id,
        publication_slot=slot,
        operation="create_draft",
        state="unknown",
        request_sha256=request_sha256,
        created_at=datetime.now(UTC),
        account_fingerprint=attestation.account_fingerprint,
        error_code=type(error).__name__,
    )


def draft_article_payload(
    edition: PersonaEdition,
    html: str,
    cover_media_id: str,
    author: str,
) -> dict[str, Any]:
    source_url = str(edition.source_links[0]).split("#", 1)[0]
    article = {
        "title": edition.title_block.text,
        "author": author,
        "digest": edition.digest_block.text,
        "content": html,
        "content_source_url": f"{source_url}#jiayu-{edition.payload_sha256}",
        "thumb_media_id": cover_media_id,
        "need_open_comment": 1,
        "only_fans_can_comment": 0,
    }
    limits = {
        "title": 32,
        "author": 16,
        "digest": 120,
        "content": 20_000,
        "content_source_url": 1_024,
    }
    for field, limit in limits.items():
        if not 0 < len(str(article[field])) <= limit:
            raise WechatPublicationError(f"draft field exceeds WeChat limit: {field}")
    if len(html.encode("utf-8")) >= 1_000_000:
        raise WechatPublicationError("draft HTML exceeds 1 MB")
    if not cover_media_id.strip():
        raise WechatPublicationError("permanent cover media_id is required")
    return article


class _DraftHTMLCanonicalizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[object] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tokens.append(("start", tag, sorted((name, value or "") for name, value in attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        self.tokens.append(("end", tag))

    def handle_data(self, data: str) -> None:
        self.tokens.append(("data", data))


def canonical_draft_html_sha256(content: str) -> str:
    parser = _DraftHTMLCanonicalizer()
    parser.feed(content)
    payload = json.dumps(parser.tokens, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _raise_api_error(payload: dict[str, Any]) -> None:
    code = int(payload.get("errcode", 0) or 0)
    if code:
        raise WechatAPIError(code)


def _raise_http_status(response: httpx.Response, endpoint: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        raise WechatHTTPError(response.status_code, endpoint) from None


def _capability(payload: dict[str, Any]) -> dict[str, Any]:
    code = int(payload.get("errcode", 0) or 0)
    return {
        "available": code == 0,
        "errcode": code,
        "count": payload.get("total_count"),
    }
