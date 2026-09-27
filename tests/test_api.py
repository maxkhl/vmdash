"""API, Start- und Klon-Jobs gegen das Mock-Backend."""

import json
import logging
import time

import pytest

from app.clone import check_preconditions, run_clone
from app.jobs import Job, JobManager
from app.clone import STEPS as CLONE_STEPS
from tests.conftest import make_config, wait_job

SECRET_OLD = "Alt-Geheim-1234"
SECRET_NEW = "Neu-Geheim-5678"


def start(client, name, passphrase):
    r = client.post(f"/api/vms/{name}/start", json={"passphrase": passphrase})
    assert r.status_code == 202, r.get_json()
    return r.get_json()["id"]


def clone(client, source="sap-template", suffix="acme", old=SECRET_OLD, new=SECRET_NEW):
    return client.post(
        f"/api/vms/{source}/clone",
        json={"suffix": suffix, "old_passphrase": old, "new_passphrase": new},
    )


def vm_state(client, name):
    return next(v for v in client.get("/api/vms").get_json() if v["name"] == name)


# ------------------------------------------------------------------ Allgemein


def test_healthz_and_info(client):
    assert client.get("/healthz").status_code == 200
    info = client.get("/api/info").get_json()
    assert info["mode"] == "mock"
    assert info["template"] == "sap-template"


def test_list_vms(client):
    vms = {v["name"]: v for v in client.get("/api/vms").get_json()}
    assert set(vms) == {"sap-template", "kunde-beispiel", "kunde-demo"}
    assert vms["sap-template"]["is_template"]
    assert vms["kunde-beispiel"]["state"] == "running"
    assert vms["kunde-demo"]["state"] == "shutoff"
    assert vms["kunde-demo"]["ip"] is None  # aus: keine IP
    assert vms["kunde-beispiel"]["ip"] == "192.168.122.243"
    assert vms["kunde-beispiel"]["rdp_url"] is None  # VMDASH_RDPGW_URL leer


def test_post_requires_json(client):
    r = client.post("/api/vms/kunde-demo/start", data={"passphrase": "x"})
    assert r.status_code == 415
    r = client.post("/api/vms/kunde-beispiel/shutdown")
    assert r.status_code == 415


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"vmdash" in r.data
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


# ------------------------------------------------------------------ Start


def test_start_and_unlock(client):
    jid = start(client, "kunde-demo", "richtig")
    job = wait_job(client, jid)
    assert job["state"] == "unlocked"
    assert [s["status"] for s in job["steps"]] == ["done", "done", "done"]
    assert vm_state(client, "kunde-demo")["state"] == "running"
    assert not vm_state(client, "kunde-demo")["busy"]


def test_start_wrong_then_unlock(client):
    jid = start(client, "kunde-demo", "falsch")
    job = wait_job(client, jid, awaiting=True)
    assert job["state"] == "wrong_key"
    assert job["passphrase_requests"] == 1
    r = client.post("/api/vms/kunde-demo/unlock", json={"passphrase": "richtig"})
    assert r.status_code == 202
    job = wait_job(client, jid)
    assert job["state"] == "unlocked"


def test_start_tries_exceeded(client):
    jid = start(client, "kunde-demo", "falsch")
    for _ in range(2):
        wait_job(client, jid, awaiting=True)
        assert client.post("/api/vms/kunde-demo/unlock", json={"passphrase": "falsch"}).status_code == 202
    job = wait_job(client, jid)
    assert job["state"] == "failed"
    assert "Limit" in job["error"]
    assert job["passphrase_requests"] == 2


def test_start_conflicts(client):
    assert client.post("/api/vms/kunde-beispiel/start", json={"passphrase": "x"}).status_code == 409
    assert client.post("/api/vms/gibtsnicht/start", json={"passphrase": "x"}).status_code == 404
    assert client.post("/api/vms/kunde-demo/start", json={}).status_code == 400
    assert client.post("/api/vms/kunde-demo/start", json={"passphrase": "a\nb"}).status_code == 400


def test_second_start_while_waiting_is_409(client):
    jid = start(client, "kunde-demo", "falsch")
    wait_job(client, jid, awaiting=True)
    r = client.post("/api/vms/kunde-demo/start", json={"passphrase": "x"})
    assert r.status_code == 409


