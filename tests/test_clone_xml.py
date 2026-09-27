"""XML-Anpassung beim Klonen, getestet gegen die echte Template-XML."""

import shutil
import subprocess

import pytest

from app.clone import (
    CloneError,
    _hosts_sed_expr,
    build_clone_xml,
    free_vnc_port,
    has_agent_channel,
    main_disk,
    parse_xml,
)
from app.config import DEFAULT_TEMPLATE_XML

TEMPLATE = DEFAULT_TEMPLATE_XML.read_text(encoding="utf-8")
NEW_DISK = "/var/lib/libvirt/images/kunde-acme.qcow2"


@pytest.fixture
def clone():
    return parse_xml(build_clone_xml(TEMPLATE, "kunde-acme", NEW_DISK, 5914))


def test_name_uuid_mac(clone):
    assert clone.findtext("name") == "kunde-acme"
    assert clone.find("uuid") is None
    assert clone.findall(".//mac") == []
    # Netzwerk selbst bleibt
    assert clone.find("./devices/interface/source").get("network") == "default"


def test_disk_source(clone):
    disk = main_disk(clone)
    assert disk.find("source").get("file") == NEW_DISK
    assert disk.find("driver").get("type") == "qcow2"
    # CD-ROM unverändert
    cdrom = clone.find("./devices/disk[@device='cdrom']")
    assert cdrom is not None and cdrom.find("source") is None


def test_vnc(clone):
    vnc = clone.find("./devices/graphics[@type='vnc']")
    assert vnc.get("port") == "5914"
    assert vnc.get("autoport") == "no"
    assert vnc.get("listen") == "127.0.0.1"
    assert vnc.get("keymap") == "de"
    assert vnc.find("listen").get("address") == "127.0.0.1"


def test_spice_unchanged(clone):
    spice = clone.find("./devices/graphics[@type='spice']")
    assert spice.get("autoport") == "yes"
    assert spice.get("port") is None


def test_nvram(clone):
    nvram = clone.find("./os/nvram")
    assert nvram.get("template") == "/var/lib/libvirt/qemu/nvram/sap-template_VARS.fd"
    assert nvram.text == "/var/lib/libvirt/qemu/nvram/kunde-acme_VARS.fd"
    assert nvram.get("format") == "raw"
    loader = clone.find("./os/loader")
    assert loader.text == "/usr/share/OVMF/OVMF_CODE_4M.ms.fd"


def test_everything_else_unchanged(clone):
    src = parse_xml(TEMPLATE)
    assert has_agent_channel(clone)
    for path in ("memory", "currentMemory", "vcpu"):
        assert clone.findtext(path) == src.findtext(path)
    assert clone.find("cpu").attrib == src.find("cpu").attrib
    assert clone.find("./devices/serial").get("type") == "pty"
    assert clone.find("./devices/console/target").get("type") == "serial"
    assert len(clone.findall("./devices/channel")) == 2
    assert clone.find("./devices/tpm") is not None


def test_namespace_prefix_preserved():
    xml = build_clone_xml(TEMPLATE, "kunde-acme", NEW_DISK, 5914)
    assert "libosinfo:libosinfo" in xml
    assert "ns0:" not in xml


def test_source_xml_not_modified():
    before = TEMPLATE
    build_clone_xml(TEMPLATE, "kunde-acme", NEW_DISK, 5914)
    assert TEMPLATE == before


def test_free_vnc_port():
    # Template belegt 5913
    assert free_vnc_port([TEMPLATE], 5910, 5919) == 5910
    xmls = [TEMPLATE] + [
        build_clone_xml(TEMPLATE, f"k{p}", f"/x/{p}.qcow2", p) for p in (5910, 5911, 5912)
    ]
    assert free_vnc_port(xmls, 5910, 5919) == 5914
    assert free_vnc_port(xmls, 5910, 5913) is None


def test_autoport_minus_one_ignored():
    xml = TEMPLATE.replace("port='5913' autoport='no'", "port='-1' autoport='yes'")
    assert free_vnc_port([xml], 5910, 5910) == 5910


def test_missing_agent_channel():
    xml = TEMPLATE.replace("org.qemu.guest_agent.0", "something.else")
    assert not has_agent_channel(parse_xml(xml))


def test_two_disks_rejected():
    root = parse_xml(TEMPLATE)
    disk = main_disk(root)
    root.find("devices").append(disk)
    with pytest.raises(CloneError, match="2 Festplatten"):
        main_disk(root)


HOSTS = (
    "127.0.0.1\tlocalhost\n"
    "127.0.1.1\tsap-template.example sap-template\n"
    "10.0.0.5 sap-template-old sap-template\n"
)


@pytest.mark.skipif(not shutil.which("sed"), reason="sed nicht verfügbar")
def test_hosts_sed_expression():
    out = subprocess.run(
        ["sed", "-E", "-e", _hosts_sed_expr("sap-template", "kunde-acme")],
        input=HOSTS.encode(), capture_output=True, check=True,
    ).stdout.decode()
    lines = out.splitlines()
    assert lines[1] == "127.0.1.1\tkunde-acme.example kunde-acme"
    # "sap-template-old" ist ein anderer Name und bleibt
    assert lines[2] == "10.0.0.5 sap-template-old kunde-acme"
    assert lines[0] == "127.0.0.1\tlocalhost"
