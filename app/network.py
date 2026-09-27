"""IP-Adressen der VMs im libvirt-NAT-Netz (DHCP-Reservierungen und Leases)."""

import ipaddress
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class DhcpHost:
    mac: str
    name: str
    ip: str


@dataclass
class DhcpState:
    """Zustand des DHCP im libvirt-Netz (nur IPv4)."""

    network: str
    gateway: str = ""  # Adresse des Netzes selbst (z. B. 192.168.122.1)
    ranges: list = field(default_factory=list)  # [(start, end)] als Strings
    hosts: list = field(default_factory=list)  # [DhcpHost] – feste Reservierungen
    leases: list = field(default_factory=list)  # [(mac, ip)] – aktuelle Leases


def parse_network_xml(network, xml):
    """Liest Gateway, DHCP-Bereiche und Reservierungen aus der Netz-XML."""
    state = DhcpState(network)
    root = ET.fromstring(xml)
    for ip in root.findall("ip"):
        if ip.get("family", "ipv4") != "ipv4":
            continue
        state.gateway = state.gateway or ip.get("address", "")
        for r in ip.findall("dhcp/range"):
            state.ranges.append((r.get("start"), r.get("end")))
        for h in ip.findall("dhcp/host"):
            if h.get("mac") and h.get("ip"):
                state.hosts.append(DhcpHost(h.get("mac").lower(), h.get("name", ""), h.get("ip")))
    return state


def vm_macs(domain_xml, network):
    """MAC-Adressen der Netzwerkkarten einer Domain, die im Netz network hängen."""
    root = ET.fromstring(domain_xml)
    macs = []
    for iface in root.findall("./devices/interface[@type='network']"):
        src = iface.find("source")
        mac = iface.find("mac")
        if src is not None and src.get("network") == network and mac is not None and mac.get("address"):
            macs.append(mac.get("address").lower())
    return macs


def ip_for_macs(state, macs):
    """1. feste Reservierung, 2. aktuelle Lease, sonst None."""
    macs = [m.lower() for m in macs]
    for mac in macs:
        for h in state.hosts:
            if h.mac == mac:
                return h.ip
    for mac in macs:
        for lease_mac, ip in state.leases:
            if lease_mac.lower() == mac:
                return ip
    return None


def free_ip(state):
    """Niedrigste IP im DHCP-Bereich, die weder reserviert noch vergeben ist."""
    used = {h.ip for h in state.hosts} | {ip for _mac, ip in state.leases}
    if state.gateway:
        used.add(state.gateway)
    for start, end in state.ranges:
        a, b = int(ipaddress.IPv4Address(start)), int(ipaddress.IPv4Address(end))
        for n in range(a, b + 1):
            ip = str(ipaddress.IPv4Address(n))
            if ip not in used:
                return ip
    return None