def test_unlock_without_waiting_job(client):
    r = client.post("/api/vms/kunde-demo/unlock", json={"passphrase": "x"})
    assert r.status_code == 409


def test_console_busy(client, backend):
    # Konsole schon belegt: kein gewaltsames Übernehmen, VM wird wieder aus
    orig = backend.open_serial

    def busy(name):
        from app.backends.base import ConsoleBusy

        raise ConsoleBusy("Active console session exists for this domain")

    backend.open_serial = busy
    job = wait_job(client, start(client, "kunde-demo", "richtig"))
    backend.open_serial = orig
    assert job["state"] == "failed"
    assert "belegt" in job["error"]
    assert vm_state(client, "kunde-demo")["state"] == "shutoff"


def test_destroy_cancels_waiting_start(client):
    jid = start(client, "kunde-demo", "falsch")
    wait_job(client, jid, awaiting=True)
    assert client.post("/api/vms/kunde-demo/destroy", json={}).status_code == 200
    job = wait_job(client, jid)
    assert job["state"] == "failed"
    assert vm_state(client, "kunde-demo")["state"] == "shutoff"


def test_shutdown_and_destroy(client):
    assert client.post("/api/vms/kunde-beispiel/shutdown", json={}).status_code == 200
    assert client.post("/api/vms/kunde-demo/shutdown", json={}).status_code == 409
    assert client.post("/api/vms/kunde-demo/destroy", json={}).status_code == 409


