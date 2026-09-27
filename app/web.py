"""Routen und API. Keine eigene Anmeldung (Authentik Forward Auth davor)."""

import logging

from flask import Flask, jsonify, request, send_from_directory

from . import __version__
from .backends import create_backend
from .backends.base import RUNNING, SHUTOFF, BackendError, NotFound
from .clone import STEPS as CLONE_STEPS
from .clone import CloneError, check_preconditions, run_clone, validate_passphrase
from .config import ROOT_DIR, Config
from .jobs import Busy, Job, JobManager, NotAwaiting
from .unlock import UnlockFailed, boot_and_unlock

log = logging.getLogger(__name__)

START_STEPS = [
    ("start", "VM starten"),
    ("prompt", "Auf LUKS-Prompt warten"),
    ("unlock", "Passphrase eingeben und entsperren"),
]


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def create_app(cfg=None, backend=None):
    cfg = cfg or Config.from_env()
    backend = backend or create_backend(cfg)
    jobs = JobManager()

    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.extensions["vmdash"] = {"cfg": cfg, "backend": backend, "jobs": jobs}

    # ------------------------------------------------------------ Allgemein
    @app.before_request
    def require_json():
        # Schutz gegen Cross-Site-Formulare: die können kein application/json senden.
        if request.method == "POST" and not request.is_json:
            raise ApiError(415, "Content-Type: application/json erforderlich")

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(ApiError)
    def api_error(e):
        return jsonify({"error": e.message}), e.status

    @app.errorhandler(BackendError)
    def backend_error(e):
        status = 404 if isinstance(e, NotFound) else 502
        return jsonify({"error": str(e)}), status

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def require_vm(name):
        vm = backend.get_vm(name)
        if vm is None:
            raise ApiError(404, f"VM „{name}“ nicht gefunden")
        return vm

    def passphrase_from(data, key="passphrase", what="Passphrase"):
        p = data.get(key)
        try:
            validate_passphrase(p, what)
        except CloneError as e:
            raise ApiError(400, str(e))
        return p

    # ------------------------------------------------------------ Seiten
    @app.get("/")
    def index():
        resp = send_from_directory(app.static_folder, "index.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/rdp-client")
    def rdp_client_page():
        resp = send_from_directory(app.static_folder, "rdp-client.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/client/vmdash-rdp-setup.sh")
    def rdp_setup_script():
        return send_from_directory(
            ROOT_DIR / "client", "vmdash-rdp-setup.sh",
            mimetype="text/x-shellscript", as_attachment=True, max_age=0,
        )

    @app.get("/healthz")
    def healthz():
        return "ok\n", 200, {"Content-Type": "text/plain"}

    @app.get("/api/info")
    def info():
        return jsonify(
            {
                "mode": backend.mode,
                "version": __version__,
                "template": cfg.template_vm,
                "clone_prefix": cfg.clone_prefix,
                "vnc_port_min": cfg.vnc_port_min,
                "vnc_port_max": cfg.vnc_port_max,
                "rdpgw": bool(cfg.rdpgw_url),
            }
        )

    # ------------------------------------------------------------ VMs
    @app.get("/api/vms")
    def list_vms():
        all_jobs = jobs.all()
        vms = backend.list_vms()
        dhcp = None
        if any(v.state == RUNNING for v in vms):
            try:
                dhcp = backend.dhcp_state()
            except BackendError as e:
                log.warning("DHCP-Zustand nicht lesbar: %s", e)
        out = []
        for vm in vms:
            job = next((j for j in reversed(all_jobs) if j.vm == vm.name), None)
            # Klon-Job, dessen Ziel noch nicht definiert ist, erscheint bei der Quelle
            clone_job = next(
                (
                    j
                    for j in reversed(all_jobs)
                    if j.kind == "clone" and j.source == vm.name and not j.result.get("defined")
                ),
                None,
            )
            ip = None
            if vm.state == RUNNING and dhcp is not None:
                try:
                    ip = backend.get_ip(vm.name, dhcp)
                except BackendError:
                    ip = None
            # Normaler Link: die .rdp-Datei muss direkt im Browser von rdpgw kommen
            # (Token an Browser-Sitzung und Client-IP gebunden).
            rdp_url = (
                f"{cfg.rdpgw_url}/connect?host={ip}:{cfg.rdp_port}"
                if cfg.rdpgw_url and ip
                else None
            )
            out.append(
                {
                    "name": vm.name,
                    "state": vm.state,
                    "is_template": vm.name == cfg.template_vm,
                    "job": job.to_dict() if job else None,
                    "clone_job": clone_job.to_dict() if clone_job else None,
                    "busy": jobs.active_for(vm.name) is not None,
                    "ip": ip,
                    "rdp_url": rdp_url,
                }
            )
        return jsonify(out)

    @app.post("/api/vms/<name>/start")
    def start(name):
        passphrase = passphrase_from(body())
        vm = require_vm(name)
        if vm.state != SHUTOFF:
            raise ApiError(409, f"VM „{name}“ ist nicht ausgeschaltet.")
        job = Job("start", name, START_STEPS)
        box = {"p": passphrase}
        del passphrase
        try:
            jobs.submit(job, [name], lambda j: run_start(j, box))
        except Busy:
            raise ApiError(409, f"Für „{name}“ läuft bereits ein Vorgang.")
        return jsonify(job.to_dict()), 202

    def run_start(job, box):
        def on_state(state):
            job.set_state(state)
            if state == "starting":
                job.begin("start")
            elif state == "waiting_prompt":
                job.complete("start", "läuft")
                job.begin("prompt")
            elif state == "unlocking":
                job.complete("prompt", "Prompt erkannt")
                job.begin("unlock", "Passphrase eingegeben, warte auf Ergebnis …")
            elif state == "wrong_key":
                job.update("unlock", "Passphrase falsch – bitte erneut eingeben")
            elif state == "unlocked":
                job.complete("unlock", "Entsperrt")

        try:
            boot_and_unlock(
                backend, cfg, job.vm, box.pop("p"), on_state,
                ask_again=lambda: job.wait_for_passphrase(cfg.retry_timeout),
                cancel_event=job.cancel_event,
            )
            job.finish("unlocked")
        except UnlockFailed as e:
            job.fail(str(e))
        except BackendError as e:
            job.fail(f"Start fehlgeschlagen: {e}")
        finally:
            box.clear()

    @app.post("/api/vms/<name>/unlock")
    def unlock(name):
        passphrase = passphrase_from(body())
        job = jobs.active_for(name)
        if job is None or job.vm != name:
            raise ApiError(409, f"Für „{name}“ wartet kein Vorgang auf eine Passphrase.")
        try:
            job.provide_passphrase(passphrase)
        except NotAwaiting:
            raise ApiError(409, "Der Vorgang wartet gerade nicht auf eine Passphrase.")
        return jsonify(job.to_dict()), 202

    @app.post("/api/vms/<name>/shutdown")
    def shutdown(name):
        vm = require_vm(name)
        if vm.state != RUNNING:
            raise ApiError(409, f"VM „{name}“ läuft nicht.")
        job = jobs.active_for(name)
        if job is not None and job.kind == "clone":
            raise ApiError(409, "Für diese VM läuft gerade ein Klon-Vorgang.")
        backend.shutdown(name)
        return jsonify({"ok": True})

    @app.post("/api/vms/<name>/destroy")
    def destroy(name):
        vm = require_vm(name)
        if vm.state == SHUTOFF:
            raise ApiError(409, f"VM „{name}“ ist bereits aus.")
        job = jobs.active_for(name)
        if job is not None and job.vm == name:
            job.cancel()  # z. B. wartender Start-Job
        backend.destroy(name)
        return jsonify({"ok": True})

    # ------------------------------------------------------------ Klonen
    @app.post("/api/vms/<name>/clone")
    def clone(name):
        data = body()
        suffix = data.get("suffix")
        secrets = {"old": data.get("old_passphrase"), "new": data.get("new_passphrase")}
        data.clear()  # auch den von Flask gecachten Body leeren
        if jobs.active_for(name) is not None:
            raise ApiError(409, f"Für „{name}“ läuft bereits ein Vorgang.")
        try:
            plan = check_preconditions(
                backend, cfg, name, suffix, secrets["old"], secrets["new"],
            )
        except CloneError as e:
            secrets.clear()
            raise ApiError(400, str(e))
        job = Job("clone", plan.target, CLONE_STEPS, source=plan.source)
        job.set_result(vnc_port=plan.vnc_port)
        try:
            jobs.submit(
                job, [plan.source, plan.target],
                lambda j: run_clone(backend, cfg, jobs, j, plan, secrets),
            )
        except Busy:
            secrets.clear()
            raise ApiError(409, "Quelle oder Ziel ist gerade durch einen anderen Vorgang belegt.")
        return jsonify(job.to_dict()), 202

    @app.post("/api/vms/<name>/delete")
    def delete(name):
        job = jobs.latest_for_target(name)
        if (
            job is None
            or job.kind != "clone"
            or job.state != "failed"
            or not job.result.get("deletable")
        ):
            raise ApiError(409, "Löschen ist nur für fehlgeschlagene Klone möglich.")
        if jobs.active_for(name) is not None:
            raise ApiError(409, f"Für „{name}“ läuft gerade ein Vorgang.")
        dhcp = job.result.get("dhcp")
        if dhcp:
            try:
                backend.remove_dhcp_host(dhcp["mac"], dhcp["name"], dhcp["ip"])
            except BackendError as e:
                log.warning("DHCP-Reservierung für %s nicht entfernt: %s", name, e)
        vm = backend.get_vm(name)
        if vm is not None:
            if vm.state != SHUTOFF:
                backend.destroy(name)
            backend.undefine_with_storage(name)
        else:
            vol = job.result.get("volume")
            if vol and backend.volume_exists(vol):
                backend.delete_volume(vol)
        job.set_result(deletable=False, deleted=True)
        log.info("Fehlgeschlagener Klon %s gelöscht", name)
        return jsonify({"ok": True})

    # ------------------------------------------------------------ Jobs
    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        job = jobs.get(job_id)
        if job is None:
            raise ApiError(404, "Vorgang nicht gefunden.")
        return jsonify(job.to_dict())

    return app
