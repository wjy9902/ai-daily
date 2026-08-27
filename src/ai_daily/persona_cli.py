from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from ai_daily.artifacts import write_artifact
from ai_daily.config import AppConfig, Secrets
from ai_daily.persona_models import (
    AuthorizationRecord,
    DailyAutoManifest,
    OperationReceipt,
    PersonaEdition,
    RenderReceipt,
    WechatTarget,
    canonical_json,
)
from ai_daily.persona_render import render_persona
from ai_daily.persona_wechat import (
    PublicationSlots,
    WechatClient,
    WechatPublicationError,
    WechatPublicationUnknown,
    account_fingerprint,
    attest_release,
    draft_article_payload,
    hmac_keys_equal,
    publish_draft,
    reconcile_draft,
    sign_authorization,
)
from ai_daily.site_publisher import SiteLayout, write_persona_status

BEIJING = ZoneInfo("Asia/Shanghai")


async def persona_draft(
    args: argparse.Namespace,
    config: AppConfig,
    layout: SiteLayout,
    edition: PersonaEdition,
) -> int:
    target, target_path = _build_target(args, config, layout, edition)
    attestation_path = _attestation_path(layout, edition.target_date)
    write_artifact(attestation_path, target.attestation)
    if not args.execute:
        _write_prepared_status(layout, target, target_path)
        return 0
    try:
        receipt = await _execute_target(layout, target)
    except WechatPublicationUnknown as error:
        _write_unknown_status(layout, target, target_path, attestation_path, error)
        return 2
    except WechatPublicationError as error:
        _write_failed_status(layout, target, target_path, error)
        return 1
    receipt_path = attestation_path.with_name("wechat-receipt.json")
    write_artifact(receipt_path, receipt)
    action = "draft_reconciled" if receipt.kind == "wechat_reconcile" else "draft_verified"
    _write_verified_status(layout, target, receipt, receipt_path, action)
    return 0


def _build_target(
    args: argparse.Namespace,
    config: AppConfig,
    layout: SiteLayout,
    edition: PersonaEdition,
) -> tuple[WechatTarget, Path]:
    persona = config.persona
    if persona is None or persona.publish_mode != "draft_only":
        raise ValueError("persona WeChat publishing is disabled")
    if args.authorization is None:
        raise ValueError("--authorization is required for draft mode")
    rendered = render_persona(edition, _site_base_url(config))
    _persist_render_receipt(layout, edition.target_date, rendered.receipt)
    authorization = AuthorizationRecord.model_validate_json(
        args.authorization.read_text(encoding="utf-8")
    )
    app_id = _required_env("WECHAT_APP_ID")
    stable_id = _required_env("WECHAT_ACCOUNT_STABLE_ID")
    auth_key = _required_env("AI_DAILY_AUTH_HMAC_KEY")
    release_key = _required_env("AI_DAILY_RELEASE_HMAC_KEY")
    if hmac_keys_equal(auth_key, release_key):
        raise ValueError("authorization and release HMAC keys must differ")
    attestation = attest_release(
        edition,
        rendered.receipt,
        authorization,
        account_fingerprint(app_id, stable_id),
        os.environ.get("AI_DAILY_RELEASE_KEY_ID", "release-v1"),
        release_key,
    )
    cover_media_id = _required_env("WECHAT_COVER_MEDIA_ID")
    author = os.environ.get("WECHAT_ARTICLE_AUTHOR", "甲鱼")
    request = {"articles": [draft_article_payload(edition, rendered.html, cover_media_id, author)]}
    target = WechatTarget(
        edition=edition,
        html=rendered.html,
        render_receipt=rendered.receipt,
        authorization=authorization,
        attestation=attestation,
        cover_media_id=cover_media_id,
        author=author,
        request_sha256=hashlib.sha256(canonical_json(request)).hexdigest(),
    )
    return target, persist_wechat_target(layout, target)


async def _execute_target(layout: SiteLayout, target: WechatTarget) -> OperationReceipt:
    slots = PublicationSlots(layout.root / "wechat-slots.sqlite3")
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        wechat = WechatClient(
            _required_env("WECHAT_APP_ID"),
            _required_env("WECHAT_APP_SECRET"),
            client,
        )
        arguments: dict[str, Any] = {
            "client": wechat,
            "slots": slots,
            "edition": target.edition,
            "html": target.html,
            "receipt": target.render_receipt,
            "authorization": target.authorization,
            "attestation": target.attestation,
            "auth_key": _required_env("AI_DAILY_AUTH_HMAC_KEY"),
            "release_key": _required_env("AI_DAILY_RELEASE_HMAC_KEY"),
            "account_stable_id": _required_env("WECHAT_ACCOUNT_STABLE_ID"),
            "cover_media_id": target.cover_media_id,
            "author": target.author,
        }
        current = slots.get(target.attestation.publication_slot)
        retryable = current is None or current["state"] == "prepared" or (
            current["state"] == "failed" and current["retryable"]
        )
        if not retryable:
            return await reconcile_draft(**arguments)
        return await publish_draft(**arguments)