def test_passphrases_never_leak(client, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "SuperGeheim-42"
    jid = start(client, "kunde-demo", "falsch")
    wait_job(client, jid, awaiting=True)
    client.post("/api/vms/kunde-demo/unlock", json={"passphrase": secret})
    wait_job(client, jid)
    r = clone(client, suffix="leak", old=SECRET_OLD, new=SECRET_NEW)
    job = wait_job(client, r.get_json()["id"], timeout=20)
    assert job["state"] == "done", job
    blob = json.dumps(client.get("/api/vms").get_json()) + json.dumps(job) + caplog.text
    for s in (secret, SECRET_OLD, SECRET_NEW, "falsch"):
        assert s not in blob


# ------------------------------------------------------------------ RDP


RDPGW = "https://rdpgw.example.de"


def test_rdp_connect_link(make_app):
    client = make_app(rdpgw_url=RDPGW).test_client()
    assert client.get("/api/info").get_json()["rdpgw"] is True
    vm = vm_state(client, "kunde-beispiel")
    assert vm["rdp_url"] == f"{RDPGW}/connect?host=192.168.122.243:3389"


def test_rdp_port_configurable(make_app):
    client = make_app(rdpgw_url=RDPGW, rdp_port=3390).test_client()
    assert vm_state(client, "kunde-beispiel")["rdp_url"].endswith("host=192.168.122.243:3390")


def test_rdp_ip_from_lease_fallback(make_app):
    client = make_app(rdpgw_url=RDPGW).test_client()
    # kunde-demo hat keine Reservierung, nur eine Lease
    job = wait_job(client, start(client, "kunde-demo", "richtig"))
    assert job["state"] == "unlocked"
    vm = vm_state(client, "kunde-demo")
    assert vm["ip"] == "192.168.122.77"
    assert vm["rdp_url"] == f"{RDPGW}/connect?host=192.168.122.77:3389"


def test_rdp_no_ip_no_link(make_app):
    client = make_app(rdpgw_url=RDPGW).test_client()
    # Template läuft, hat aber noch keine Lease (wartet am LUKS-Prompt)
    jid = start(client, "sap-template", "falsch")
    wait_job(client, jid, awaiting=True)
    vm = vm_state(client, "sap-template")
    assert vm["state"] == "running" and vm["ip"] is None and vm["rdp_url"] is None
    # nach dem Entsperren bekommt es eine Lease
    client.post("/api/vms/sap-template/unlock", json={"passphrase": "richtig"})
    wait_job(client, jid)
    for _ in range(100):
        if vm_state(client, "sap-template")["ip"]:
            break
        time.sleep(0.02)
    assert vm_state(client, "sap-template")["rdp_url"].startswith(RDPGW)


def test_rdp_off_without_url(client):
    assert client.get("/api/info").get_json()["rdpgw"] is False
    assert all(v["rdp_url"] is None for v in client.get("/api/vms").get_json())


def test_old_rdp_route_removed(client):
    assert client.get("/api/vms/kunde-beispiel/rdp").status_code == 404


def test_rdp_client_page_and_script(client):
    r = client.get("/rdp-client")
    assert r.status_code == 200 and "vmdash-rdp-setup.sh" in r.get_data(as_text=True)
    r = client.get("/client/vmdash-rdp-setup.sh")
    assert r.status_code == 200
    assert r.get_data(as_text=True).startswith("#!/usr/bin/env bash")
    assert "attachment" in r.headers["Content-Disposition"]


# ------------------------------------------------------------------ Klonen


def test_clone_full_flow(client, backend):
    r = clone(client)
    assert r.status_code == 202, r.get_json()
    job = wait_job(client, r.get_json()["id"], timeout=20)
    assert job["state"] == "done", job
    assert all(s["status"] == "done" for s in job["steps"]), job["steps"]
    assert job["result"]["vnc_port"] == 5912  # 5910, 5911, 5913 belegt

    vm = backend.vms["kunde-acme"]
    guest = backend._guest(vm)
    assert vm.state == "running"
    assert guest.hostname == "kunde-acme"
    assert b"kunde-acme" in guest.fs["/etc/hosts"][0]
    assert b"sap-template" not in guest.fs["/etc/hosts"][0]
    # Schlüsseldateien: 0600, ohne Zeilenumbruch, danach gelöscht
    assert not [p for p in guest.fs if p.startswith("/run/")]
    add, remove = vm.key_file_snapshots
    assert sorted(d for d, _m in add.values()) == sorted([SECRET_OLD.encode(), SECRET_NEW.encode()])
    assert all(mode == 0o600 for _d, mode in list(add.values()) + list(remove.values()))
    # Passphrasen nie als Argument
    argv = json.dumps(vm.exec_log)
    assert SECRET_OLD not in argv and SECRET_NEW not in argv
    cmds = [a[:2] for a in vm.exec_log]
    assert cmds.index(["cryptsetup", "luksAddKey"]) < cmds.index(["cryptsetup", "luksRemoveKey"])
    # Alter Keyslot weg, neuer gilt
    assert not guest.any_key_valid and guest.keys == {SECRET_NEW}
    # Template unverändert und wieder frei
    tpl = backend._guest(backend.vms["sap-template"])
    assert tpl.hostname == "sap-template" and tpl.any_key_valid
    assert not vm_state(client, "sap-template")["busy"]
    # DHCP-Reservierung mit der MAC des Klons
    mac = backend._mac("kunde-acme")
    host = next(h for h in backend.dhcp_hosts if h.name == "kunde-acme")
    assert host.mac == mac and host.ip == job["result"]["ip"] == "192.168.122.2"
    assert backend.get_ip("kunde-acme") == "192.168.122.2"
    # xrdp-Zertifikat nach dem Hostnamen neu erzeugt
    assert guest.snakeoil_cn == "kunde-acme" and guest.xrdp_restarts == 1
    assert cmds.index(["hostnamectl", "set-hostname"]) < cmds.index(["make-ssl-cert", "generate-default-snakeoil"])
    assert "CN = kunde-acme" in next(s["detail"] for s in job["steps"] if s["key"] == "xrdp_cert")
    # Neue Domain-XML
    xml = backend.vms["kunde-acme"].xml
    assert "kunde-acme.qcow2" in xml and "port=\"5912\"" in xml.replace("'", '"')


def test_cloned_vm_needs_new_passphrase(client, backend):
    job = wait_job(client, clone(client).get_json()["id"], timeout=20)
    assert job["state"] == "done"
    backend.destroy("kunde-acme")
    jid = start(client, "kunde-acme", SECRET_OLD)
    assert wait_job(client, jid, awaiting=True)["state"] == "wrong_key"
    client.post("/api/vms/kunde-acme/unlock", json={"passphrase": SECRET_NEW})
    assert wait_job(client, jid)["state"] == "unlocked"


def test_clone_retry_old_passphrase(client, backend):
    r = clone(client, old="falsch")
    jid = r.get_json()["id"]
    job = wait_job(client, jid, awaiting=True)
    assert job["state"] == "wrong_key"
    assert vm_state(client, "kunde-acme")["job"]["id"] == jid
    assert client.post("/api/vms/kunde-acme/unlock", json={"passphrase": SECRET_OLD}).status_code == 202
    job = wait_job(client, jid, timeout=20)
    assert job["state"] == "done", job
    add = backend.vms["kunde-acme"].key_file_snapshots[0]
    assert SECRET_OLD.encode() in [d for d, _m in add.values()]


@pytest.mark.parametrize(
    "suffix,old,new,msg",
    [
        ("ACME", "a", "b", "Kundenkürzel"),
        ("a b", "a", "b", "Kundenkürzel"),
        ("demo", "a", "b", "existiert bereits"),
        ("acme", "a", "a", "unterscheiden"),
        ("acme", "", "b", "fehlt"),
        ("acme-", "a", "b", "gültiger Hostname"),
        ("a" * 60, "a", "b", "gültiger Hostname"),
    ],
)
def test_clone_validation(client, suffix, old, new, msg):
    r = clone(client, suffix=suffix, old=old, new=new)
    assert r.status_code == 400
    assert msg in r.get_json()["error"]


def test_clone_source_running(client):
    r = clone(client, source="kunde-beispiel")
    assert r.status_code == 400
    assert "nicht ausgeschaltet" in r.get_json()["error"]


def test_clone_volume_exists(client, backend):
    from app.backends.mock_backend import GuestState, Volume

    backend.volumes["kunde-acme.qcow2"] = Volume("kunde-acme.qcow2", 1, GuestState("x"))
    r = clone(client)
    assert r.status_code == 400 and "Volume" in r.get_json()["error"]


def test_clone_no_space(client, backend):
    backend.pool_capacity = 60 * 1024**3  # belegt 58 GB, Template braucht 16*1,2
    r = clone(client)
    assert r.status_code == 400 and "Zu wenig Platz" in r.get_json()["error"]


def test_clone_no_vnc_port(make_app):
    client = make_app(vnc_port_min=5910, vnc_port_max=5911).test_client()
    r = clone(client)
    assert r.status_code == 400 and "VNC-Port" in r.get_json()["error"]


def test_clone_missing_agent_channel(client, backend):
    vm = backend.vms["sap-template"]
    vm.xml = vm.xml.replace("org.qemu.guest_agent.0", "anders")
    r = clone(client)
    assert r.status_code == 400
    assert "virt-xml sap-template --add-device" in r.get_json()["error"]


def test_source_locked_during_host_phase(make_app):
    app = make_app(mock_delay=0.2)
    client = app.test_client()
    r = clone(client)
    assert r.status_code == 202
    # Während vol-clone ist die Quelle gesperrt
    assert client.post("/api/vms/sap-template/start", json={"passphrase": "x"}).status_code == 409
    assert vm_state(client, "sap-template")["clone_job"]["id"] == r.get_json()["id"]
    wait_job(client, r.get_json()["id"], timeout=30)


FAIL_STEPS = [
    "clone_volume", "define", "dhcp", "unlock_old", "agent", "luks_add_key", "hostname",
    "xrdp_cert", "machine_id", "ssh_keys", "unlock_new", "luks_remove_key",
]


@pytest.mark.parametrize("step", FAIL_STEPS)
def test_clone_fail_step(make_app, step):
    app = make_app(mock_fail_step=step, agent_timeout=0.3)
    client = app.test_client()
    backend = app.extensions["vmdash"]["backend"]
    jid = clone(client).get_json()["id"]
    job = wait_job(client, jid, timeout=20, awaiting=True)
    while job["awaiting_passphrase"]:  # unlock_old: jede Eingabe wird abgelehnt
        client.post("/api/vms/kunde-acme/unlock", json={"passphrase": SECRET_OLD})
        job = wait_job(client, jid, timeout=20, awaiting=True)
    job = wait_job(client, jid, timeout=20)
    assert job["state"] == "failed"
    failed = [s["key"] for s in job["steps"] if s["status"] == "failed"]
    assert failed == [step], job["steps"]
    assert job["result"]["deletable"] == (step != "clone_volume")

    after_add = FAIL_STEPS.index(step) > FAIL_STEPS.index("luks_add_key")
    assert ("alte Keyslot ist noch aktiv" in (job["hint"] or "")) == after_add

    vm = backend.vms.get("kunde-acme")
    if vm is not None:
        guest = backend._guest(vm)
        assert not [p for p in guest.fs if p.startswith("/run/")]
        if step == "unlock_new":
            assert ["cryptsetup", "luksRemoveKey"] not in [a[:2] for a in vm.exec_log]
            assert guest.any_key_valid  # alte Passphrase geht noch


def test_delete_failed_clone(make_app):
    app = make_app(mock_fail_step="hostname")
    client = app.test_client()
    backend = app.extensions["vmdash"]["backend"]
    jid = clone(client).get_json()["id"]
    assert wait_job(client, jid, timeout=20)["state"] == "failed"
    assert backend.vms["kunde-acme"].state == "running"
    assert client.post("/api/vms/kunde-acme/delete", json={}).status_code == 200
    assert "kunde-acme" not in backend.vms
    assert "kunde-acme.qcow2" not in backend.volumes
    assert "sap-template.qcow2" in backend.volumes
    assert not [h for h in backend.dhcp_hosts if h.name == "kunde-acme"]
    assert [h for h in backend.dhcp_hosts if h.name == "kunde-beispiel"]
    # zweites Mal: nichts mehr zu löschen
    assert client.post("/api/vms/kunde-acme/delete", json={}).status_code == 409


def test_delete_orphan_volume_after_define_failure(make_app):
    app = make_app(mock_fail_step="define")
    client = app.test_client()
    backend = app.extensions["vmdash"]["backend"]
    jid = clone(client).get_json()["id"]
    assert wait_job(client, jid, timeout=20)["state"] == "failed"
    assert "kunde-acme.qcow2" in backend.volumes
    # erscheint bei der Quelle, weil die Ziel-VM nicht existiert
    assert vm_state(client, "sap-template")["clone_job"]["result"]["deletable"]
    assert client.post("/api/vms/kunde-acme/delete", json={}).status_code == 200
    assert "kunde-acme.qcow2" not in backend.volumes


def test_delete_only_for_failed_clones(client):
    assert client.post("/api/vms/kunde-demo/delete", json={}).status_code == 409
    assert client.post("/api/vms/sap-template/delete", json={}).status_code == 409


def test_secrets_cleared_after_clone():
    from app.backends.mock_backend import MockBackend

    for fail in ("", "hostname"):
        cfg = make_config(mock_fail_step=fail)
        be = MockBackend(cfg)
        plan = check_preconditions(be, cfg, "sap-template", "x", SECRET_OLD, SECRET_NEW)
        job = Job("clone", plan.target, CLONE_STEPS, source=plan.source)
        secrets = {"old": SECRET_OLD, "new": SECRET_NEW}
        run_clone(be, cfg, JobManager(), job, plan, secrets)
        assert secrets == {}
        assert job.state == ("failed" if fail else "done")


def test_clone_xrdp_not_snakeoil(client, backend):
    backend._guest(backend.vms["sap-template"]).links["/etc/xrdp/cert.pem"] = "/etc/xrdp/eigen.pem"
    job = wait_job(client, clone(client).get_json()["id"], timeout=20)
    assert job["state"] == "failed"
    assert "kein Symlink auf /etc/ssl/certs/ssl-cert-snakeoil.pem" in job["error"]
    assert [s["key"] for s in job["steps"] if s["status"] == "failed"] == ["xrdp_cert"]
    argv = [a[0] for a in backend.vms["kunde-acme"].exec_log]
    assert "make-ssl-cert" not in argv


def test_clone_xrdp_not_installed_skipped(client, backend):
    backend._guest(backend.vms["sap-template"]).xrdp_installed = False
    job = wait_job(client, clone(client).get_json()["id"], timeout=20)
    assert job["state"] == "done"
    step = next(s for s in job["steps"] if s["key"] == "xrdp_cert")
    assert step["status"] == "skipped"


def test_clone_stale_reservation(client, backend):
    from app.network import DhcpHost

    backend.dhcp_hosts.append(DhcpHost("52:54:00:aa:bb:cc", "kunde-acme", "192.168.122.9"))
    r = clone(client)
    assert r.status_code == 400
    assert "DHCP-Reservierung für „kunde-acme“" in r.get_json()["error"]


def test_clone_no_free_ip(client, backend):
    import app.backends.mock_backend as mb

    orig = mb.NET_RANGE
    mb.NET_RANGE = ("192.168.122.243", "192.168.122.243")  # einzige IP ist reserviert
    try:
        r = clone(client)
    finally:
        mb.NET_RANGE = orig
    assert r.status_code == 400 and "Keine freie IP" in r.get_json()["error"]
