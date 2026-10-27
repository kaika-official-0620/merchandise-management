"""Exercise ONLY new fictional inventory on this Render staging service.

Run from its Render Shell. Passwords come from secret environment variables and
are never printed. There is no arbitrary origin, account, item ID, or delete
option. Existing inventory is never modified. The optional intake sequence
marks only the newly created fictional item as received by fictional staff.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from staging_environment import StagingConfigurationError, validate_environment


DEPLOYED_HOST = "kaika-platform-staging.onrender.com"
FIXTURE_NOTE = "【動作確認】スマホ相当の別セッションで編集。実在の商品ではありません。"
ROOT = Path(__file__).resolve().parents[1]


class VerificationStopped(RuntimeError):
    """Contains a constant check code only, never a raw HTTP exception/body."""


def same_origin_url(origin, target):
    try:
        base = urlsplit(origin)
        destination = urlsplit(urljoin(origin + "/", target))
        if (base.scheme != "https" or base.username or base.password or base.query or base.fragment
                or base.path not in {"", "/"} or base.port not in {None, 443}
                or destination.scheme != "https" or destination.username or destination.password
                or destination.hostname != base.hostname or destination.port not in {None, 443}
                or destination.fragment or "\\" in target):
            raise VerificationStopped("same_origin_https_required")
    except (ValueError, TypeError):
        raise VerificationStopped("same_origin_https_required") from None
    return destination.geturl()


class GuardedSession:
    def __init__(self, origin, user_agent, session_factory=None):
        if session_factory is None:
            import requests
            session_factory = requests.Session
        self.origin = origin.rstrip("/")
        same_origin_url(self.origin, self.origin)
        self.session = session_factory()
        self.session.trust_env = False  # No inherited proxy can receive secrets.
        self.session.headers.update({"User-Agent": user_agent})
        self.confirmed = False
        self.requests = 0

    def request(self, method, target, *, expected=None, **kwargs):
        url = same_origin_url(self.origin, target)
        if method != "GET" and not self.confirmed:
            raise VerificationStopped("anonymous_staging_confirmation_required")
        if any(key in kwargs for key in ("allow_redirects", "verify", "timeout", "stream", "auth")):
            raise VerificationStopped("transport_override_not_allowed")
        response = None
        try:
            response = self.session.request(method, url, allow_redirects=False, verify=True,
                                            timeout=(10, 60), stream=True, **kwargs)
            self.requests += 1
            same_origin_url(self.origin, response.url)
            if response.headers.get("X-Kaika-Environment") != "staging":
                raise VerificationStopped("staging_response_header_required")
            # A redirect is never followed automatically, including after POST.
            # Validate it now, before the caller can use its destination.
            if response.headers.get("Location"):
                same_origin_url(self.origin, response.headers["Location"])
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise VerificationStopped("response_size_limit")
                chunks.append(chunk)
            response._content = b"".join(chunks)
            response._content_consumed = True
            if expected is not None and response.status_code not in expected:
                raise VerificationStopped("unexpected_http_status_" + str(int(response.status_code)))
            return response
        except VerificationStopped:
            raise
        except Exception:
            # requests exceptions can contain a URL or other connection details.
            raise VerificationStopped("https_request_failed") from None
        finally:
            if response is not None:
                response.close()

    def confirm(self, expected_host):
        response = self.request("GET", "/", expected={200, 302, 303})
        if response.status_code == 200 and b'data-kaika-staging="true"' not in response.content:
            raise VerificationStopped("staging_top_marker_required")
        response = self.request("GET", "/healthz", expected={200})
        try:
            body = response.json()
        except Exception:
            raise VerificationStopped("staging_health_json_required") from None
        if not (body.get("status") == "ok" and body.get("database") == "postgres"
                and body.get("primary_domain") == expected_host):
            raise VerificationStopped("staging_health_identity_mismatch")
        response = self.request("GET", "/login", expected={200})
        if b'data-kaika-staging="true"' not in response.content:
            raise VerificationStopped("staging_login_marker_required")
        self.confirmed = True

    def close(self):
        self.session.close()


class Forms(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.forms, self.images, self.current, self.textarea = [], [], None, None
        self.feed(text)

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "form":
            self.current = {"id": values.get("id"), "action": values.get("action", ""),
                            "method": values.get("method", "get").lower(), "values": {}}
            self.forms.append(self.current)
        elif tag == "img":
            self.images.append(values)
        elif self.current is not None and values.get("name") and "disabled" not in values:
            name = values["name"]
            if tag == "input":
                kind = values.get("type", "text").lower()
                if kind in {"file", "submit", "button", "reset"}:
                    return
                if kind in {"checkbox", "radio"} and "checked" not in values:
                    return
                # Tokens and known scalar fields only; selecting intake items
                # is always explicit from this run's freshly created ID.
                if name not in {"item_ids", "remove_photo", "remove_additional"}:
                    self.current["values"][name] = values.get("value", "")
            elif tag == "textarea":
                self.textarea = name
                self.current["values"][name] = ""

    def handle_data(self, data):
        if self.current is not None and self.textarea:
            self.current["values"][self.textarea] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.textarea = None
        if tag == "form":
            self.current, self.textarea = None, None

    def select(self, *, form_id=None, action=None, token="submission_token"):
        found = [form for form in self.forms if form["method"] == "post"
                 and token in form["values"] and form["values"].get("csrf_token")
                 and (form_id is None or form["id"] == form_id)
                 and (action is None or form["values"].get("action") == action)]
        if len(found) != 1:
            raise VerificationStopped("unique_authenticated_form_required")
        return found[0]


def parse(response):
    return Forms(response.content.decode("utf-8", errors="strict"))


def form_data(client, path, *, form_id=None, action=None, token="submission_token"):
    response = client.request("GET", path, expected={200})
    document = parse(response)
    form = document.select(form_id=form_id, action=action, token=token)
    action_url = same_origin_url(client.origin, form["action"] or path)
    # Do not submit a token or any other field to an unexpected same-host route.
    if urlsplit(action_url).path != urlsplit(same_origin_url(client.origin, path)).path:
        raise VerificationStopped("form_action_changed")
    return dict(form["values"]), document, response


def redirected_id(client, response, prefix):
    location = same_origin_url(client.origin, response.headers.get("Location", ""))
    parsed = urlsplit(location)
    found = re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)", parsed.path)
    if not found or parsed.query:
        raise VerificationStopped("new_record_redirect_required")
    return int(found[1])


def fixture_photo():
    from PIL import Image, ImageDraw
    picture = Image.new("RGB", (160, 120), (239, 245, 255))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((20, 30, 140, 100), fill=(86, 114, 190))
    draw.text((28, 8), "KAIKA TEST ONLY", fill=(30, 41, 59))
    data = io.BytesIO()
    picture.save(data, format="PNG")
    return data.getvalue()


def photo_path(client, document):
    candidates = [image.get("src", "") for image in document.images if image.get("alt") == "メイン写真"]
    if len(candidates) != 1:
        raise VerificationStopped("new_item_photo_required")
    url = same_origin_url(client.origin, candidates[0])
    parsed = urlsplit(url)
    if not parsed.path.startswith("/static/uploads/") or parsed.query:
        raise VerificationStopped("same_origin_uploaded_photo_required")
    return parsed.path


class RunLedger:
    """Only our new non-secret manifests on the already marked staging disk.

    The MAC prevents a changed manifest from pointing the read-only replay at
    unrelated inventory. Neither the secret key nor the MAC appears in output.
    No upload directory is enumerated and no customer file is opened.
    """
    def __init__(self, uploads, secret_key):
        self.uploads = Path(uploads).resolve()
        marker = self.uploads / ".kaika-staging-volume"
        if not marker.is_file() or marker.is_symlink() or marker.read_text(encoding="utf-8").strip() != "staging":
            raise VerificationStopped("marked_staging_disk_required")
        self.directory = self.uploads / ".staging-verification"
        if self.directory.is_symlink() or self.directory.resolve() != self.directory:
            raise VerificationStopped("verification_directory_must_not_redirect")
        self.key = secret_key.encode("utf-8")

    def path(self, run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise VerificationStopped("valid_verification_run_id_required")
        path = self.directory / (run_id + ".json")
        if path.is_symlink() or path.resolve() != path:
            raise VerificationStopped("verification_manifest_must_not_redirect")
        return path

    def encoded(self, manifest):
        return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def write(self, manifest):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        path = self.path(manifest["run_id"])
        digest = hmac.new(self.key, self.encoded(manifest), hashlib.sha256).hexdigest()
        # New run only; never replace an existing manifest or inventory.
        with path.open("x", encoding="utf-8") as output:
            json.dump({"manifest": manifest, "mac": digest}, output, ensure_ascii=False)

    def read(self, run_id, host):
        try:
            path = self.path(run_id)
            if path.stat().st_size > 4096:
                raise VerificationStopped("verification_manifest_invalid")
            saved = json.loads(path.read_text(encoding="utf-8"))
            manifest = saved["manifest"]
            digest = hmac.new(self.key, self.encoded(manifest), hashlib.sha256).hexdigest()
            expected_keys = {"run_id", "host", "name", "item_id", "photo_path", "photo_sha256", "received", "intake_id"}
            if (not hmac.compare_digest(digest, saved["mac"]) or set(manifest) != expected_keys
                    or manifest["run_id"] != run_id or manifest["host"] != host
                    or manifest["name"] != "【動作確認・架空】" + run_id
                    or type(manifest["item_id"]) is not int or manifest["item_id"] < 1
                    or type(manifest["received"]) is not bool
                    or not re.fullmatch(r"[a-f0-9]{64}", manifest["photo_sha256"])
                    or not re.fullmatch(r"/static/uploads/[a-zA-Z0-9_./-]+", manifest["photo_path"])
                    or ".." in manifest["photo_path"].split("/")
                    or (manifest["received"] and (type(manifest["intake_id"]) is not int or manifest["intake_id"] < 1))
                    or (not manifest["received"] and manifest["intake_id"] is not None)):
                raise VerificationStopped("verification_manifest_invalid")
            return manifest
        except VerificationStopped:
            raise
        except Exception:
            raise VerificationStopped("verification_manifest_unavailable") from None


def exercise(config, environ, *, include_intake=False, session_factory=None, ledger=None, verify_run=None):
    origin = "https://" + config.host
    replay_requested = verify_run is not None
    clients = {}
    report = {"scope": "deployed staging HTTPS; independent PC/app cookie sessions; fictional new data only",
              "checks": [], "passed": 0, "failed": 0, "created_item": False, "created_intake": False,
              "native_device_tested": False, "external_delivery_tested": False,
              "mode": "inventory_read_only_replay" if replay_requested else "new_fictional_run",
              "login_history_may_update": True}
    stage = "anonymous_environment_confirmation"

    def passed(name):
        report["checks"].append({"name": name, "pass": True})

    def require(name, condition):
        if not condition:
            raise VerificationStopped(name)
        passed(name)

    def own_form(actor, item_id, expected_name):
        payload, document, response = form_data(clients[actor], f"/inventory/self/{item_id}/edit", form_id="self-inventory-form")
        require(actor + "_opens_only_this_runs_item", payload.get("product_name") == expected_name)
        return payload, document, response

    def own_detail(actor, item_id, expected_name, *markers):
        response = clients[actor].request("GET", f"/view/{item_id}", expected={200})
        text = response.content.decode("utf-8")
        require(actor + "_shared_item_visible", expected_name in text and all(marker in text for marker in markers))
        return response

    try:
        if config.host != DEPLOYED_HOST:
            raise VerificationStopped("dedicated_deployed_host_required")
        stage = "staging_verification_ledger"
        ledger = ledger or RunLedger(ROOT / "static/uploads", environ["SECRET_KEY"])
        saved = ledger.read(verify_run, config.host) if replay_requested else None
        run_id = verify_run if replay_requested else secrets.token_hex(16)
        report["run_id"] = run_id
        name, note = "【動作確認・架空】" + run_id, FIXTURE_NOTE
        stage = "anonymous_environment_confirmation"
        actors = {"pc": "business", "app": "business", "other": "normal"}
        if include_intake and not replay_requested:
            actors["staff"] = "admin"
        for actor, role in actors.items():
            user_agent = "Mozilla/5.0 KaikaApp/0.1.0 StagingVerification/1.0" if actor == "app" else "Mozilla/5.0 KaikaPCVerification/1.0"
            client = clients[actor] = GuardedSession(origin, user_agent, session_factory)
            client.confirm(config.host)
            passed(actor + "_anonymous_staging_confirmed")
            stage = actor + "_login"
            client.request("POST", "/login", data={"username": "staging_" + role,
                           "password": environ["STAGING_" + role.upper() + "_PASSWORD"]}, expected={302, 303})
            if role != "admin":
                form_data(client, "/inventory/self/new", form_id="self-inventory-form")
            else:
                client.request("GET", "/admin", expected={200})
            passed(actor + "_independent_cookie_login")

        if saved is not None:
            stage = "read_only_persistence_verification"
            item_id = saved["item_id"]
            report.update(item_id=item_id, photo_sha256=saved["photo_sha256"], intake_id=saved["intake_id"])
            custody = "開花で保管・管理" if saved["received"] else "自己保管"
            for actor in ("pc", "app"):
                own_detail(actor, item_id, name, note, custody)
                _, document, response = own_form(actor, item_id, name)
                require(actor + "_persisted_photo_path_matches", photo_path(clients[actor], document) == saved["photo_path"])
                photo = clients[actor].request("GET", saved["photo_path"], expected={200})
                require(actor + "_persisted_photo_sha256_matches", hashlib.sha256(photo.content).hexdigest() == saved["photo_sha256"])
                if saved["received"]:
                    require(actor + "_received_item_remains_locked", "この画面で編集できません" in response.content.decode("utf-8"))
                    intake = clients[actor].request("GET", f"/inventory/intakes/{saved['intake_id']}", expected={200})
                    require(actor + "_persisted_intake_visible", name in intake.content.decode("utf-8") and "開花での受領・登録が完了しました" in intake.content.decode("utf-8"))
            other = clients["other"].request("GET", f"/view/{item_id}")
            require("persisted_item_owner_boundary", other.status_code in {302, 403, 404} and name not in other.content.decode("utf-8", errors="replace"))
            passed("read_only_replay_complete")
            return report  # finally finalizes counts and closes all sessions.

        stage = "create_new_fictional_inventory"
        fields, _, _ = form_data(clients["pc"], "/inventory/self/new", form_id="self-inventory-form")
        fields.update(product_name=name, purchase_price="3000", listing_price="7000", expected_shipping="0",
                      expected_commission="0", notes="【動作確認】実物・取引・配送のない検証専用商品です。")
        report["created_item"] = "unknown_until_response"
        response = clients["pc"].request("POST", "/inventory/self/new", data=fields,
            files={"photo": ("fictional-check.png", fixture_photo(), "image/png")}, expected={302, 303})
        item_id = redirected_id(clients["pc"], response, "/view/")
        report["item_id"] = item_id
        own_detail("app", item_id, name, "自己保管")
        report["created_item"] = True
        passed("pc_registration_visible_in_app_session")

        stage = "edit_only_new_item_from_other_session"
        stale, _, _ = own_form("pc", item_id, name)
        fields, document, _ = own_form("app", item_id, name)
        original_photo = photo_path(clients["app"], document)
        fields["notes"] = note
        response = clients["app"].request("POST", f"/inventory/self/{item_id}/edit", data=fields, expected={302, 303})
        require("app_edit_keeps_same_item_id", redirected_id(clients["app"], response, "/view/") == item_id)
        own_detail("pc", item_id, name, note)
        stale["notes"] = "【動作確認】古い画面の上書きは拒否される想定。"
        clients["pc"].request("POST", f"/inventory/self/{item_id}/edit", data=stale, expected={409})
        passed("stale_pc_edit_is_rejected")
        own_detail("app", item_id, name, note)
        other = clients["other"].request("GET", f"/view/{item_id}")
        require("other_test_user_cannot_read_new_item", other.status_code in {302, 403, 404} and name not in other.content.decode("utf-8", errors="replace"))

        stage = "photo_shared_between_sessions"
        pc_photo = clients["pc"].request("GET", original_photo, expected={200})
        app_photo = clients["app"].request("GET", original_photo, expected={200})
        require("photo_bytes_match_between_pc_and_app", bool(pc_photo.content) and pc_photo.content == app_photo.content
                and pc_photo.headers.get("Content-Type", "").startswith("image/"))
        from PIL import Image
        with Image.open(io.BytesIO(pc_photo.content)) as image:
            require("uploaded_fixture_photo_decodes", image.width == 160 and image.height == 120)
        report["photo_sha256"] = hashlib.sha256(pc_photo.content).hexdigest()
        intake_id = None

        if include_intake:
            stage = "intake_only_new_fictional_item"
            fields, _, _ = form_data(clients["app"], "/inventory/intakes/new", token="operation_token")
            # Discard every candidate/field except authenticated tokens and this
            # run's new item ID. No existing product can enter this request.
            fields = {key: fields[key] for key in ("csrf_token", "operation_token")}
            fields.update(kind="transfer", item_ids=str(item_id), expected_count="1",
                          client_note=name + "。実物の発送・受領は行わない動作確認です。")
            report["created_intake"] = "unknown_until_response"
            response = clients["app"].request("POST", "/inventory/intakes/new", data=fields, expected={302, 303})
            intake_id = redirected_id(clients["app"], response, "/inventory/intakes/")
            report["intake_id"] = intake_id
            intake_path = f"/inventory/intakes/{intake_id}"
            response = clients["pc"].request("GET", intake_path, expected={200})
            require("new_intake_is_visible_on_pc", name in response.content.decode("utf-8"))
            report["created_intake"] = True
            for actor, action, extra in (
                ("staff", "approve", {"shipping_instructions": "【動作確認】架空の案内です。商品を送らないでください。"}),
                ("app", "ship", {"carrier": "架空・動作確認便", "tracking_number": "TEST-NO-SHIPMENT"}),
                ("staff", "receive", {"received_count": "1"}),
            ):
                stage = "fictional_intake_" + action
                fields, _, response = form_data(clients[actor], intake_path, action=action, token="operation_token")
                require(action + "_targets_this_runs_intake", name in response.content.decode("utf-8"))
                fields.update(extra)
                response = clients[actor].request("POST", intake_path, data=fields, expected={302, 303})
                require(action + "_keeps_same_intake_id", redirected_id(clients[actor], response, "/inventory/intakes/") == intake_id)
                passed("fictional_intake_" + action + "_accepted")
            stage = "receipt_keeps_shared_inventory_and_photo"
            own_detail("pc", item_id, name, "開花で保管・管理", note)
            own_detail("app", item_id, name, "開花で保管・管理", note)
            other = clients["other"].request("GET", f"/view/{item_id}")
            require("receipt_keeps_owner_access_boundary", other.status_code in {302, 403, 404} and name not in other.content.decode("utf-8", errors="replace"))
            fields, document, response = own_form("app", item_id, name)
            require("received_item_self_edit_is_locked", "この画面で編集できません" in response.content.decode("utf-8"))
            require("receipt_keeps_original_photo_path", photo_path(clients["app"], document) == original_photo)
            after_photo = clients["app"].request("GET", original_photo, expected={200})
            require("receipt_keeps_original_photo_bytes", after_photo.content == pc_photo.content)

        stage = "logout_sessions_independently"
        clients["app"].request("GET", "/logout", expected={302, 303})
        denied = clients["app"].request("GET", f"/view/{item_id}")
        require("app_logout_ends_only_app_cookie_session", denied.status_code in {302, 401, 403})
        own_detail("pc", item_id, name)
        passed("pc_cookie_session_survives_app_logout")
        stage = "persist_verification_manifest"
        ledger.write({"run_id": run_id, "host": config.host, "name": name,
                      "item_id": item_id, "photo_path": original_photo,
                      "photo_sha256": report["photo_sha256"], "received": bool(include_intake), "intake_id": intake_id})
        passed("non_secret_verification_manifest_saved")
    except VerificationStopped as error:
        report["checks"].append({"name": stage, "pass": False, "code": str(error)})
    except Exception:
        report["checks"].append({"name": stage, "pass": False, "code": "verification_stopped"})
    finally:
        report["http_requests"] = sum(client.requests for client in clients.values())
        for client in clients.values():
            client.close()
        report["passed"] = sum(check["pass"] for check in report["checks"])
        report["failed"] = sum(not check["pass"] for check in report["checks"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--with-intake", action="store_true", help="Also transfer this run's fictional new item through fictional staff receipt.")
    modes.add_argument("--verify-run", help="Recheck this completed run's own item/photo after restart; no inventory/intake writes (login history may update).")
    args = parser.parse_args()
    try:
        config = validate_environment()
    except StagingConfigurationError:
        print(json.dumps({"scope": "deployed staging", "passed": 0, "failed": 1, "code": "staging_environment_not_ready"}))
        return 1
    report = exercise(config, os.environ, include_intake=args.with_intake, verify_run=args.verify_run)
    print("DEPLOYED_STAGING_JSON=" + json.dumps(report, ensure_ascii=False))
    return int(report["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