async def wechat_probe(args: argparse.Namespace) -> int:
    del args
    async with httpx.AsyncClient(timeout=30) as client:
        result = await WechatClient(
            _required_env("WECHAT_APP_ID"),
            _required_env("WECHAT_APP_SECRET"),
            client,
        ).probe()
    _emit(result)
    return 0


async def wechat_reconcile(args: argparse.Namespace) -> int:
    layout = _layout(args)
    target_date = _target_date(args.date)
    target = _load_reconciliation_target(layout, target_date, args.authorization)
    async with httpx.AsyncClient(timeout=45) as client:
        receipt = await reconcile_draft(
            client=WechatClient(
                _required_env("WECHAT_APP_ID"),
                _required_env("WECHAT_APP_SECRET"),
                client,
            ),
            slots=PublicationSlots(layout.root / "wechat-slots.sqlite3"),
            edition=target.edition,
            receipt=target.render_receipt,
            authorization=target.authorization,
            attestation=target.attestation,
            auth_key=_required_env("AI_DAILY_AUTH_HMAC_KEY"),
            release_key=_required_env("AI_DAILY_RELEASE_HMAC_KEY"),
            account_stable_id=_required_env("WECHAT_ACCOUNT_STABLE_ID"),
            html=target.html,
            cover_media_id=target.cover_media_id,
            author=target.author,
        )
    output = layout.persona_runs / target_date.isoformat() / "wechat-reconcile-receipt.json"
    write_artifact(output, receipt)
    _write_verified_status(layout, target, receipt, output, "draft_reconciled")
    return 0


def _load_reconciliation_target(
    layout: SiteLayout,
    target_date: date,
    authorization_path: Path | None,
) -> WechatTarget:
    if authorization_path is None:
        raise ValueError("--authorization is required")
    authorization = AuthorizationRecord.model_validate_json(
        authorization_path.read_text(encoding="utf-8")
    )
    target = WechatTarget.model_validate_json(
        layout.wechat_target_path(target_date).read_text(encoding="utf-8")
    )
    expected = {
        "articles": [
            draft_article_payload(
                target.edition,
                target.html,
                target.cover_media_id,
                target.author,
            )
        ]
    }
    if target.request_sha256 != hashlib.sha256(canonical_json(expected)).hexdigest():
        raise ValueError("immutable WeChat target request hash mismatch")
    return target.model_copy(update={"authorization": authorization})


