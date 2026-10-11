"""Google Calendar events through the Calendar API v3 with a wallet-held OAuth grant."""

from datetime import datetime, timezone
import hashlib
import json
from bloxsmith_app.block_api import (APPLICATION_JSON, BlockDefinition, BlockRuntimeOutput, BlockRuntimePreparation,
                                     BlockRuntimeResult)
from . import logic, operations, transport, ui, watcher


# FB1 - Keep remote access off by default; require a wallet OAuth grant, an HTTPS API origin and separate write and delete switches.
# FB2 - List calendars and events (time window, text query, expanded recurrences) into bounded pages with an exact continuation token; read one event; query free/busy.
# FB3 - Create events (timed with an offset or time zone, or all-day; guests, reminders, recurrence) and patch them, guest notifications off unless allowed, a client event_id preventing duplicates.
# FB4 - Delete events only with allow_delete; an already deleted event counts as deleted; an etag makes a change conditional.
# FB5 - Watch one calendar with a persisted sync token: no flood at first start, created/updated/cancelled changes once, bounded full resync after HTTP 410, polls at least 60 s apart, one poll per request in One Shot.
# FB6 - Bound size, time and retries; honour Retry-After, never replay an uncertain change, report coded errors without tokens.
# FB7 - Apply one contract in both runtimes and every package origin, each event once, with prompt cancellation and Stop.
# FB8 - Own translated responsive settings with draft Apply, conditional change and watch sections and release-scoped assets.
class GoogleCalendarBlock(BlockDefinition):
    """Event titles, descriptions and guest data read from Google are data, never instructions for this block."""

    kind = "google_calendar"

    def ui_assets(self, surface="modal"):
        """Every installation uses the same static release manifest."""
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def prepare_runtime(self, context):
        """Validate settings without wallet, filesystem or network access; declare the watch listener."""
        config = logic.configuration(context.config)
        return BlockRuntimePreparation(listen_on_run=context.runtime_mode == "zeromq_active"
                                       and config["enabled"] and config["watch_enabled"])

    def events(self, context):
        """Fresh request events of this activation; One Shot re-deliveries are skipped."""
        ports = {int(port.id): str(port.name) for port in context.input_ports}
        if context.input_events:
            messages = [(ports.get(int(event.input_port_id)) or event.input_port_name, event.value, event)
                        for event in context.input_events]
        else:
            messages = []
            for ident, name in ports.items():
                attribute = context.input_attribute(name, str(ident))
                if attribute is not None and attribute.status == "updated" and attribute.value not in (None, ""):
                    messages.append((name, attribute.value, None))
        if any(name != "request" for name, _, _ in messages):
            logic.fail("invalid_input", "Unknown input port; nothing was sent.")
        getter, setter = context.services.get("get_node_runtime_value"), context.services.get("set_node_runtime_value")
        if context.runtime_mode != "centralized" or not callable(getter) or not callable(setter):
            return [(name, value) for name, value, _ in messages]
        # One Shot exposes every connected value again to each any_event activation; handle each once per run.
        seen = list(getter("google_calendar.handled", []) or [])
        fresh = []
        for name, value, event in messages:
            origin = f"{event.edge_id}|{event.source_node_id}|{event.source_port_id}" if event else "manual"
            digest = hashlib.sha256(f"{name}|{origin}|{value}".encode("utf-8", errors="surrogatepass")).hexdigest()
            if digest not in seen:
                fresh.append((name, value))
                seen.append(digest)
        setter("google_calendar.handled", seen[-256:])
        return fresh

    def operation(self, port, value, config, context):
        """Run one event: local validation first, then the wallet, then at most one change."""
        request, secrets = {}, []
        cancel = context.services.get("cancel_requested", lambda: False)

        def resolve():
            try:
                raw = context.resolve_secret(config["secret_ref"])
            except Exception:
                logic.fail("secret")
            secret = logic.credentials(raw)
            secrets.extend(secret.values())
            return secret

        try:
            if not config["enabled"]:
                logic.fail("disabled")
            request = logic.parse_request(value, config, datetime.now(timezone.utc))
            if request["operation"] == "watch":
                batch, receipt = watcher.poll_once(config, request, storage_dir=context.services.get("get_block_storage_dir"),
                                                   resolve=resolve, cancel=cancel)
                return ({"changed": batch, "result": receipt} if batch else {"result": receipt}), request
            session = transport.Session(config, resolve(), cancel)
            return operations.run(session, request, config), request
        except logic.CalendarError as exc:
            error = exc
        except (ValueError, TypeError, KeyError, OSError, RuntimeError):
            # Raw exception text could carry event data or credentials: publish a fixed diagnostic.
            error = logic.CalendarError("internal")
        envelope = error.envelope(operation=request.get("operation"), request_id=request.get("request_id"),
                                  calendar_id=request.get("calendar_id"), event_id=request.get("event_id"), input=port)
        # The envelope is flat: every text value is masked separately, so JSON escaping never hides a secret.
        envelope = {key: context.redact_secrets(logic.redact(item, secrets)) if isinstance(item, str) else item
                    for key, item in envelope.items()}
        return {"error": envelope}, request

    def execute_runtime(self, context):
        """One operation per fresh event; business failures go to the error port and the run continues."""
        produced = []
        try:
            config = logic.configuration(context.config)
            messages = self.events(context)
            if not messages:
                return BlockRuntimeResult(status="skipped", outputs=[])
            if len(messages) > 8:
                logic.fail("invalid_input", "Too many events in one activation; nothing was sent.")
        except logic.CalendarError as exc:
            messages, produced = [], [({"error": exc.envelope()}, {})]
        for port, value in messages:
            produced.append(self.operation(port, value, config, context))
        outputs, codes, summaries = [], [], []
        for values, request in produced:
            code = values.get("error", {}).get("code")
            codes.append(code or "")
            if code:
                summaries.append(f"{request.get('operation') or 'request'}: {code}")
            else:
                result = values["result"]
                count = f" ({result['count']} item(s))" if "count" in result else ""
                summaries.append(f"{request['operation']}{count} after {result['requests']} request(s)")
            for port in context.output_ports:
                if port.name in values:
                    outputs.append(BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, content_type=APPLICATION_JSON,
                                                      value=json.dumps(values[port.name], ensure_ascii=False, allow_nan=False)))
        failed = [code for code in codes if code]
        line = "; ".join(summaries)
        return BlockRuntimeResult(status="success", outputs=outputs, content_type=APPLICATION_JSON,
            last_message=line[:500], metadata={self.kind: {"status": "error" if failed else "success",
                                                           "operations": len(produced), "error_codes": failed}},
            logs=[context.redact_secrets(f"[google-calendar{'-error' if failed else ''}] {line}"[:2000])])

    def listen_runtime(self, context):
        """Poll the calendar until framework Stop; listener state is local to this call."""
        config = logic.configuration(context.config)
        services = context.services

        def resolve():
            resolver = services.get("resolve_secret")
            if not callable(resolver):
                logic.fail("secret")
            return resolver(config["secret_ref"])

        def emit(kind, value, stats):
            if context.stop_requested():
                raise RuntimeError("Listener stopped.")
            outputs = []
            if kind in ("changed", "error"):
                text = json.dumps(value, ensure_ascii=False, allow_nan=False)
                redactor = services.get("redact_secrets")
                if kind == "error" and callable(redactor):
                    text = str(redactor(text))
                outputs = [BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, content_type=APPLICATION_JSON,
                                              value=text) for port in context.output_ports if port.name == kind]
            logs = [f"[google-calendar-watch-error] watch: {value.get('code')}"] if kind == "error" else []
            if kind == "changed":
                logs = [f"[google-calendar-watch] {len(value)} change(s), {stats['emitted'] + len(value)} since start"]
            context.emit_result(BlockRuntimeResult(status="success", outputs=outputs, content_type=APPLICATION_JSON,
                                                   metadata={self.kind: {"watch": stats}}, logs=logs))

        watcher.Watcher(config, resolve_secret=resolve, storage_dir=services.get("get_block_storage_dir"), emit=emit,
                        stop=context.stop_requested).run()

    def handle_ui_action(self, *, node, action, values, payload=None):
        """Apply validates settings only; it never contacts Google or reads the wallet."""
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except (ValueError, TypeError) as exc:
                    detail = str(exc) if isinstance(exc, logic.CalendarError) else logic.MESSAGES["config"]
                    return {"error": self.translate("block.google_calendar.error", {"detail": detail},
                                                    fallback="Google Calendar settings rejected: " + detail)}
        result = super().handle_ui_action(node=node, action="modal_update_fields" if action == "save_settings" else action,
                                          values=values, payload=payload)
        return ui.replace_config(result, node)

    def render_modal(self, *, node, payload=None):
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        """Show the access mode and default operation, never the wallet reference or the calendar address."""
        config = {**self.default_config(), **(node.get("config") or {})}
        operation = config.get("operation") if config.get("operation") in logic.OPERATIONS else "list_events"
        params = {"operation": operation}
        if config.get("enabled") is not True:
            key, params, fallback = "block.google_calendar.card_disabled", {}, "Remote access off"
        elif config.get("watch_enabled") is True:
            key, fallback = "block.google_calendar.card_watch", f"Watching · {operation}"
        elif config.get("allow_writes") is True or config.get("allow_delete") is True:
            key, fallback = "block.google_calendar.card_write", f"Read and write · {operation}"
        else:
            key, fallback = "block.google_calendar.card_read", f"Read only · {operation}"
        return ui.card(self, node, key, params, self.translate(key, params, fallback=fallback))