async def authorize_wechat(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    unsigned = {
        "schema_version": 1,
        "authorization_id": uuid.uuid4().hex,
        "issuer": args.issuer,
        "column_id": args.column_id,
        "account_stable_id": _required_env("WECHAT_ACCOUNT_STABLE_ID"),
        "environment": "production",
        "allowed_actions": ["create_draft", "reconcile_draft"],
        "valid_from": now.isoformat(),
        "expires_at": (now + timedelta(days=args.valid_days)).isoformat(),
        "revoked_at": None,
        "key_id": os.environ.get("AI_DAILY_AUTH_KEY_ID", "auth-v1"),
    }
    record = sign_authorization(unsigned, _required_env("AI_DAILY_AUTH_HMAC_KEY"))
    write_artifact(args.output, record)
    _emit({"action": "authorization_created", "path": str(args.output)})
    return 0


def persist_wechat_target(layout: SiteLayout, target: WechatTarget) -> Path:
    path = layout.wechat_target_path(target.edition.target_date)
    if path.exists():
        _verify_same_target(path, target)
        return path
    candidate = path.with_name(f".{path.name}.{uuid.uuid4().hex}.candidate")
    write_artifact(candidate, target)
    try:
        try:
            os.link(candidate, path)
        except FileExistsError:
            _verify_same_target(path, target)
    finally:
        candidate.unlink(missing_ok=True)
    return path


def _persist_render_receipt(
    layout: SiteLayout, target_date: date, receipt: RenderReceipt
) -> Path:
    path = layout.persona_render_receipt_path(target_date)
    if path.exists():
        existing = RenderReceipt.model_validate_json(path.read_text(encoding="utf-8"))
        if existing != receipt:
            raise ValueError("immutable render receipt already exists for this publication slot")
        return path
    write_artifact(path, receipt)
    return path


def _verify_same_target(path: Path, target: WechatTarget) -> None:
    existing = WechatTarget.model_validate_json(path.read_text(encoding="utf-8"))
    if existing != target:
        raise ValueError("immutable WeChat target already exists for this publication slot")


def _attestation_path(layout: SiteLayout, target_date: date) -> Path:
    return layout.persona_runs / target_date.isoformat() / "attestation.json"


def _write_prepared_status(layout: SiteLayout, target: WechatTarget, target_path: Path) -> None:
    edition = target.edition
    _write_manifest(layout, target, "not_attempted", None)
    write_persona_status(
        layout,
        {
            "target_date": edition.target_date.isoformat(),
            "editorial_state": "ready",
            "site_state": "published",
            "wechat_state": "not_attempted",
            "aggregate_state": "ready",
            "action": "draft_prepared",
            "target": str(target_path),
        },
    )
    _emit({"action": "draft_prepared", "target": str(target_path)})


def _write_unknown_status(
    layout: SiteLayout,
    target: WechatTarget,
    target_path: Path,
    attestation_path: Path,
    error: WechatPublicationUnknown,
) -> None:
    edition = target.edition
    receipt_path: Path | None = None
    if error.receipt is not None:
        receipt_path = attestation_path.with_name("wechat-receipt.json")
        write_artifact(receipt_path, error.receipt)
    _write_manifest(layout, target, "unknown", receipt_path)
    write_persona_status(
        layout,
        {
            "target_date": edition.target_date.isoformat(),
            "editorial_state": "ready",
            "site_state": "published",
            "wechat_state": "unknown",
            "aggregate_state": "partial",
            "action": "draft_unknown_reconcile_required",
            "target": str(target_path),
        },
    )
    _emit({"action": "draft_unknown_reconcile_required", "target": str(target_path)})


def _write_verified_status(
    layout: SiteLayout,
    target: WechatTarget,
    receipt: OperationReceipt,
    receipt_path: Path,
    action: str,
) -> None:
    target_date = target.edition.target_date
    _write_manifest(layout, target, "draft_verified", receipt_path)
    write_persona_status(
        layout,
        {
            "target_date": target_date.isoformat(),
            "editorial_state": "ready",
            "site_state": "published",
            "wechat_state": "draft_verified",
            "aggregate_state": "draft_complete",
            "action": action,
            "remote_id": receipt.remote_id,
        },
    )
    _emit({"action": action, "remote_id": receipt.remote_id})


def _write_failed_status(
    layout: SiteLayout,
    target: WechatTarget,
    target_path: Path,
    error: WechatPublicationError,
) -> None:
    error_code = type(error).__name__
    _write_manifest(layout, target, "failed", None)
    write_persona_status(
        layout,
        {
            "target_date": target.edition.target_date.isoformat(),
            "editorial_state": "ready",
            "site_state": "published",
            "wechat_state": "failed",
            "aggregate_state": "partial",
            "action": "draft_failed",
            "target": str(target_path),
            "error_code": error_code,
        },
    )
    _emit({"action": "draft_failed", "error_code": error_code})


def _write_manifest(
    layout: SiteLayout,
    target: WechatTarget,
    wechat_state: Literal["not_attempted", "draft_verified", "unknown", "failed"],
    receipt_path: Path | None,
) -> None:
    manifest = DailyAutoManifest(
        publication_slot=target.attestation.publication_slot,
        edition_payload_sha256=target.edition.payload_sha256,
        account_stable_id=target.attestation.account_stable_id,
        account_fingerprint=target.attestation.account_fingerprint,
        authorization_id=target.authorization.authorization_id,
        release_attestation=target.attestation,
        editorial_state="ready",
        site_state="published",
        wechat_state=wechat_state,
        render_receipt_path=str(layout.persona_render_receipt_path(target.edition.target_date)),
        wechat_receipt_path=str(receipt_path) if receipt_path else None,
    )
    write_artifact(layout.persona_manifest_path(target.edition.target_date), manifest)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"required environment variable is missing: {name}")
    return value


def _layout(args: argparse.Namespace) -> SiteLayout:
    root = getattr(args, "site_root", None) or os.environ.get("AI_DAILY_SITE_ROOT")
    return SiteLayout(Path(root)) if root else SiteLayout(Path.cwd() / "site")


def _target_date(value: str | None) -> date:
    return date.fromisoformat(value) if value else datetime.now(BEIJING).date()


def _site_base_url(config: AppConfig) -> str:
    secrets = Secrets()
    return str(
        os.environ.get("AI_DAILY_SITE_BASE_URL")
        or secrets.site_base_url
        or config.pipeline.site_base_url
    )


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))
